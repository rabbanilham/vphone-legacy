#!/usr/bin/env python3
"""Bounded real-kernel probe; writes evidence, never asserts iOS boot success."""
import argparse
import hashlib
import json
import re
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from smoke_test import command
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from qemu.runtime import PanicMonitor

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--image', type=Path, required=True)
    p.add_argument('--entry', required=True)
    p.add_argument('--boot-args', required=True)
    p.add_argument('--boot-el', type=int, choices=(1, 3), default=1)
    p.add_argument('--iop-diagnostic', action='store_true',
                   help='Enable provisional Cortex-A7 IOP execution; unsupported peripherals fail')
    p.add_argument('--framebuffer', type=lambda value: int(value, 0),
                   help='Physical scanout address; otherwise read the matching adjacent boot_plan.json')
    p.add_argument('--hmp', action='append', default=[])
    p.add_argument('--backtrace', action='store_true', help='Capture up to 16 ARM64 frame-pointer records after stopping')
    p.add_argument('--seconds', type=float, default=0.25)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    assert 0 < args.seconds <= 60, 'Probe duration must be 0..60 seconds'
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=True)
    binary = Path(__file__).resolve().parent / 'bin/qemu-system-aarch64'
    with tempfile.TemporaryDirectory(prefix='a7probe-', dir='/tmp') as tmp:
        sockpath = str(Path(tmp) / 'qmp.sock')
        framebuffer = args.framebuffer
        plan = None
        plan_path = args.image.parent / 'boot_plan.json'
        if plan_path.is_file():
            plan = json.loads(plan_path.read_text())
            if Path(plan['image']).resolve() != args.image.resolve() or plan['image_sha256'] != hashlib.sha256(args.image.read_bytes()).hexdigest():
                p.error('Boot plan does not match the supplied image')
        if framebuffer is None:
            if plan is not None:
                framebuffer = plan['framebuffer_pa']
            elif args.boot_el == 3:
                p.error('Native monitor probe requires a matching boot_plan.json or --framebuffer')
            else:
                framebuffer = 0x83fc00000  # Explicit EL1 fixture layout only.
        cmd = [str(binary), '-machine', f's5l8960x,boot-entry={args.entry},boot-args={args.boot_args},boot-el={args.boot_el},framebuffer={framebuffer:#x}',
               '-accel', 'tcg', '-smp', '1', '-m', '1024M', '-kernel', str(args.image.resolve()),
               '-display', 'none', '-serial', 'file:' + str(output / 'serial.log'),
               '-qmp', f'unix:{sockpath},server=on,wait=off', '-S',
               '-d', 'guest_errors,unimp,int', '-D', str(output / 'qemu-trace.log')]
        if args.iop_diagnostic:
            cmd[2] += ',iop-diagnostic=1'
            cmd[4] = 'tcg,thread=single'
        for image in (plan or {}).get('auxiliary_images', []):
            payload = Path(image['path']).read_bytes()
            if len(payload) != image['size'] or hashlib.sha256(payload).hexdigest() != image['sha256']:
                p.error('Auxiliary firmware image differs from the boot plan')
            cmd += ['-device', f"loader,file={image['path']},addr={image['address']:#x},force-raw=on"]
        (output / 'command.json').write_text(json.dumps(cmd, indent=2))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not Path(sockpath).exists():
                assert proc.poll() is None, proc.stderr.read().decode()
                assert time.monotonic() < deadline, 'QMP startup timeout'
                time.sleep(0.01)
            sock = socket.socket(socket.AF_UNIX); sock.settimeout(5); sock.connect(sockpath)
            with sock, sock.makefile('rwb') as fp:
                json.loads(fp.readline()); command(fp, 'qmp_capabilities')
                (output / 'registers-before.txt').write_text(command(fp, 'human-monitor-command', {'command-line':'info registers'}))
                command(fp, 'cont')
                monitor = PanicMonitor()
                deadline = time.monotonic() + args.seconds
                while time.monotonic() < deadline:
                    if monitor.poll(output / 'serial.log'):
                        break
                    time.sleep(min(0.1, max(0, deadline - time.monotonic())))
                command(fp, 'stop')
                panicked = monitor.poll(output / 'serial.log')
                registers = command(fp, 'human-monitor-command', {'command-line':'info registers'})
                (output / 'registers-after.txt').write_text(registers)
                if args.backtrace:
                    frame = int(re.search(r'X29=([0-9a-fA-F]+)', registers).group(1), 16)
                    frames = []; seen = set()
                    for n in range(16):
                        if not frame or frame in seen or frame & 7:
                            break
                        seen.add(frame)
                        dump = command(fp, 'human-monitor-command', {'command-line':f'x /2xg {hex(frame)}'})
                        words = re.findall(r'0x([0-9a-fA-F]+)', dump)
                        if len(words) != 2:
                            frames.append({'fp': hex(frame), 'unreadable': dump}); break
                        previous, lr = [int(word, 16) for word in words]
                        frames.append({'fp': hex(frame), 'previous_fp': hex(previous), 'lr': hex(lr)})
                        frame = previous
                    (output / 'backtrace.json').write_text(json.dumps(frames, indent=2) + '\n')
                for n, hmp in enumerate(args.hmp):
                    (output / f'hmp-{n}.txt').write_text(command(fp, 'human-monitor-command', {'command-line': hmp}))
                command(fp, 'screendump', {'filename':str(output / 'framebuffer.ppm')})
                command(fp, 'quit')
            out, err = proc.communicate(timeout=5)
            (output / 'stderr.log').write_bytes(err)
            assert proc.returncode == 0, err.decode()
            (output / 'observations.json').write_text(json.dumps({
                'kernel_panic': panicked, 'qemu_exit_code': proc.returncode,
                'boot_verified': False, 'scanout_physical_address': framebuffer,
                'serial_evidence': str(output / 'serial.log'),
            }, indent=2) + '\n')
        finally:
            if proc.poll() is None: proc.kill(); proc.wait(timeout=5)
    print(f'Probe evidence: {output}; no boot success is inferred.')
    if panicked:
        print('FAIL: XNU panic observed on guest UART.', file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__': main()
