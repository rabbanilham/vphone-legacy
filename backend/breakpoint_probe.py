#!/usr/bin/env python3
"""Inspect a real guest at a GDB breakpoint without modifying firmware files."""
import argparse
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from smoke_test import command

def rsp(sock, packet):
    data = packet.encode()
    sock.sendall(b'$' + data + b'#' + f'{sum(data) & 255:02x}'.encode())
    return receive_rsp(sock)


def receive_rsp(sock, started=False):
    """Receive one pending response without sending another guest operation."""
    while not started:
        start = sock.recv(1)
        if not start:
            raise RuntimeError('GDB disconnected before response')
        if start == b'$':
            break
    payload = bytearray()
    while True:
        c = sock.recv(1)
        if c == b'#': break
        if not c: raise RuntimeError('GDB disconnected')
        payload.extend(c)
    checksum = bytearray()
    while len(checksum) < 2:
        part = sock.recv(2 - len(checksum))
        if not part:
            raise RuntimeError('GDB disconnected during checksum')
        checksum.extend(part)
    if int(checksum, 16) != sum(payload) & 255:
        raise RuntimeError('GDB response checksum mismatch')
    sock.sendall(b'+')
    return payload.decode()


def capture_hmp(fp, output, commands):
    for i, hmp in enumerate(commands):
        result = command(fp, 'human-monitor-command', {'command-line': hmp})
        (output / f'hmp-{i}.txt').write_text(hmp + '\n' + result)
        print(result)

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--plan', type=Path, required=True)
    stop = p.add_mutually_exclusive_group(required=True)
    stop.add_argument('--breakpoint')
    stop.add_argument('--watchpoint', help='Eight-byte write watchpoint address')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--hmp', action='append', default=[])
    p.add_argument('--boot-el', type=int, choices=(1, 3), default=1)
    p.add_argument('--condition-substring', help='Continue breakpoint hits until this register dump substring matches')
    p.add_argument('--system-image', type=Path, help='Explicit raw System image for the modeled ANS device')
    p.add_argument('--data-image', type=Path, help='Explicit raw Data image; use a diagnostic copy')
    p.add_argument('--utility-image', type=Path, help='Committed logical utility container; use a diagnostic copy')
    p.add_argument('--system-writable', action='store_true', help='Allow writes to the supplied diagnostic System copy')
    p.add_argument('--timeout-seconds', type=float, default=30,
                   help='Total guest breakpoint search bound (default: 30 seconds)')
    args = p.parse_args(); plan = json.loads(args.plan.read_text())
    if not 0 < args.timeout_seconds <= 300:
        p.error('Timeout must be greater than zero and at most 300 seconds')
    if bool(args.system_image) != bool(args.data_image) or (args.system_writable and not args.system_image):
        p.error('Supply both raw images; writable mode requires a diagnostic System image')
    if args.utility_image and not args.system_image:
        p.error('Utility model requires the explicit System/Data image pair')
    if hashlib.sha256(Path(plan['image']).read_bytes()).hexdigest() != plan['image_sha256']:
        p.error('Boot image differs from the retained plan')
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=True)
    binary = Path(__file__).resolve().parent / 'bin/qemu-system-aarch64'
    with tempfile.TemporaryDirectory(prefix='a7bp-', dir='/tmp') as tmp:
        qmp, gdb = [Path(tmp) / x for x in ('qmp.sock', 'gdb.sock')]
        entry_pa = plan['initial_entry_pa'] if args.boot_el == 3 and 'initial_entry_pa' in plan else plan['entry_pa']
        args_pa = plan['initial_boot_args_pa'] if args.boot_el == 3 and 'initial_boot_args_pa' in plan else plan['boot_args_pa']
        framebuffer = plan['framebuffer_pa'] if args.boot_el == 3 else plan.get('framebuffer_pa', 0x83fc00000)
        cmd = [str(binary), '-machine', f's5l8960x,boot-entry={hex(entry_pa)},boot-args={hex(args_pa)},boot-el={args.boot_el},framebuffer={framebuffer:#x}',
               '-accel', 'tcg', '-kernel', plan['image'], '-display', 'none',
               '-serial', 'file:' + str(output / 'serial.log'), '-S',
               '-qmp', f'unix:{qmp},server=on,wait=off', '-gdb', f'unix:{gdb},server=on,wait=off',
               '-d', 'guest_errors,unimp,int', '-D', str(output / 'qemu-trace.log')]
        if args.system_image:
            system = str(args.system_image.resolve()).replace(',', ',,')
            data = str(args.data_image.resolve()).replace(',', ',,')
            cmd[2] += f',system-image={system},data-image={data},system-writable={int(args.system_writable)}'
            if args.utility_image:
                cmd[2] += ',utility-image=' + str(args.utility_image.resolve()).replace(',', ',,')
        for image in plan.get('auxiliary_images', []):
            payload = Path(image['path']).read_bytes()
            if len(payload) != image['size'] or hashlib.sha256(payload).hexdigest() != image['sha256']:
                p.error('Auxiliary firmware image differs from the boot plan')
            cmd += ['-device', f"loader,file={image['path']},addr={image['address']:#x},force-raw=on"]
        (output / 'command.json').write_text(json.dumps(cmd, indent=2))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not qmp.exists() or not gdb.exists():
                assert proc.poll() is None, proc.stderr.read().decode()
                assert time.monotonic() < deadline
                time.sleep(0.01)
            qs = socket.socket(socket.AF_UNIX); qs.settimeout(30); qs.connect(str(qmp))
            gs = socket.socket(socket.AF_UNIX); gs.settimeout(args.timeout_seconds); gs.connect(str(gdb))
            with qs, gs, qs.makefile('rwb') as fp:
                json.loads(fp.readline()); command(fp, 'qmp_capabilities')
                request = ('Z2,' + args.watchpoint.removeprefix('0x') + ',8'
                           if args.watchpoint else
                           'Z0,' + args.breakpoint.removeprefix('0x') + ',4')
                assert rsp(gs, request) == 'OK'
                search_deadline = time.monotonic() + args.timeout_seconds
                try:
                    for hit in range(10000):
                        remaining = search_deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError('Breakpoint search deadline expired')
                        gs.settimeout(remaining)
                        result = rsp(gs, 'c')
                        registers = command(fp, 'human-monitor-command', {'command-line': 'info registers'})
                        if not args.condition_substring or args.condition_substring in registers:
                            break
                        remaining = search_deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError('Breakpoint search deadline expired')
                        gs.settimeout(remaining)
                        rsp(gs, 's')
                    else: raise RuntimeError('No matching breakpoint hit within bound')
                except (TimeoutError, RuntimeError) as exc:
                    # A missing hook is evidence of an unresolved wait, never a
                    # successful breakpoint. Retain the actual stopped CPU.
                    (output / 'failure.json').write_text(json.dumps(dict(
                        breakpoint_verified=False, error=type(exc).__name__,
                        message=str(exc), timeout_seconds=args.timeout_seconds), indent=2))
                    command(fp, 'stop')
                    registers = command(fp, 'human-monitor-command', {'command-line': 'info registers'})
                    (output / 'timeout-registers.txt').write_text(registers)
                    capture_hmp(fp, output, args.hmp)
                    command(fp, 'quit')
                    raise
                (output / 'gdb-stop.txt').write_text(f'hit={hit+1}\n{result}')
                (output / 'registers.txt').write_text(registers); print(registers[:1100])
                capture_hmp(fp, output, args.hmp)
                command(fp, 'quit')
        finally:
            try:
                out, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate(timeout=5)
            (output / 'stdout.log').write_bytes(out)
            (output / 'stderr.log').write_bytes(err)
            (output / 'process-exit.json').write_text(json.dumps(dict(exit_code=proc.returncode)))

if __name__ == '__main__': main()
