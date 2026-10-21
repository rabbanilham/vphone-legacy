#!/usr/bin/env python3
"""Synthetic native GPIOIC + HBPP EB input contract; requires rebuilt QEMU.

No iOS/disk. Completes firmware boot, sends deferred unlock 70=2, drains
reset-complete, enables BF detection (or uses --force diagnostically), waits
for a virtual-clock scripted start and fetches it through SPI1 PIO.
"""
import argparse
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import tempfile
import time
from smoke_test import BINARY, command
from gpio_contract_test import move64

SPI, GPIO, AIC, RESULT = 0x20a084000, 0x20e300000, 0x20e100000, 0x800020000


def fixture(force=False):
    code = move64(4, RESULT)
    expected, packets = [], []

    def write(address, value):
        code.extend(move64(2, address) + move64(0, value) + [0xb9000040])

    def record(address, value):
        code.extend(move64(2, address) + [0xb9400043,
                    0xb9000003 | len(expected) << 10 | 4 << 5])
        expected.append(value)

    def transfer(tx, result):
        start = len(expected)
        write(SPI + 4, 0x4018)
        write(SPI + 0x34, len(tx)); write(SPI + 0x4c, len(tx))
        write(SPI + 4, 0x4038)
        for byte, want in zip(tx, result):
            write(SPI + 0x10, byte)
            record(SPI + 0x20, want)
        write(SPI + 4, 0x4018)
        return start

    def query(length):
        r = bytearray(16); r[0] = 0xeb
        struct.pack_into('<H', r, 1, length)
        struct.pack_into('<H', r, 14, sum(r[:14]))
        transfer(bytes([0xeb, 1]) + bytes(14), r)

    write(SPI, 0xd)
    write(AIC + 0x418c, 1 << 12)  # mask-clear word3, IRQ108
    # Minimal valid framed image plus final execute exercises the modeled
    # firmware lifecycle; no iOS or guest disk is used by this fixture.
    image = bytearray([0x18, 0xe1, 0x30, 1, 0, 1, 0, 0, 0x10, 0, 0, 0]) + bytearray(8)
    struct.pack_into('>H', image, 10, sum(image[4:10]))
    transfer(image, [None] * len(image))
    execute = bytearray([0x1d, 0x53, 0x34, 0, 0x10, 0, 0, 1, 0, 0, 0, 0])
    struct.pack_into('>H', execute, 10, sum(execute[2:10]))
    transfer(execute, [None] * len(execute))
    stop = bytearray([0xe4, 0x70, 1, 2]) + bytearray(12)
    struct.pack_into('<H', stop, 14, sum(stop[:14]))
    r = bytearray(16); r[0] = 0xe4; r[14] = 0xe4
    # iOS enables falling-edge type 0xa long before the line asserts; the
    # edge is latched once, so W1C clears it while the line stays low.
    write(GPIO + 88, 0x20a)
    record(GPIO + 0x800, 0)
    transfer(stop, r)  # 70=2 announces reset-complete
    record(GPIO + 88, 0x20a)  # active low sample
    record(GPIO + 0x800, 1 << 22)
    record(AIC + 0x2004, 0x1006c)
    # Native event source disables type before workloop SPI reads.
    write(GPIO + 88, 0x20e); write(GPIO + 0x800, 1 << 22)
    record(GPIO + 0x800, 0)
    query(4); query(4)  # duplicate query must not pop reset-complete
    reset = [0xeb, 4, 4, 0, 13, 0x60, 2, 0x62, 0]
    fetch = bytearray([0xeb, 1, 1]) + bytearray(6)
    struct.pack_into('<H', fetch, 7, sum(fetch[:3]))
    transfer(fetch, reset)
    record(GPIO + 88, 0x20f)  # line released after consume
    write(GPIO + 88, 0x20a)
    write(AIC + 0x418c, 1 << 12)
    # D9 extended surface bounds must be available over the same SPI path
    # used at startup, before the normalized MTContact can reach HID.
    r = bytearray(16); r[0] = 0xe3; r[3] = 16
    struct.pack_into('<H', r, 14, sum(r[:14]))
    transfer(bytes([0xe3, 0xd9]) + bytes(14), r)
    surface = struct.pack('<IIHHHH', 5100, 9100, 0, 0, 640, 1136)
    r = bytearray(21); r[:3] = bytes([0xe6, 0xd9, 1]); r[3:19] = surface
    struct.pack_into('<H', r, 19, sum(r[:19]))
    transfer(bytes([0xe6, 0xd9, 1, 16]) + bytes(17), r)
    if not force:
        r = bytearray(16); r[0] = 0xe3; r[3] = 4; r[14] = 0xe7
        transfer(bytes([0xe3, 0xbf]) + bytes(14), r)
        r = bytearray(16); r[:3] = bytes([0xe6, 0xbf, 1])
        struct.pack_into('<H', r, 14, sum(r[:14]))
        transfer(bytes([0xe6, 0xbf, 1, 4]) + bytes(12), r)
        control = bytearray([0xe4, 0xbf, 4, 0x9d, 1, 9, 0]) + bytearray(9)
        struct.pack_into('<H', control, 14, sum(control[:14]))
        r = bytearray(16); r[0] = 0xe4; r[14] = 0xe4
        transfer(control, r)
    # Wait for timer to produce down, without injecting a guest-owned IRQ.
    code.extend(move64(2, GPIO + 0x800))
    code.extend([0xb9400043, 0x34ffffe3])  # ldr w3,[x2]; cbz w3,-4
    record(AIC + 0x2004, 0x1006c)
    write(GPIO + 88, 0x20e); write(GPIO + 0x800, 1 << 22)
    query(58)
    fetch = bytearray([0xeb, 1, 1]) + bytearray(60)
    struct.pack_into('<H', fetch, 61, sum(fetch[:3]))
    at = transfer(fetch, [None] * 63)
    packets.append((at, 63))
    record(GPIO + 88, 0x20f)
    record(GPIO + 0x800, 0)
    write(AIC + 0x418c, 1 << 12)
    record(AIC + 0x2004, 0)
    write(RESULT + len(expected) * 4, 0xface)
    expected.append(0xface); code.append(0x14000000)
    assert len(code) * 4 < 0x20000
    return struct.pack('<' + 'I' * len(code), *code), expected, packets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--force', action='store_true', help='diagnostic injection after 70=2 without BF enable')
    args = parser.parse_args()
    payload, expected, packets = fixture(args.force)
    with tempfile.TemporaryDirectory(prefix='a7touch-', dir='/tmp') as directory:
        root = Path(directory); (root / 'fixture.bin').write_bytes(payload)
        proc = subprocess.Popen([
            str(BINARY), '-machine', 's5l8960x,boot-entry=0x800000000,boot-args=0x800001000',
            '-kernel', str(root / 'fixture.bin'), '-accel', 'tcg', '-display', 'none', '-S',
            '-qmp', f'unix:{root}/qmp.sock,server=on,wait=off'],
            env={**os.environ, 'VPHONE_TOUCH_SCRIPT': '0.05,60,1018,600,1018,0.3',
                 'VPHONE_TOUCH_FORCE': '1' if args.force else '0'},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 15
            while not (root / 'qmp.sock').exists():
                assert proc.poll() is None, proc.stderr.read().decode()
                assert time.monotonic() < deadline
                time.sleep(.01)
            sock = socket.socket(socket.AF_UNIX); sock.settimeout(5)
            sock.connect(str(root / 'qmp.sock'))
            with sock, sock.makefile('rwb') as fp:
                json.loads(fp.readline()); command(fp, 'qmp_capabilities'); command(fp, 'cont')
                while True:
                    dump = command(fp, 'human-monitor-command', {
                        'command-line': f'xp /{len(expected)}wx {RESULT:#x}'})
                    actual = [int(x, 16) for x in re.findall(r'0x([0-9a-fA-F]+)', dump)]
                    if actual and actual[-1] == 0xface: break
                    assert time.monotonic() < deadline, dump
                    time.sleep(.01)
                assert len(actual) == len(expected), (len(actual), len(expected))
                for i, (got, want) in enumerate(zip(actual, expected)):
                    assert want is None or got == want, (i, hex(got), want)
                for at, length in packets:
                    r = bytes(actual[at:at + length]); f = r[5:-2]
                    assert r[0] == 0xeb and sum(r[:5]) % 256 == 0, r.hex()
                    assert int.from_bytes(r[2:4], 'little') == 58
                    assert int.from_bytes(r[-2:], 'little') == sum(f), r.hex()
                    assert f[:4] == bytes([0x74, 0, 26, 4])
                    assert f[14] == 1 and int.from_bytes(f[12:14], 'little') == 30
                    assert f[26:30] == bytes([1, 1, 2, 1])
                    assert struct.unpack_from('<HH', f, 30) == (60, 118)
                    assert struct.unpack_from('<HH', f, 38) == (400, 300)
                    assert struct.unpack_from('<HHHHH', f, 44) == (256,) * 5
                command(fp, 'quit')
            assert proc.wait(timeout=5) == 0
        finally:
            if proc.poll() is None: proc.kill(); proc.wait(timeout=5)
    print('PASS: masked GPIO22, group0/AIC108, W1C, EB queries, reset report and scripted contact')


if __name__ == '__main__': main()
