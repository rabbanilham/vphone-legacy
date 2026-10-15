#!/usr/bin/env python3
"""Synthetic AGX META firmware host-interface checks; not evidence of an iOS boot.

The fixture mirrors the native AGXMetaFirmware boot ABI: an sgx1 sequencer
program sets RGX_CR_META_BOOT and RGX_CR_BIF_CAT_BASE0, the GPU page catalogue
maps the firmware boot page (PIO list + argv) and the shared control page, and
the modeled firmware posts one type-0 "alive" entry to firmware->host ring 0
and raises AIC IRQ 84. An empty sequencer program must post nothing. A
doorbell on host ring 1 registers a channel queue whose type-4 command must
store its stamp and produce a type-1 completion event with a second IRQ 84.
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

RAM = 0x800000000
DATA = RAM + 0x10000
PC, PD, PT0, PT2 = DATA, DATA + 0x1000, DATA + 0x2000, DATA + 0x3000
BOOT, ARGV, SHARED = DATA + 0x10000, DATA + 0x11000, DATA + 0x12000
QUEUE, SHARED_9, SHARED_B = DATA + 0x13000, DATA + 0x14000, DATA + 0x15000
QUEUE_META = 0x40030000
SGX0 = 0x201000000
RESULT = RAM + 0x2000
SGX1, AIC = 0x201e00000, 0x20e100000
HEAP = 0xe1c0000000


def data_blob():
    blob = bytearray(SHARED_B + 0x1000 - DATA)
    def put(pa, fmt, *values): struct.pack_into(fmt, blob, pa - DATA, *values)
    def pte(pa): return pa | 1
    put(PC + ((HEAP >> 30) & 0x3ff) * 4, '<I', ((PD >> 12) << 4) | 1)
    put(PD + ((HEAP >> 21) & 0x1ff) * 8, '<Q', pte(PT0))
    put(PD + (((HEAP + 0x448000) >> 21) & 0x1ff) * 8, '<Q', pte(PT2))
    put(PT0 + 0 * 8, '<Q', pte(BOOT))
    put(PT0 + 0x24 * 8, '<Q', pte(ARGV))
    put(PT2 + (((HEAP + 0x448000) >> 12) & 0x1ff) * 8, '<Q', pte(SHARED))
    put(PT2 + (((HEAP + 0x448000 + 0x9000) >> 12) & 0x1ff) * 8, '<Q', pte(SHARED_9))
    put(PT2 + (((HEAP + 0x448000 + 0xb000) >> 12) & 0x1ff) * 8, '<Q', pte(SHARED_B))
    put(PT0 + 0x30 * 8, '<Q', pte(QUEUE))
    # Host ring 1 (control shared+0x98e0, entries shared+0xb140): one queue registration.
    put(SHARED_9 + 0x8e0, '<5I', 0, 0, 0, 0, 1)
    put(SHARED_B + 0x140, '<3I', 1, 0, QUEUE_META)
    # Channel queue block: control/entries pointers, one type-4 stamp command.
    put(QUEUE, '<3I', 0, QUEUE_META + 0x30, QUEUE_META + 0x70)
    put(QUEUE + 0x30, '<13I', 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 4)
    put(QUEUE + 0x70, '<I', QUEUE_META + 0x100)
    put(QUEUE + 0x100, '<4I', 4, QUEUE_META + 0x200, 0x1234, 7)
    # Boot page: one PIO pair, (0, 0) terminator, then META address of argv.
    put(BOOT + 0x200, '<5I', 0x4850000, 0x10000f02, 0, 0, 0xc0024000)
    put(ARGV, '<2I', 0x40438000, 0xc0448000)
    put(SHARED + 0xc0, '<Q', 0xffffffffffffffff)  # must become a type-0 entry
    return bytes(blob)


def fixture():
    code = move64(1, SGX1 + 0x10000) + move64(3, SGX1 + 0x1000) + move64(4, RESULT)
    code += move64(7, AIC) + move64(8, AIC + 0x4000) + move64(9, SGX0)
    def store(base, offset, value):
        return move64(2, value) + [0xb9000000 | (offset // 4) << 10 | base << 5 | 2]
    # Native GPU probe at 0xffffff8001188614: empty program, then run.
    code += store(1, 0, 0xffff0000) + store(3, 0, 0x11)
    # Native META start at 0xffffff800118727c: META_BOOT, CAT_BASE0, run.
    for offset, value in enumerate([0xff0bf8, 1, 0xff1200, PC & 0xffffffff,
                                    0xff1204, PC >> 32, 0xffff0000]):
        code += store(1, offset * 4, value)
    code += store(3, 0, 0x11)
    code += store(8, 0x188, 1 << 20)  # AIC+0x4188: unmask IRQ 84
    # Spin on AIC IAR until an interrupt is delivered, record it, then mark done.
    def wait_irq(slot):
        return [0xb9400000 | (0x2004 // 4) << 10 | 7 << 5 | 6,   # ldr w6, [x7, #0x2004]
                0x34000000 | ((-1 & 0x7ffff) << 5) | 6,           # cbz w6, .-4
                0xb9000000 | slot << 10 | 4 << 5 | 6]             # str w6, [x4, #slot*4]
    code += wait_irq(0)
    # Acknowledge as the driver does (0xffffff80011852c0), re-enable, then kick ring 1.
    code += store(8, 0x88, 1 << 20) + store(8, 0x188, 1 << 20) + store(9, 0xb00, 0x11)
    code += wait_irq(2)
    code += store(4, 4, 0xface) + [0x14000000]
    return struct.pack('<' + 'I' * len(code), *code)


def main():
    with tempfile.TemporaryDirectory(prefix='a7agx-', dir='/tmp') as directory:
        root = Path(directory)
        (root / 'fixture.bin').write_bytes(fixture())
        (root / 'data.bin').write_bytes(data_blob())
        cmd = [str(BINARY), '-machine', 's5l8960x,boot-entry=0x800000000,boot-args=0x800001000',
               '-kernel', str(root / 'fixture.bin'), '-accel', 'tcg', '-display', 'none', '-S',
               '-device', f'loader,file={root}/data.bin,addr={DATA:#x},force-raw=on',
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
                def words(addr, count):
                    dump = command(fp, 'human-monitor-command', {'command-line': f'xp /{count}wx {addr:#x}'})
                    return [int(x, 16) for x in re.findall(r'0x([0-9a-fA-F]+)', dump)[-count:]]
                while words(RESULT + 4, 1) != [0xface]:
                    assert time.monotonic() < deadline, 'guest never observed an interrupt'
                    time.sleep(.01)
                assert words(RESULT, 1) == [0x10000 | 84], hex(words(RESULT, 1)[0])
                read_index, _, _, _, write_index = words(SHARED + 0x40, 5)
                assert (read_index, write_index) == (0, 2), (read_index, write_index)
                assert words(SHARED + 0xc0, 10) == [0] * 10, 'alive entry is not type 0'
                assert words(RESULT + 8, 1) == [0x10000 | 84], 'no completion interrupt'
                assert words(QUEUE + 0x200, 1) == [0x1234], 'stamp not stored'
                assert words(QUEUE + 0x30, 1) == [1], 'queue read index not advanced'
                assert words(SHARED_9 + 0x8e0, 1) == [1], 'host ring 1 read index not advanced'
                assert words(SHARED + 0x50, 1) == [2], 'no completion event posted'
                assert words(SHARED + 0xc0 + 0x28, 1) == [1], 'completion event is not type 1'
                command(fp, 'quit')
            assert proc.wait(timeout=5) == 0
        finally:
            if proc.poll() is None: proc.kill(); proc.wait(timeout=5)
    print('PASS: AGX META boot program -> one type-0 alive entry on firmware ring 0 and AIC IRQ 84; empty program posts nothing')
    print('PASS: AGX doorbell 0x11 -> channel queue registered, type-4 stamp stored, type-1 completion event and IRQ 84')
    print('Synthetic AGX host-interface contract only; no GPU command execution or iOS rendering inferred.')


if __name__ == '__main__': main()
