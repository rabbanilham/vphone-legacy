#!/usr/bin/env python3
"""Synthetic guest checks for the S5L8960X SIO SmartIO mailbox/AES/DMA model."""
import json
from pathlib import Path
import re
import socket
import struct
import subprocess
import tempfile
import time
from gpio_contract_test import move64
from smoke_test import BINARY, command

BASE = 0x800000000
OP_BUF = BASE + 0x100000
DMA_BUF = BASE + 0x104000
SRC_BUF = BASE + 0x108000
DST_BUF = BASE + 0x109000
RT_BUF = BASE + 0x10a000
OUTPUT = BASE + 0x200000
SIO = 0x20ae00000
AIC = 0x20e100000


def msg(ep, tag, op, param=0, data=0):
    return (ep & 0xff) | ((tag & 0xff) << 8) | ((op & 0xff) << 16) | ((param & 0xff) << 24) | ((data & 0xffffffff) << 32)


def fixture():
    code = move64(4, OUTPUT)
    expected = []

    def write32(address, value):
        code.extend(move64(2, address) + move64(0, value) + [0xb9000040])

    def write64(address, value):
        code.extend(move64(2, address) + move64(0, value) + [0xf9000040])

    def read32(address, value):
        code.extend(move64(2, address) + [0xb9400043,
                    0xf9000003 | len(expected) << 10 | 4 << 5])
        expected.append(value)

    def read64(address, value):
        code.extend(move64(2, address) + [0xf9400043,
                    0xf9000003 | len(expected) << 10 | 4 << 5])
        expected.append(value)

    # 1. Before CPU start, outbox is empty (bit 17) and IRQ 135 (bank 4, bit 7) is low.
    read32(SIO + 0x1020, 0x20000)
    read32(AIC + 0x4210, 0)

    # 2. AppleA7IOP::startCPU enables outbox control (0x1020=1) and releases reset (0x28=0x10).
    # Reading 0x1020 observes non-empty status (0x1) and arms IRQ 135 (0x10087).
    write32(SIO + 0x1020, 1)
    write32(SIO + 0x0028, 0x10)
    read32(SIO + 0x1020, 0x1)
    read32(AIC + 0x4210, 0x80)
    write32(AIC + 0x4190, 0x80)
    read32(AIC + 0x2004, 0x10087)
    read64(SIO + 0x1038, msg(0, 0, 0x6d, 1, 0))
    read32(SIO + 0x1020, 0x20001)
    read32(AIC + 0x4210, 0)

    # 3. EP=0 (AppleSmartIOControl) setParam/getParam for DMA segments (1,2) and AES op buffer (3,4).
    write64(SIO + 0x1010, msg(0, 1, 3, 1, DMA_BUF >> 12))
    read64(SIO + 0x1038, msg(0, 1, 0x65, 1, 0))
    write64(SIO + 0x1010, msg(0, 2, 3, 2, 16))
    read64(SIO + 0x1038, msg(0, 2, 0x65, 2, 0))
    write64(SIO + 0x1010, msg(0, 3, 3, 3, OP_BUF >> 12))
    read64(SIO + 0x1038, msg(0, 3, 0x65, 3, 0))
    write64(SIO + 0x1010, msg(0, 4, 3, 4, 8))
    read64(SIO + 0x1038, msg(0, 4, 0x65, 4, 0))
    write64(SIO + 0x1010, msg(0, 5, 2, 3, 0))
    read64(SIO + 0x1038, msg(0, 5, 0x67, 3, OP_BUF >> 12))

    # 4. EP=1 (AppleSmartIOAES) inline operation (ctrl bit 10 = 0x400) with UID key (0x1e).
    write64(SIO + 0x1010, msg(1, 0x10, 0x0d, 0x1e, 0))
    read64(SIO + 0x1038, msg(1, 0x10, 0x65, 0x1e, 0))

    pt0, pt1 = 0x0123456789abcdef, 0xfedcba9876543210
    write32(OP_BUF + 0x00, 0x712)   # inline + CBC + encrypt
    write32(OP_BUF + 0x04, 0x1e)    # UID key
    write64(OP_BUF + 0x28, 0)       # zero IV
    write64(OP_BUF + 0x30, 0)
    write32(OP_BUF + 0x38, 1)       # 1 block (16 bytes)
    write64(OP_BUF + 0x100, pt0)
    write64(OP_BUF + 0x108, pt1)
    write64(SIO + 0x1010, msg(1, 0x11, 0x0b, 0, 0))
    read64(SIO + 0x1038, msg(1, 0x11, 0x65, 0, 0))

    # Inline decrypt in-place back to original plaintext.
    write32(OP_BUF + 0x00, 0x711)   # inline + CBC + decrypt
    write64(OP_BUF + 0x28, 0)
    write64(OP_BUF + 0x30, 0)
    write64(SIO + 0x1010, msg(1, 0x12, 0x0b, 0, 0))
    read64(SIO + 0x1038, msg(1, 0x12, 0x65, 0, 0))
    read64(OP_BUF + 0x100, pt0)
    read64(OP_BUF + 0x108, pt1)

    # 5. EP=1 DMA-segment AES-128-CBC NIST SP 800-38A F.2.1 test vector in slot 1.
    # Key: 2b7e151628aed2a6abf7158809cf4f3c
    # IV:  000102030405060708090a0b0c0d0e0f
    # PT:  6bc1bee22e409f96e93d7e117393172a
    # CT:  7649abac8119b246cee98e9b12e9197d
    slot1 = OP_BUF + 0x200
    write32(slot1 + 0x00, 0x312)    # DMA-segment (bit 10 clear) + CBC + encrypt
    write32(slot1 + 0x04, 1)        # 128-bit user key
    write64(slot1 + 0x08, 0xa6d2ae2816157e2b)
    write64(slot1 + 0x10, 0x3c4fcf098815f7ab)
    write64(slot1 + 0x28, 0x0706050403020100)
    write64(slot1 + 0x30, 0x0f0e0d0c0b0a0908)
    write32(slot1 + 0x38, 1)
    write32(slot1 + 0x178, SRC_BUF & 0xffffffff)
    write32(slot1 + 0x17c, 1)
    write32(slot1 + 0x180, DST_BUF & 0xffffffff)
    write32(slot1 + 0x184, 1)

    write64(SRC_BUF + 0x0, 0x969f402ee2bec16b)
    write64(SRC_BUF + 0x8, 0x2a179373117e3de9)

    write64(SIO + 0x1010, msg(1, 0x13, 0x0b, 0, 1))
    read64(SIO + 0x1038, msg(1, 0x13, 0x65, 0, 0))
    read64(DST_BUF + 0x0, 0x46b21981acab4976)
    read64(DST_BUF + 0x8, 0x7d19e9129b8ee9ce)

    # Decrypt via DMA segments from DST_BUF to RT_BUF.
    write32(slot1 + 0x00, 0x311)    # DMA-segment + CBC + decrypt
    write64(slot1 + 0x28, 0x0706050403020100)
    write64(slot1 + 0x30, 0x0f0e0d0c0b0a0908)
    write32(slot1 + 0x178, DST_BUF & 0xffffffff)
    write32(slot1 + 0x17c, 1)
    write32(slot1 + 0x180, RT_BUF & 0xffffffff)
    write32(slot1 + 0x184, 1)
    write64(SIO + 0x1010, msg(1, 0x14, 0x0b, 0, 1))
    read64(SIO + 0x1038, msg(1, 0x14, 0x65, 0, 0))
    read64(RT_BUF + 0x0, 0x969f402ee2bec16b)
    read64(RT_BUF + 0x8, 0x2a179373117e3de9)

    # 6. disableHardwareKey (op=0x0c) disables UID key (0x1e) and blocks subsequent UID operations.
    write64(SIO + 0x1010, msg(1, 0x20, 0x0c, 0x1e, 0))
    read64(SIO + 0x1038, msg(1, 0x20, 0x65, 0x1e, 0))
    write64(SIO + 0x1010, msg(1, 0x21, 0x0d, 0x1e, 0))
    read64(SIO + 0x1038, msg(1, 0x21, 0x66, 0x1e, 0))
    write64(SIO + 0x1010, msg(1, 0x22, 0x0b, 0, 0))
    read64(SIO + 0x1038, msg(1, 0x22, 0x66, 0, 0))

    write64(OUTPUT + len(expected) * 8, 0xface)
    expected.append(0xface)
    code.append(0x14000000)
    return struct.pack('<' + 'I' * len(code), *code), expected


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
    print('PASS: guest SIO startCPU/OS-active, IRQ 135, EP0 params, EP1 inline/DMA AES-CBC, and hardware key disable')


if __name__ == '__main__':
    main()
