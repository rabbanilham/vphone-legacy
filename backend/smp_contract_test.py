#!/usr/bin/env python3
"""Two-CPU TCG contract. User-run after rebuilding: python3 backend/smp_contract_test.py."""
import json
from pathlib import Path
import re
import socket
import struct
import subprocess
import tempfile
import time

from smoke_test import BINARY, command


def fixture(directory):
    obj = directory / 'fixture.o'
    args = ['clang', '-target', 'aarch64-none-elf', '-c',
            str(Path(__file__).with_name('smp_contract.S')), '-o', str(obj)]
    subprocess.run(args, check=True)
    # Extract .text from clang's ELF64 little-endian relocatable object.
    data = obj.read_bytes()
    assert data[:6] == b'\x7fELF\x02\x01'
    shoff = struct.unpack_from('<Q', data, 40)[0]
    entsize, count, stridx = struct.unpack_from('<HHH', data, 58)
    sections = [struct.unpack_from('<IIQQQQIIQQ', data, shoff + i * entsize)
                for i in range(count)]
    strings = sections[stridx]
    names = data[strings[4]:strings[4] + strings[5]]
    for section in sections:
        name = names[section[0]:].split(b'\0', 1)[0]
        # No unresolved relocations may be silently discarded.
        assert not (section[1] == 4 and section[5]), name
        if name == b'.text':
            payload = data[section[4]:section[4] + section[5]]
    return payload


def run_case(root):
    image = root / 'fixture.bin'
    image.write_bytes(fixture(root))
    qmp = root / 'qmp.sock'
    proc = subprocess.Popen([
        str(BINARY), '-machine',
        's5l8960x,boot-entry=0x800000000,boot-args=0x800030000',
        '-smp', '2', '-kernel', str(image), '-accel', 'tcg,thread=single',
        '-icount', 'shift=1,sleep=off', '-display', 'none', '-serial', 'none',
        '-S', '-qmp', f'unix:{qmp},server=on,wait=off',
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 15
        while not qmp.exists():
            assert proc.poll() is None, proc.stderr.read().decode()
            assert time.monotonic() < deadline, 'QMP startup timed out'
            time.sleep(.01)
        sock = socket.socket(socket.AF_UNIX)
        sock.settimeout(5)
        sock.connect(str(qmp))
        with sock, sock.makefile('rwb') as fp:
            json.loads(fp.readline())
            command(fp, 'qmp_capabilities')
            command(fp, 'cont')
            while True:
                dump = command(fp, 'human-monitor-command', {
                    'command-line': 'xp /8wx 0x800020000'})
                actual = [int(x, 16) for x in re.findall(r'0x([0-9a-fA-F]+)', dump)]
                if actual and actual[-2] == 0xface:
                    break
                assert time.monotonic() < deadline, dump
                time.sleep(.01)
            assert actual == [0x11, 1, 2, 1, 1, 0, 0xface, 0], actual
            command(fp, 'quit')
        assert proc.wait(timeout=5) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def main():
    assert BINARY.exists(), 'Build backend first with scripts/qemu/build_backend.py'
    with tempfile.TemporaryDirectory(prefix='a7smp-', dir='/tmp') as directory:
        run_case(Path(directory))
    print('PASS: powered-off CPU1, PMGR release, CPU1 marker, cross/self IPIs on both cores')
    print('Synthetic EL1 contract only; native EL3/iOS startup still requires validation.')


if __name__ == '__main__':
    main()
