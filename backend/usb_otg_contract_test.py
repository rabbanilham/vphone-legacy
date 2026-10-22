#!/usr/bin/env python3
"""Maintainer-only detached USB MMIO contract; synthetic RAM, no iOS/disks."""
import struct
from ehci_contract_test import main as run_contract
from gpio_contract_test import move64

BASE = 0x20c100000


def transactions(endpoints=range(16)):
    """Word transactions shared with the host test of the actual board C."""
    t = []
    def read(o, v): t.append(('r', BASE + o, v))
    def write(o, v): t.append(('w', BASE + o, v))
    for o, v in [(0, 0x10000), (0x10, 0x80000000), (0x14, 0x20),
                 (0x40, 0x4f54280a), (0x44, 0x264),
                 (0xc, 0x1400),
                 (0x48, (1 << 19) | (7 << 10) | (1 << 6) | (2 << 3)),
                 (0x4c, 0x08200044), (0x50, (7 << 26) | (1 << 25) | 7),
                 (0x24, 0x400), (0x28, 0x01000400), (0x2c, 0x80100),
                 (0x800, 0), (0x804, 2), (0x808, 1), (0x818, 0), (0x20, 0)]:
        read(o, v)
    for o in (0x40, 0x44, 0x48, 0x4c, 0x50, 0, 0x808):
        write(o, 0xffffffff)
    read(0, 0x10000); read(0x808, 1); read(0x40, 0x4f54280a)
    for command in (1, 0x10, 0x420, 0x430, 0xffffffff):
        write(0x10, command); read(0x10, 0x80000000)
    # Native e6ce58 / e70978: global IN/OUT NAK effective.
    write(0x804, 0x282); read(0x804, 0xe); read(0x14, 0xe0)
    write(0x14, 0xffffffff); read(0x14, 0xe0)
    write(0x804, 0x502); read(0x804, 2); read(0x14, 0x20)
    # Native e6cf54 / e70a88: endpoint disable completed, not transfer done.
    for ep in endpoints:
        for start in (0x900, 0xb00):
            o = start + 32 * ep
            read(o, 0x20000); read(o + 8, 0)
            write(o, 0x84008040); read(o, 0x80008040)  # enable + CNAK
            write(o, 0xc8008040); read(o, 0x28040)  # disable + SNAK
            read(o + 8, 2); write(o + 8, 2); read(o + 8, 0)
        read(0x918 + 32 * ep, 0x20 if ep else 0x100)
    write(0x810, 2); write(0x814, 2); write(0x81c, 0x10001)
    write(0x900, 0x48000000); write(0xb00, 0x48000000)
    read(0x818, 0x10001); read(0x14, 0xc0020)
    write(0x908, 2); write(0xb08, 2); read(0x818, 0)
    for o in (0x54, 0xffc, 0x1000, 0xfffc):
        read(o, 0); write(o, 0x12345678); read(o, 0x12345678)
    write(0x24, 0x123); write(0x10, 1)
    read(0x24, 0x400); read(0xfffc, 0); read(0x14, 0x20)
    return t


def fixture():
    code = move64(4, 0x800002000)
    expected = []
    actions = transactions((0, 1, 15))
    # Every additional DT range: independent storage, final word inclusive.
    for base, size in [(0x20c000010, 0x10), (0x20c000050, 0x10),
                       (0x20c000060, 0x10), (0x20c000020, 0x20),
                       (0x20e031000, 0x1000),
                       (0x20c300000 + 0x1000, 0xf000)]:
        for offset in (0, size - 4):
            actions += [('r', base + offset, 0), ('w', base + offset, 0xaabbccdd),
                        ('r', base + offset, 0xaabbccdd)]
    actions += [('r', 0x20c300000, 0x10), ('r', 0x20c300004, 0),
                ('r', 0x20c300048, 0x201),
                ('r', 0x20c300054, 0x100), ('w', 0x20c300008, 1),
                ('r', 0x20c300008, 0), ('r', 0x20c300054, 0x100)]
    for op, address, value in actions:
        code.extend(move64(2, address))
        if op == 'w':
            code.extend(move64(0, value) + [0xb9000040])
        else:
            code.append(0xb9400043)
            code.append(0xb9000003 | len(expected) << 10 | 4 << 5)
            expected.append(value)
    code.extend(move64(2, 0x800002000 + len(expected) * 4) +
                move64(0, 0xface) + [0xb9000040, 0x14000000])
    expected.append(0xface)
    assert len(code) * 4 < 0x2000, 'payload overlaps result buffer'
    return struct.pack('<' + 'I' * len(code), *code), expected


if __name__ == '__main__':
    run_contract(fixture, 'PASS: detached OTG reset/FIFO/NAK/endpoint polls, HSIC/control storage and OHCI idle/reset')
