#!/usr/bin/env python3
"""Synthetic Samsung DSIM status checks; not evidence of an iOS boot.

STATUS (0x206400000) must follow the control registers polled by
AppleSamsungMIPIDSIController::_waitStatus (0xffffff8000e91d20).
"""
import json
from pathlib import Path
import re
import socket
import struct
import subprocess
import tempfile
import time
from smoke_test import BINARY, command
from gpio_contract_test import move64

DSIM, RESULT = 0x206400000, 0x800002000


def fixture():
    code = move64(1, DSIM) + move64(4, RESULT)
    expected = []
    def store(offset, value):
        return move64(2, value) + [0xb9000000 | (offset // 4) << 10 | 1 << 5 | 2]
    def sample(value):
        code.extend([0xb9400023, 0xb9000003 | len(expected) << 10 | 4 << 5])  # ldr w3,[x1]; str w3,[x4,#n]
        expected.append(value)
    sample((1 << 20) | 0x10f)                           # reset: stop states, reset released
    code.extend(store(0x04, 1)); sample((1 << 20) | 0x10f)  # SWRST completes immediately
    code.extend(store(0x4c, 1 << 23)); sample((1 << 31) | (1 << 20) | 0x10f)  # PLL_EN
    code.extend(store(0x14, 0x8a)); sample((1 << 31) | (1 << 20) | 0x2f0)     # ULPS entry
    code.extend(store(0x14, 0x8f)); sample((1 << 31) | (1 << 20) | 0x10f)     # ULPS exit
    code.extend(store(0x08, 0x80000000)); sample((1 << 31) | (1 << 20) | 0x50f)  # HS clock
    code.extend(store(0x08, 0)); sample((1 << 31) | (1 << 20) | 0x10f)
    code.extend(move64(2, 0xface) + [0xb9000002 | len(expected) << 10 | 4 << 5, 0x14000000])
    expected.append(0xface)
    return struct.pack('<' + 'I' * len(code), *code), expected


def main():
    payload, expected = fixture()
    with tempfile.TemporaryDirectory(prefix='a7dsim-', dir='/tmp') as directory:
        root = Path(directory)
        (root / 'fixture.bin').write_bytes(payload)
        cmd = [str(BINARY), '-machine', 's5l8960x,boot-entry=0x800000000,boot-args=0x800001000',
               '-kernel', str(root / 'fixture.bin'), '-accel', 'tcg', '-display', 'none', '-S',
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
                    dump = command(fp, 'human-monitor-command', {'command-line': f'xp /{len(expected)}wx {RESULT:#x}'})
                    actual = [int(x, 16) for x in re.findall(r'0x([0-9a-fA-F]+)', dump)[-len(expected):]]
                    if actual and actual[-1] == 0xface:
                        break
                    assert time.monotonic() < deadline, dump
                    time.sleep(.01)
                assert actual == expected, ([hex(v) for v in actual], [hex(v) for v in expected])
                command(fp, 'quit')
            assert proc.wait(timeout=5) == 0
        finally:
            if proc.poll() is None: proc.kill(); proc.wait(timeout=5)
    print('PASS: DSIM status follows SWRST, PLL_EN, ULPS entry/exit and HS clock request')
    print('Synthetic DSIM contract only; no panel transfer or iOS display inferred.')


if __name__ == '__main__': main()
