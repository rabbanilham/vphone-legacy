#!/usr/bin/env python3
"""Synthetic backend contract checks. These are NOT evidence of an iOS boot."""
import json
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import time

BACKEND = Path(__file__).resolve().parent
BINARY = BACKEND / 'bin/qemu-system-aarch64'
BASE = 0x800000000

def fixture():
    # Guest code emits an explicitly synthetic UART marker, writes one red
    # framebuffer pixel, then spins. No host-generated guest output.
    code = [0xd538f80c, 0xb27c0d8c, 0xb2780d8c, 0xd518f80c, 0xd538f80c,
            0xd2800001, 0xf2800000 | (1 << 21) | (0x0a0a << 5) | 1,
            0xf2800000 | (2 << 21) | (2 << 5) | 1]
    for c in b'A7-SYNTHETIC-UART-FIXTURE\n':
        code += [0x52800000 | (c << 5), 0x39008020]
    code += [0xd2800001, 0xf2800000 | (1 << 21) | (0x3fc0 << 5) | 1,
             0xf2800000 | (2 << 21) | (8 << 5) | 1,
             0x52800000 | (1 << 21) | (0xff << 5), 0xb9000020, 0x14000000]
    return struct.pack('<' + 'I' * len(code), *code)

def command(fp, name, arguments=None):
    fp.write((json.dumps({'execute': name, 'arguments': arguments or {}}) + '\n').encode())
    fp.flush()
    while True:
        msg = json.loads(fp.readline())
        if 'error' in msg: raise AssertionError(msg)
        if 'return' in msg: return msg['return']

def main():
    assert BINARY.exists(), 'Build backend first'
    with tempfile.TemporaryDirectory(prefix='a7-', dir='/tmp') as directory:
        tmp = Path(directory)
        image = tmp / 'fixture.bin'; image.write_bytes(fixture())
        common = [str(BINARY), '-machine', 's5l8960x,boot-entry=0x800000000,boot-args=0x800001000',
                  '-kernel', str(image), '-accel', 'tcg', '-display', 'none']
        proc = subprocess.Popen(common + ['-S', '-serial', 'file:' + str(tmp / 'serial.log'),
            '-qmp', 'unix:' + str(tmp / 'qmp.sock') + ',server=on,wait=off'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not (tmp / 'qmp.sock').exists():
                assert proc.poll() is None, proc.stderr.read().decode()
                assert time.monotonic() < deadline, 'QMP startup timed out'
                time.sleep(0.01)
            sock = socket.socket(socket.AF_UNIX); sock.settimeout(5); sock.connect(str(tmp / 'qmp.sock'))
            with sock, sock.makefile('rwb') as fp:
                greeting = json.loads(fp.readline()); assert 'QMP' in greeting
                command(fp, 'qmp_capabilities')
                registers = command(fp, 'human-monitor-command', {'command-line': 'info registers'})
                assert '800001000' in registers, registers
                assert 'EL1h' in registers, registers
                command(fp, 'cont')
                while (tmp / 'serial.log').read_bytes() != b'A7-SYNTHETIC-UART-FIXTURE\n':
                    assert time.monotonic() < deadline, 'Guest UART code failed'
                    time.sleep(0.01)
                command(fp, 'stop')
                registers = command(fp, 'human-monitor-command', {'command-line': 'info registers'})
                assert 'X12=0000000000000ff0' in registers, registers
                command(fp, 'screendump', {'filename': str(tmp / 'frame.ppm')})
                ppm = (tmp / 'frame.ppm').read_bytes()
                assert ppm.startswith(b'P6\n640 1136\n255\n'), ppm[:50]
                assert ppm.split(b'\n', 3)[3][:3] == b'\xff\x00\x00', 'Guest-written framebuffer pixel missing'
                command(fp, 'quit')
            assert proc.wait(timeout=5) == 0
        finally:
            if proc.poll() is None: proc.kill(); proc.wait(timeout=5)
        for extra, message in [(['-smp', '2'], 'CPUs'), (['-m', '512'], 'one CPU'),
                               (['-kernel', str(tmp / 'missing')], 'Could not open')]:
            result = subprocess.run(common + extra, capture_output=True, timeout=5)
            assert result.returncode != 0, extra
            assert message.lower() in result.stderr.decode().lower(), result.stderr
    print('PASS: real guest instruction execution, boot x0, UART, guest framebuffer, QMP stop/quit, invalid configuration')
    print('Synthetic fixture only; iOS kernel/userspace/UI not verified.')

if __name__ == '__main__': main()
