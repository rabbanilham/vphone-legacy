#!/usr/bin/env python3
"""Guest-executed ASP software-model tests; no filesystem/userspace boot claim."""
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
CMD = BASE + 0x20000
PAGE = BASE + 0x30000
OUTPUT = BASE + 0x40000
INIT_RESULT = BASE + 0x50000
ANS = 0x208040000
EP = 4 << 56
MARKER = 0x6162636465666768


def fixture(writable, short_read=False):
    code = move64(4, OUTPUT)
    expected = []

    def write(address, value, wide=False):
        code.extend(move64(2, address) + move64(0, value) +
                    [(0xf9000040 if wide else 0xb9000040)])

    def read(address, value, wide=True):
        code.extend(move64(2, address) + [(0xf9400043 if wide else 0xb9400043),
                    0xf9000003 | len(expected) << 10 | 4 << 5])
        expected.append(value)

    def send(message): write(ANS + 0x1010, message, True)
    def execute(opcode, status, lba=0, count=1, ppn=PAGE >> 12, tag=0, sub=None, length=4):
        write(CMD, opcode); write(CMD + 4, lba); write(CMD + 8, count)
        write(CMD + 0x30, ppn)
        if sub is not None:
            write(CMD + 0x34, length); write(CMD + 0x38, sub)
        send(EP | tag << 4 | 3)
        read(ANS + 0x1038, EP | status << 12 | tag << 4 | 2)

    send(EP | CMD << 16 | 0x4750)
    execute(0, 0)
    read(CMD + 0x30, 24, False)
    read(CMD + 0x34, 4096, False)
    read(CMD + 0x38, 256, False)
    read(CMD + 0x40, 0, False)  # Utility media has no persistent backing/model.
    execute(0x81, 5 if short_read else 0, ppn=INIT_RESULT >> 12, sub=16)
    read(INIT_RESULT, 5 if short_read else 0, False)
    execute(0x81, 1, ppn=0x20a0a0000 >> 12, sub=16)
    execute(0x81, 1, sub=16, length=8)
    execute(0x81, 5, sub=17)
    # Native first SetTimestamp ignores supplied elapsed and derives calendar
    # delta; subsequent backwards elapsed is rejected while calendar is set.
    write(CMD + 0x48, 100); write(CMD + 0x4c, 42)
    execute(0x80, 0, sub=6)
    execute(0x80, 0, sub=5)
    read(CMD + 0x44, 0, False)
    read(CMD + 0x48, (100, 105), False)
    read(CMD + 0x4c, (100, 105), False)
    write(CMD + 0x48, 95); write(CMD + 0x4c, 50)
    execute(0x80, 0, sub=6)
    execute(0x80, 0, sub=5)
    read(CMD + 0x48, (95, 100), False)
    read(CMD + 0x4c, (100, 105), False)
    write(CMD + 0x48, 200); write(CMD + 0x4c, 150)
    execute(0x80, 0, sub=6)
    # Wait two nominal counter seconds: QEMU floors the counter period to41ns,
    # so24M counts are984ms.48M counts safely pass a full clock second.
    code.extend(move64(12, 48000000) + [0xd53be02a, 0x8b0c014c,
                0xd53be02a, 0xeb0c015f, 0x54ffffc3])
    execute(0x80, 0, sub=5)
    read(CMD + 0x48, (201, 205), False)
    read(CMD + 0x4c, (151, 155), False)
    execute(0x80, 5, sub=7)
    execute(0x10, 5 if short_read else 0, lba=8)
    read(PAGE, 0 if short_read else 0x3c3c3c3c3c3c3c3c)
    execute(0x10, 0, lba=1)
    read(PAGE, 0x5452415020494645)  # Real generated GPT header bytes.
    write(PAGE, MARKER, True)
    execute(0x11, 0, lba=12)
    execute(0x13, 0)  # Native synchronize cache.
    execute(0x11, 0 if writable else 5, lba=8)
    execute(0x10, 1, lba=0xffffffff)
    execute(0x10, 1, lba=8, count=17)  # Tag0 only supports 64 KiB.
    execute(0x10, 1, lba=8, ppn=0x20a0a0000 >> 12)  # DMA cannot touch UART MMIO.
    execute(0x37, 5)  # Unsupported utility command cannot report success.
    execute(0x10, 1, lba=8, tag=32)  # No descriptor beyond native32tagpool.
    write(OUTPUT + len(expected) * 8, 0xface, True)
    expected.append(0xface)
    code.append(0x14000000)
    return struct.pack('<' + 'I' * len(code), *code), expected


