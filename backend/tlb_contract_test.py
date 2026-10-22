#!/usr/bin/env python3
"""Bare-metal page/block remap regression. Requires clang and a built backend.

Run: python3 backend/tlb_contract_test.py
Unfixed QEMU should leave result[1] == 0x11 instead of 0x22.
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


def fixture(directory, *, tbi=False, local=False):
    obj = directory / 'fixture.o'
    args = ['clang', '-target', 'aarch64-none-elf', '-c',
            str(Path(__file__).with_name('tlb_contract.S')), '-o', str(obj)]
    if tbi:
        args.append('-DTBI')
    if local:
        args.append('-DTLBI_OP=vae1')
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


def run_case(root, *, tbi, local):
    image = root / 'fixture.bin'
    image.write_bytes(fixture(root, tbi=tbi, local=local))
    qmp = root / 'qmp.sock'
    proc = subprocess.Popen([
        str(BINARY), '-machine',
        's5l8960x,boot-entry=0x800000000,boot-args=0x800030000',
        '-kernel', str(image), '-accel', 'tcg,thread=single',
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
                    'command-line': 'xp /5wx 0x800020000'})
                actual = [int(x, 16) for x in re.findall(r'0x([0-9a-fA-F]+)', dump)]
                if actual and actual[-1] == 0xface:
                    break
                assert time.monotonic() < deadline, dump
                time.sleep(.01)
            assert actual == [0x11, 0x22, 0x33, 0x44, 0xface], actual
            command(fp, 'quit')
        assert proc.wait(timeout=5) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def main():
    assert BINARY.exists(), 'Build backend first with scripts/qemu/build_backend.py'
    for tbi in (False, True):
        for local in (False, True):
            with tempfile.TemporaryDirectory(prefix='a7tlb-', dir='/tmp') as directory:
                run_case(Path(directory), tbi=tbi, local=local)
            print(f'PASS: block and page remap, TBI={tbi}, local={local}')
    print('Synthetic TCG invalidation contract only; iOS reproducer still required.')


if __name__ == '__main__':
    main()
