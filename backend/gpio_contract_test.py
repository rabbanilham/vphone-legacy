#!/usr/bin/env python3
"""Synthetic native GPIO ABI checks; not evidence of an iOS boot."""
import json
from pathlib import Path
import re
import socket
import struct
import subprocess
import tempfile
import time
from smoke_test import BINARY, command


def move64(register, value):
    return [(0xd2800000 if half == 0 else 0xf2800000) |
            (half << 21) | (((value >> (half * 16)) & 0xffff) << 5) | register
            for half in range(4)]


def fixture():
    code = move64(2, 0x20e300140) + move64(4, 0x800002000)
    expected = []
    for pin in range(4):
        # Initially released; output low/high; release; native I2C mux.
        for value, result in [(None, 1), (2, 2), (3, 3), (0x200, 0x201), (0x220, 0x221)]:
            if value is not None:
                code += [0x52800000 | value << 5,
                         0xb9000000 | pin << 10 | 2 << 5]
            code += [0xb9400003 | pin << 10 | 2 << 5,
                     0xb9000003 | len(expected) << 10 | 4 << 5]
            expected.append(result)
    code += move64(5, 0x20e300138)
    code += [0xb94000a3, 0xb9000003 | len(expected) << 10 | 4 << 5]
    expected.append(0)  # Adjacent unrelated pin is not forced high.
    code += [0x52800000 | 0xface << 5,
             0xb9000000 | len(expected) << 10 | 4 << 5, 0x14000000]
    expected.append(0xface)
    return struct.pack('<' + 'I' * len(code), *code), expected


def main():
    payload, expected = fixture()
    with tempfile.TemporaryDirectory(prefix='a7gpio-', dir='/tmp') as directory:
        root = Path(directory)
        image = root / 'fixture.bin'; image.write_bytes(payload)
        cmd = [str(BINARY), '-machine', 's5l8960x,boot-entry=0x800000000,boot-args=0x800001000',
               '-kernel', str(image), '-accel', 'tcg', '-display', 'none', '-S',
               '-qmp', f'unix:{root}/qmp.sock,server=on,wait=off']
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not (root / 'qmp.sock').exists():
                assert proc.poll() is None, proc.stderr.read().decode()
                assert time.monotonic() < deadline
                time.sleep(.01)
            sock = socket.socket(socket.AF_UNIX); sock.settimeout(5); sock.connect(str(root / 'qmp.sock'))
            with sock, sock.makefile('rwb') as fp:
                json.loads(fp.readline()); command(fp, 'qmp_capabilities'); command(fp, 'cont')
                while True:
                    dump = command(fp, 'human-monitor-command', {
                        'command-line': f'xp /{len(expected)}wx 0x800002000'})
                    actual = [int(x, 16) for x in re.findall(r'0x([0-9a-fA-F]+)', dump)]
                    if actual and actual[-1] == 0xface:
                        break
                    assert time.monotonic() < deadline, dump
                    time.sleep(.01)
                assert actual == expected, (actual, expected)
                command(fp, 'quit')
            assert proc.wait(timeout=5) == 0
        finally:
            if proc.poll() is None: proc.kill(); proc.wait(timeout=5)
    print('PASS: guest observes four released GPIO pull-ups, low/high drive, I2C mux, and unchanged adjacent pin')
    print('Synthetic GPIO contract only; no PMU transaction or iOS boot success inferred.')


if __name__ == '__main__': main()
