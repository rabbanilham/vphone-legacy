#!/usr/bin/env python3
"""Synthetic guest checks for native I2C NACK/event ABI; no slave or iOS claim."""
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


def fixture():
    code = move64(4, 0x800002000)
    expected = []

    def write(address, value):
        code.extend(move64(2, address) + move64(0, value) + [0xb9000040])

    def read(address, value):
        code.extend(move64(2, address) + [0xb9400043,
                    0xb9000003 | len(expected) << 10 | 4 << 5])
        expected.append(value)

    bus = 0x20a110000
    aic = 0x20e100000
    read(bus + 4, 0x100)
    read(bus + 0x14, 0)
    # Preserve an unrelated software interrupt across peripheral updates/reset.
    write(aic + 0x4014, 1)
    write(aic + 0x4114, 1)  # Mask IRQ160 for deterministic event selection.
    write(bus, 0x1e8)  # Native PMU write address: unmodeled, never ACK.
    read(bus + 0x14, 0x08200000)
    read(aic + 0x4210, 0)  # Controller IRQ disabled.
    write(bus + 0x18, 0x08800040)
    read(aic + 0x4210, 0x04000000)
    write(aic + 0x4190, 0x04000000)  # AIC resets with external lines masked.
    read(aic + 0x2004, 0x1009a)  # Real AIC event and auto-mask on claim.
    read(aic + 0x2004, 0)
    read(aic + 0x4210, 0x04000000)  # Claim is not device acknowledgement.
    write(aic + 0x4090, 0x04000000)  # Software clear cannot clear device IRQ.
    read(aic + 0x4210, 0x04000000)
    write(aic + 0x4190, 0x04000000)  # Reenable asserted line.
    read(aic + 0x2004, 0x1009a)
    write(bus + 0x14, 0x08000000)  # W1C only completion, preserve NACK.
    read(bus + 0x14, 0x00200000)
    read(aic + 0x4210, 0)
    write(bus + 0x18, 0x00b00040)  # Native read mask includes NACK.
    read(aic + 0x4210, 0x04000000)
    write(bus + 0x14, 0x00200000)
    read(bus + 0x14, 0)
    read(aic + 0x4210, 0)
    write(bus, 0x1e9)
    write(bus, 0x601)  # Read command consumes request but produces no fake RX.
    read(bus + 4, 0x100)
    read(bus + 8, 0)
    read(bus + 0x14, 0x08200000)
    write(bus + 0x10, 0x80000000)  # Bounded reset/flush model.
    read(bus + 0x14, 0)
    read(bus + 0x18, 0)
    read(bus + 4, 0x100)
    read(aic + 0x4210, 0)
    read(aic + 0x4214, 1)
    # A second controller/address behaves independently, without an ALS ID.
    bus += 0x1000
    write(bus + 0x18, 0x00b00040)
    write(bus, 0x173)
    read(bus + 0x14, 0x08200000)
    read(aic + 0x4210, 0x08000000)
    write(aic + 0x4190, 0x08000000)
    read(aic + 0x2004, 0x1009b)
    write(bus + 0x14, 0xffffffff)
    read(aic + 0x4210, 0)
    read(bus + 4, 0x100)
    write(0x800002000 + len(expected) * 4, 0xface)
    expected.append(0xface)
    code.append(0x14000000)
    return struct.pack('<' + 'I' * len(code), *code), expected


def main():
    payload, expected = fixture()
    with tempfile.TemporaryDirectory(prefix='a7i2c-', dir='/tmp') as directory:
        root = Path(directory)
        image = root / 'fixture.bin'; image.write_bytes(payload)
        proc = subprocess.Popen([str(BINARY), '-machine',
            's5l8960x,boot-entry=0x800000000,boot-args=0x800001000',
            '-kernel', str(image), '-accel', 'tcg', '-display', 'none', '-S',
            '-qmp', f'unix:{root}/qmp.sock,server=on,wait=off'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
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
                        'command-line': f'xp /{len(expected)}wx 0x800002000'})
                    actual = [int(x, 16) for x in re.findall(r'0x([0-9a-fA-F]+)', dump)]
                    if actual and actual[-1] == 0xface: break
                    assert time.monotonic() < deadline, dump
                    time.sleep(.01)
                assert actual == expected, (actual, expected)
                command(fp, 'quit')
            assert proc.wait(timeout=5) == 0
        finally:
            if proc.poll() is None: proc.kill(); proc.wait(timeout=5)
    print('PASS: guest NACK, RX empty, event masks, AIC claim/reassert, W1C, reset and unrelated IRQ preservation')
    print('Synthetic controller boundary; unmatched slave addresses remain unimplemented.')


if __name__ == '__main__': main()