def run(root, writable, short_read=False):
    system = root / 'system.raw'; system.write_bytes(b'\x3c' * (4 * 4096))
    data = root / 'data.raw'; data.write_bytes(b'\x00' * (4 * 4096))
    payload, expected = fixture(writable, short_read)
    image = root / 'guest.bin'; image.write_bytes(payload)
    sockpath = root / 'qmp.sock'
    machine = (f's5l8960x,boot-entry={BASE:#x},boot-args={BASE+0x1000:#x},'
               f'system-image={system},data-image={data},system-writable={int(writable)}')
    common = [str(BINARY), '-machine', machine, '-kernel', str(image),
              '-accel', 'tcg', '-display', 'none']
    with (root / 'stderr.log').open('wb') as stderr:
        proc = subprocess.Popen(common + ['-S', '-qmp',
            f'unix:{sockpath},server=on,wait=off'], stdout=subprocess.DEVNULL, stderr=stderr)
        try:
            deadline = time.monotonic() + 5
            while not sockpath.exists():
                assert proc.poll() is None, (root / 'stderr.log').read_text()
                assert time.monotonic() < deadline
                time.sleep(.01)
            # A second writer must fail before issuing guest commands.
            competing = subprocess.run(common, capture_output=True, timeout=5)
            assert competing.returncode != 0 and b'already in use' in competing.stderr, competing.stderr
            if short_read: system.write_bytes(b'')  # Inject post-open host EOF, not a zero sector.
            sock = socket.socket(socket.AF_UNIX); sock.settimeout(5); sock.connect(str(sockpath))
            with sock, sock.makefile('rwb') as fp:
                json.loads(fp.readline()); command(fp, 'qmp_capabilities'); command(fp, 'cont')
                while True:
                    dump = command(fp, 'human-monitor-command', {
                        'command-line': f'xp /{len(expected)}gx {OUTPUT:#x}'})
                    actual = [int(x, 16) for x in re.findall(r'0x([0-9a-fA-F]+)', dump)]
                    if actual and actual[-1] == 0xface: break
                    assert time.monotonic() < deadline, dump
                    time.sleep(.01)
                assert len(actual) == len(expected), (actual, expected)
                assert all((want[0] <= got <= want[1]) if isinstance(want, tuple)
                           else got == want for got, want in zip(actual, expected)), (actual, expected)
                command(fp, 'quit')
            assert proc.wait(timeout=5) == 0
        finally:
            if proc.poll() is None: proc.kill(); proc.wait(timeout=5)
    assert data.read_bytes()[:8] == struct.pack('<Q', MARKER), 'Data write discarded'
    if writable:
        assert system.read_bytes()[:8] == struct.pack('<Q', MARKER), 'System write discarded'
    elif not short_read:
        assert system.read_bytes() == b'\x3c' * (4 * 4096), 'Read-only System mutated'
    # Persistence is verified across emulator exit and a separate host open.
    with data.open('rb') as fp: assert fp.read(8) == struct.pack('<Q', MARKER)
    if not short_read:
        # A fresh QEMU process/guest reads persisted Data, rather than trusting
        # only a host read after exit. Reopening also verifies lock ownership ends.
        code = move64(4, OUTPUT)
        def put(address, value, wide=False):
            code.extend(move64(2, address) + move64(0, value) +
                        [0xf9000040 if wide else 0xb9000040])
        put(ANS + 0x1010, EP | CMD << 16 | 0x4750, True)
        put(CMD, 0x10); put(CMD + 4, 12); put(CMD + 8, 1)
        put(CMD + 0x30, PAGE >> 12)
        put(ANS + 0x1010, EP | 3, True)
        code.extend(move64(2, ANS + 0x1038) + [0xf9400043, 0xf9000083])
        code.extend(move64(2, PAGE) + [0xf9400043, 0xf9000483])
        put(OUTPUT + 16, 0xface, True); code.append(0x14000000)
        image.write_bytes(struct.pack('<' + 'I' * len(code), *code))
        sockpath.unlink(missing_ok=True)
        reopened = subprocess.Popen(common + ['-S', '-qmp',
            f'unix:{sockpath},server=on,wait=off'], stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not sockpath.exists():
                assert reopened.poll() is None, reopened.stderr.read()
                assert time.monotonic() < deadline
                time.sleep(.01)
            sock = socket.socket(socket.AF_UNIX); sock.settimeout(5)
            sock.connect(str(sockpath))
            with sock, sock.makefile('rwb') as fp:
                json.loads(fp.readline()); command(fp, 'qmp_capabilities'); command(fp, 'cont')
                while True:
                    dump = command(fp, 'human-monitor-command', {
                        'command-line': f'xp /3gx {OUTPUT:#x}'})
                    actual = [int(x, 16) for x in re.findall(r'0x([0-9a-fA-F]+)', dump)]
                    if actual and actual[-1] == 0xface: break
                    assert time.monotonic() < deadline, dump
                    time.sleep(.01)
                assert actual == [EP | 2, MARKER, 0xface], actual
                command(fp, 'quit')
            assert reopened.wait(timeout=5) == 0
        finally:
            if reopened.poll() is None: reopened.kill(); reopened.wait(timeout=5)
    return common


def main():
    with tempfile.TemporaryDirectory(prefix='a7ans-', dir='/tmp') as directory:
        root = Path(directory)
        for mode in ['readonly', 'writable', 'host-eof']:
            case = root / mode; case.mkdir()
            run(case, mode == 'writable', mode == 'host-eof')
        payload, _ = fixture(False)
        image = root / 'invalid-guest.bin'; image.write_bytes(payload)
        invalid = root / 'bad.raw'; invalid.write_bytes(b'bad')
        basecmd = [str(BINARY), '-kernel', str(image), '-display', 'none']
        for props, error in [(f'system-image={invalid}', b'both system-image'),
                             (f'system-image={invalid},data-image={invalid}', b'sector-aligned'),
                             ('system-writable=2', b'must be 0 or 1')]:
            result = subprocess.run(basecmd + ['-machine', f's5l8960x,boot-entry={BASE:#x},boot-args={BASE+0x1000:#x},' + props],
                                    capture_output=True, timeout=5)
            assert result.returncode != 0 and error in result.stderr, result.stderr
    print('PASS: guest ASP geometry/GPT, actual file read/write, System write durability, cache flush, file locking')
    print('PASS: DMA/tag/count/LBA bounds, host short read, read-only write, unsupported opcode and bad-image failures')
    print('PASS: native init-status DMA and actual backing failure; virtual clock advancement and native timestamp policy')
    print('ASP software model only; filesystem mount, launchd and SpringBoard remain unverified.')


if __name__ == '__main__': main()
