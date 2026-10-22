#!/usr/bin/env python3
"""Maintainer-run EHCI MMIO contract. Builds a synthetic RAM payload only.

Run after rebuilding: python3 backend/ehci_contract_test.py.
Does not load iOS or access any guest disk.
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


def fixture():
    code = move64(4, 0x800002000)
    expected = []

    def write(address, value):
        code.extend(move64(2, address) + move64(0, value) + [0xb9000040])

    def read(address, value, mask=None):
        code.extend(move64(2, address) + [0xb9400043])
        if mask is not None:
            code.extend(move64(5, mask) + [0x0a050063])  # and w3,w3,w5
        code.append(0xb9000003 | len(expected) << 10 | 4 << 5)
        expected.append(value)

    for base, ports, control in [(0x20c200000, 3, 0x20c000000),
                                  (0x20c400000, 1, 0x20c000008)]:
        # Four exact word reads from native start eefb10..20.
        read(base, 0x01000010)  # CAPLENGTH 0x10, EHCI version 1.0
        read(base + 4, ports)
        read(base + 8, 0x80)  # QEMU engine: frame cache, 32-bit DMA
        read(base + 12, 0)
        op = base + 0x10
        read(op, 0x80000)  # reset interrupt threshold, RunStop=0
        read(op + 4, 0x1000)  # HCHalted
        for port in range(ports):
            read(op + 0x44 + 4 * port, 0x1000)  # power, CCS/CSC/PE clear
        # Holes inside QEMU's container and the entire DT reservation tail.
        for offset in (0x64, 0x2c0, 0xffc, 0x1000, 0xfffc):
            read(base + offset, 0)
            write(base + offset, 0x12345678 + ports)
            read(base + offset, 0x12345678 + ports)
        write(control + 4, 0xf)  # native cfg-incr, separate from clock word
        read(control + 4, 0xf)
        write(control, 0xf)
        read(control, 0xf)
        write(op + 8, 0x3f)  # interrupt enable alone creates no events
        write(op + 4, 0x3f)  # W1C must preserve HCHalted
        read(op + 4, 0x1000)
        write(op, 2)  # HCRESET self clears and resets operational state
        read(op, 0x80000)
        read(op + 8, 0)
        read(base + 0x2c0, 0x12345678 + ports)
        read(control + 4, 0xf)
        read(control, 0xf)  # controller reset does not reset complex config
        write(op, 1)  # Run, no schedules or attached endpoints
        read(op + 4, 0)
        write(op, 0)
        read(op + 4, 0x1000)
        for port in range(ports):
            read(op + 0x44 + 4 * port, 0x1000)
    read(0x20c000000, 0xf)
    read(0x20c000008, 0xf)
    # Real descriptor fetch AND writeback through USB's private bus view.
    # No endpoint is attached: a valid high-speed IN qTD must finish with
    # transaction-error/halted, not fetch zeros/unmapped system memory.
    for base in (0x20c200000, 0x20c400000):
        op = base + 0x10
        qh, qtd = 0x800011000, 0x800012000
        words = [0x11002, (64 << 16) | (1 << 15) | (2 << 12) | (1 << 8),
                 1 << 30, 0, 0x12000, 1, 0, 0, 0, 0, 0, 0]
        for i, word in enumerate(words):
            write(qh + 4 * i, word)
        for i, word in enumerate([1, 1, (8 << 16) | (3 << 10) | (1 << 8) | 0x80,
                                  0x13000, 0, 0, 0, 0]):
            write(qtd + 4 * i, word)
        # Initialize a 1024-entry terminated periodic frame list in AP RAM.
        code.extend(move64(2, 0x800010000) + move64(0, 1) + move64(5, 1024))
        code.extend([0xb8004440, 0x710004a5, 0x54ffffc1])  # str w0,[x2],4; subs; b.ne
        write(op + 0x14, 0x10000)  # device bus addresses, not AP physical
        write(op + 0x18, 0x11000)
        write(op, 0x31)  # run + periodic + async
        code.extend(move64(2, op + 4))
        code.extend([0xb9400043, 0x360fffe3])  # ldr; tbz w3,#1,previous
        read(qtd + 8, 0x48, 0xfc)  # inactive, halted, transaction error
        read(op + 4, 2, 2)  # USBERRINT proves engine reached descriptor
        write(op, 0)
        read(op + 4, 0x1000, 0x1000)
        write(op + 4, 0x3f)
    write(0x800002000 + len(expected) * 4, 0xface)
    expected.append(0xface)
    code.append(0x14000000)
    assert len(code) * 4 < 0x2000, 'payload overlaps result buffer'
    return struct.pack('<' + 'I' * len(code), *code), expected


def main():
    payload, expected = fixture()
    with tempfile.TemporaryDirectory(prefix='a7ehci-', dir='/tmp') as directory:
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
    print('PASS: EHCI capability header, reset, run/halt, disconnected ports, W1C, control isolation, vendor holes/tail, schedule DMA read/writeback')
    print('Synthetic MMIO contract only; no iOS or modem startup claim.')


if __name__ == '__main__': main()
