#!/usr/bin/env python3
"""Synthetic SPI1 PIO/HBPP contract. Requires an integrator-built QEMU.

Replays spi1-trace.txt, polls the AIC IAR for IRQ 137, drains RX as the
native transferInterrupt does, W1C-acks status, and verifies deassertion.
This tests controller transport/detection, not iOS touch registration.
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

SPI, AIC, RESULT = 0x20a084000, 0x20e100000, 0x800002000
PROBE = bytes.fromhex('1a a1' + ' 18 e1' * 7)
SYNC = bytes.fromhex('4a d1 4a d1') + bytes(12)


def fixture():
    code = move64(1, SPI) + move64(4, RESULT) + move64(7, AIC)
    code += move64(8, AIC + 0x4000)

    def store(base, offset, value):
        return move64(2, value) + [0xb9000000 | (offset // 4) << 10 | base << 5 | 2]

    def load(base, offset, reg=6):
        return [0xb9400000 | (offset // 4) << 10 | base << 5 | reg]

    def record(slot):
        return [0xb9000000 | slot << 10 | 4 << 5 | 6]

    # AIC mask-clear word 4: IRQ 137 is bit 9.
    code += store(8, 0x190, 1 << 9)
    for offset, value in [(0, 0), (0xc, 0), (8, 0x40000f), (0x30, 3),
                          (0x38, 0), (8, 0x40000f), (4, 0x4018), (0, 0xd),
                          (0x34, 16), (0x4c, 16), (4, 0x4038)]:
        code += store(1, offset, value)
    for byte in PROBE:
        code += store(1, 0x10, byte)
    # No interrupt until enabled, even though RX data already arrived.
    code += load(7, 0x2004) + record(0)
    code += store(1, 4, 0x2041b8)
    code += load(7, 0x2004)
    code += [0x34000000 | ((-1 & 0x7ffff) << 5) | 6]  # cbz w6, previous ldr
    code += record(1) + load(1, 8) + record(2)
    for i in range(16):
        code += load(1, 0x20) + record(3 + i)
    code += store(1, 8, 0x40000f)  # status W1C
    code += load(1, 8) + record(19)
    code += store(1, 4, 0x4018)  # native completion action masks enables
    code += store(8, 0x190, 1 << 9)  # IAR masks delivered IRQ: re-enable it
    code += load(7, 0x2004) + record(20)
    code += store(4, 21 * 4, 0xface) + [0x14000000]
    return struct.pack('<' + 'I' * len(code), *code)


def main():
    with tempfile.TemporaryDirectory(prefix='a7spi-', dir='/tmp') as directory:
        root = Path(directory)
        (root / 'fixture.bin').write_bytes(fixture())
        cmd = [str(BINARY), '-machine', 's5l8960x,boot-entry=0x800000000,boot-args=0x800001000',
               '-kernel', str(root / 'fixture.bin'), '-accel', 'tcg', '-display', 'none', '-S',
               '-qmp', f'unix:{root}/qmp.sock,server=on,wait=off']
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not (root / 'qmp.sock').exists():
                assert proc.poll() is None, proc.stderr.read().decode()
                assert time.monotonic() < deadline, 'QMP socket timeout'
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
                        'command-line': f'xp /22wx {RESULT:#x}'})
                    actual = [int(x, 16) for x in re.findall(r'0x([0-9a-fA-F]+)', dump)]
                    if actual and actual[-1] == 0xface:
                        break
                    assert time.monotonic() < deadline, f'guest completion timeout: {dump}'
                    time.sleep(.01)
                expected = [0, 0x10000 | 137, 0x400000 | (16 << 11)]
                expected += list(SYNC) + [0, 0, 0xface]
                assert actual == expected, (actual, expected)
                command(fp, 'quit')
            assert proc.wait(timeout=5) == 0
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
    print('PASS: SPI1 native 16-byte probe, masked IRQ, AIC IRQ 137, two HBPP tokens, FIFO drain and W1C')


if __name__ == '__main__':
    main()
