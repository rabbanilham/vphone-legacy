#!/usr/bin/env python3
"""Native SIO/SPI1 DMA guest contract; run after the integrator rebuilds QEMU.

No iOS or disk image required. Exercises the probe-80 TX-only 0xfb58 path,
split physical segments, deferred completion, full duplex and RX-only DMA,
SPI IRQ masking/W1C, cancellation, and RAM-only descriptor validation.
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
from sio_contract_test import BASE, SIO, AIC, msg
from gpio_contract_test import move64

SPI = 0x20a084000
TABLE, TX, RX, OUTPUT = (BASE + n for n in (0x10000, 0x11000, 0x30000, 0x40000))


def fixture():
    code = move64(4, OUTPUT)
    expected = []

    def write(address, value, wide=False):
        code.extend(move64(2, address) + move64(0, value) +
                    [0xf9000040 if wide else 0xb9000040])

    def read(address, value, wide=False):
        code.extend(move64(2, address) +
                    [0xf9400043 if wide else 0xb9400043,
                     0xf9000003 | len(expected) << 10 | 4 << 5])
        expected.append(value)

    def send(ep, tag, op, param=0, data=0):
        write(SIO + 0x1010, msg(ep, tag, op, param, data), True)

    def reply(ep, tag, op, param=0, data=0):
        read(SIO + 0x1038, msg(ep, tag, op, param, data), True)

    def segments(handle, entries):
        write(TABLE + handle * 12 + 20, len(entries))
        for i, (address, length) in enumerate(entries):
            # 12-byte stride: odd entries are only 4-aligned, and an
            # unaligned 64-bit store faults with the MMU off.
            write(TABLE + (handle + 2 + i) * 12, address & 0xffffffff)
            write(TABLE + (handle + 2 + i) * 12 + 4, address >> 32)
            write(TABLE + (handle + 2 + i) * 12 + 8, length)

    send(0, 1, 3, 1, TABLE >> 12)
    reply(0, 1, 0x65, 1)
    send(0, 2, 3, 2, 32)
    reply(0, 2, 0x65, 2)
    for ep in (22, 23):
        send(ep, 129, 5)
        reply(ep, 129, 0x65)
    segments(0, [(TX, 7), (TX + 7, 0xfb58 - 7)])
    send(22, 130, 6)
    read(SIO + 0x1020, 0x20000)  # no ACK or premature completion
    send(22, 131, 7)
    reply(22, 131, 0x69, 130)
    write(SPI, 0xd)
    write(SPI + 0x34, 0)
    write(SPI + 0x4c, 0xfb58)
    write(SPI + 4, 0x4058)
    read(SPI + 8, 0x400000)
    read(AIC + 0x4210, 0)  # baseline DMA mode has no SPI IRQ enable
    reply(22, 130, 0x68, 0xc0, 0xfb58)
    write(SPI + 4, 0x4018)
    # Upload receipt via unchanged byte-at-a-time PIO path.
    write(SPI + 0x34, 2)
    write(SPI + 0x4c, 2)
    write(SPI + 4, 0x4038)
    write(SPI + 0x10, 0x1a)
    write(SPI + 0x10, 0xa1)
    read(SPI + 0x20, 0x4b)
    read(SPI + 0x20, 0xc1)
    write(SPI + 4, 0x4018)
    # Full duplex E2: separate TX and RX descriptors, each split unaligned.
    for offset, value in ((0, 0xe2), (8, 0)):
        write(TX + offset, value, True)
    segments(4, [(TX, 3), (TX + 3, 13)])
    segments(8, [(RX, 5), (RX + 5, 11)])
    send(23, 132, 6, data=8)  # native RX is armed before TX
    send(22, 133, 6, data=4)
    write(SPI + 0x34, 16)
    write(SPI + 0x4c, 16)
    write(SPI + 4, 0x204058)
    read(SPI + 8, 0x400000)
    read(AIC + 0x4210, 1 << 9)
    reply(22, 133, 0x68, 0xc0, 16)
    reply(23, 132, 0x68, 0xc0, 16)
    read(RX, int.from_bytes(bytes.fromhex('e2 00 11 00 05 00 00 00'), 'little'), True)
    read(RX + 8, 0xf8 << 48, True)
    write(SPI + 8, 0x40000f)
    read(AIC + 0x4210, 0)
    write(SPI + 4, 0x4018)
    # RX-only clocks 0xff dummy TX and scatters the disconnected response.
    send(23, 134, 6, data=8)
    write(SPI + 0x34, 16)
    write(SPI + 0x4c, 16)
    write(SPI + 4, 0x4058)
    reply(23, 134, 0x68, 0xc0, 16)
    read(RX, 0xffffffffffffffff, True)
    write(SPI + 4, 0x4018)
    # Cancel armed work before mode enable; original request receives a
    # short completion and stop receives ACK, then idle flag on completion.
    send(22, 135, 6, data=4)
    send(22, 136, 8)
    reply(22, 136, 0x65)
    reply(22, 135, 0x68, 0xc0)
    # Reject the entire list when a later segment targets SPI MMIO.
    segments(4, [(TX, 8), (SPI, 8)])
    send(22, 137, 6, data=4)
    reply(22, 137, 0x66)
    write(OUTPUT + len(expected) * 8, 0xface, True)
    expected.append(0xface)
    code.append(0x14000000)
    payload = struct.pack('<' + 'I' * len(code), *code)
    assert len(payload) < 0x10000
    payload = payload.ljust(TX - BASE, b'\0') + bytes(i & 255 for i in range(0xfb58))
    return payload, expected


def main():
    payload, expected = fixture()
    with tempfile.TemporaryDirectory(prefix='a7sio-', dir='/tmp') as directory:
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
                        'command-line': f'xp /{len(expected)}gx {OUTPUT:#x}'
                    })
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
    print('PASS: SPI1 TX/RX DMA, segmented RAM, HBPP upload receipt, completions and IRQ')


if __name__ == '__main__':
    main()
