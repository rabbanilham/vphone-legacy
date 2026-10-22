#!/usr/bin/env python3
"""DWI (backlight wire interface) transaction contract; run after rebuilding QEMU.

No iOS or disk image required. Each channel control register (0x10, 0x20,
0x40) must drop GO (bit 0) once written, as AppleS5L8940DWI::_waitTransaction
polls for, while keeping the other control bits and the data words.
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

BASE = 0x800000000
DWI = 0x20e200000
OUTPUT = BASE + 0x2000


def fixture():
    code = move64(4, OUTPUT)
    expected = []

    def write(address, value):
        code.extend(move64(2, address) + move64(0, value) + [0xb9000040])

    def read(address, value):
        code.extend(move64(2, address) +
                    [0xb9400043, 0xb9000083 | len(expected) << 10])
        expected.append(value)

    for ctl, word in ((0x10, 0x41), (0x20, 0x71), (0x40, 0x71)):
        write(DWI + ctl + 4, 0x1cb)
        write(DWI + ctl, word)
        read(DWI + ctl, word & ~1)
        read(DWI + ctl + 4, 0x1cb)
    write(DWI + 0x50, 0x3)  # ordinary configuration storage is unchanged
    read(DWI + 0x50, 0x3)
    write(OUTPUT + len(expected) * 4, 0xface)
    expected.append(0xface)
    code.append(0x14000000)
    return struct.pack('<' + 'I' * len(code), *code), expected


def main():
    payload, expected = fixture()
    with tempfile.TemporaryDirectory(prefix='a7dwi-', dir='/tmp') as directory:
        root = Path(directory)
        image = root / 'fixture.bin'
        image.write_bytes(payload)
        proc = subprocess.Popen([
            str(BINARY), '-machine',
            f's5l8960x,boot-entry={BASE:#x},boot-args={BASE + 0x1000:#x}',
            '-kernel', str(image), '-accel', 'tcg', '-display', 'none', '-S',
            '-qmp', f'unix:{root}/qmp.sock,server=on,wait=off'
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not (root / 'qmp.sock').exists():
                assert proc.poll() is None, proc.stderr.read().decode()
                assert time.monotonic() < deadline
                time.sleep(.01)
            sock = socket.socket(socket.AF_UNIX)
            sock.settimeout(5)
            sock.connect(str(root / 'qmp.sock'))
            with sock, sock.makefile('rwb') as fp:
                json.loads(fp.readline())
                command(fp, 'qmp_capabilities')
                command(fp, 'cont')
                while True:
                    dump = command(fp, 'human-monitor-command', {
                        'command-line': f'xp /{len(expected)}wx {OUTPUT:#x}'})
                    actual = [int(x, 16) for x in re.findall(r'0x([0-9a-fA-F]+)', dump)]
                    if actual and actual[-1] == 0xface:
                        break
                    assert time.monotonic() < deadline, dump
                    time.sleep(.01)
                assert actual == expected, (actual, expected)
                command(fp, 'quit')
            assert proc.wait(timeout=5) == 0
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
    print('PASS: DWI channel GO bits self-clear; data and configuration words retained')


if __name__ == '__main__':
    main()
