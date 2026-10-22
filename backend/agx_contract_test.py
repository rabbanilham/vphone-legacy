#!/usr/bin/env python3
"""Synthetic AGX META firmware host-interface checks; not evidence of an iOS boot.

The fixture mirrors the native AGXMetaFirmware boot ABI: an sgx1 sequencer
program sets RGX_CR_META_BOOT and RGX_CR_BIF_CAT_BASE0, the GPU page catalogue
maps the firmware boot page (PIO list + argv) and the shared control page, and
the modeled firmware posts one type-0 "alive" entry to firmware->host ring 0
and raises AIC IRQ 84. An empty sequencer program must post nothing. A
doorbell on host ring 1 registers a channel queue whose type-4 command must
store its stamp and produce a type-1 completion event with a second IRQ 84.
A synthetic type-1 render logs RT0 translation and the first discontinuity.
Context and CAT_BASE0 fallback runs (fill unset and 11223344) check
no-doorbell ring-3 binds, opt-in little-endian pixel writes,
noncontiguous pages, and untouched padding. The context/no-fill case also
checks one exact PBE hit in a referenced buffer. Replay cases check two synthetic
quad draws into a PBE target with the default environment and the explicit
opt-out, TA pairing, transforms, dense targets, dest-in masks and vibrancy.
Requires an existing QEMU binary built with the replay rules under test.
"""
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

RAM = 0x800000000
DATA = RAM + 0x10000
PC, PD, PT0, PT2 = DATA, DATA + 0x1000, DATA + 0x2000, DATA + 0x3000
BOOT, ARGV, SHARED = DATA + 0x10000, DATA + 0x11000, DATA + 0x12000
QUEUE, SHARED_9, SHARED_B = DATA + 0x13000, DATA + 0x14000, DATA + 0x15000
CTX_PC = DATA + 0x7000
CONTEXT_ID, CONTEXT_SLOT = 7, 10
SHARED_E = DATA + 0x1a000
RT_PD, RT_PT0, RT_PT1 = DATA + 0x4000, DATA + 0x5000, DATA + 0x6000
RT_GVA = 0x60a00000
RT_PA, RT_SECOND, RT_LAST = DATA + 0x16000, DATA + 0x18000, DATA + 0x19000
QUEUE_META = 0x40030000
SGX0 = 0x201000000
RESULT = RAM + 0x2000
SGX1, AIC = 0x201e00000, 0x20e100000
HEAP = 0xe1c0000000


def data_blob(context=True, pbe=False):
    blob = bytearray(SHARED_E + 0x1000 - DATA)
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
    put(PT2 + (((HEAP + 0x448000 + 0xe000) >> 12) & 0x1ff) * 8, '<Q', pte(SHARED_E))
    put(PT0 + 0x30 * 8, '<Q', pte(QUEUE))
    # In the bind case RT0 is absent from CAT_BASE0, mapped only in CTX_PC.
    # Keep the original firmware-catalogue fixture for fallback coverage.
    rt_catalogue = CTX_PC if context else PC
    put(rt_catalogue + ((RT_GVA >> 30) & 0x3ff) * 4, '<I', ((RT_PD >> 12) << 4) | 1)
    put(RT_PD + ((RT_GVA >> 21) & 0x1ff) * 8, '<Q', pte(RT_PT0))
    put(RT_PD + (((RT_GVA + 0x2c5000) >> 21) & 0x1ff) * 8, '<Q', pte(RT_PT1))
    put(RT_PT0, '<2Q', pte(RT_PA), pte(RT_SECOND))
    put(RT_PT1 + (((RT_GVA + 0x2c5000) >> 12) & 0x1ff) * 8, '<Q', pte(RT_LAST))
    for page in (RT_PA, RT_SECOND, RT_LAST):
        blob[page - DATA:page - DATA + 0x1000] = b'\xa5' * 0x1000
    # Host ring 1 (control shared+0x98e0, entries shared+0xb140): one queue registration.
    put(SHARED_9 + 0x8e0, '<5I', 0, 0, 0, 0, 1)
    put(SHARED_B + 0x140, '<3I', 1, 0, QUEUE_META)
    # No ring-3 doorbell: unknown type and invalid slot must be skipped, then bind.
    if context:
        put(SHARED_9 + 0x920, '<5I', 0, 0, 0, 0, 3)
        put(SHARED_E + 0x140, '<4IQ', 99, 0, 0, 0, 0)
        put(SHARED_E + 0x158, '<4IQ', 3, 0, 16, 0, CTX_PC)
        put(SHARED_E + 0x170, '<4IQ', 3, 0, CONTEXT_SLOT, 0, CTX_PC)
    # Channel queue block: control/entries pointers, a type-1 render then a type-4 stamp.
    put(QUEUE, '<3I', 0, QUEUE_META + 0x30, QUEUE_META + 0x70)
    put(QUEUE + 0x30, '<13I', 0, 0, 0, 0, 0, 0, 0, 0, 2, 0, 0, 0, 4)
    put(QUEUE + 0x70, '<2I', QUEUE_META + 0x300, QUEUE_META + 0x100)
    put(QUEUE + 0x100, '<4I', 4, QUEUE_META + 0x200, 0x1234, 7)
    put(QUEUE + 0x300, '<2I', 1, CONTEXT_ID)
    # Three active pixels, two rows, encoded 4096-byte pitch.
    put(QUEUE + 0x368, '<4I', RT_GVA, 0x020007fc, 1, 2)
    put(QUEUE + 0x4d0, '<I', 2)
    if pbe:
        # A mapped context buffer, referenced twice: scan it only once.
        put(QUEUE + 0x340, '<I', RT_GVA)
        put(QUEUE + 0x350, '<I', RT_GVA)
        put(RT_PA + 0x100, '<Q', 0x400470000 >> 4)
    # Boot page: one PIO pair, (0, 0) terminator, then META address of argv.
    put(BOOT + 0x200, '<5I', 0x4850000, 0x10000f02, 0, 0, 0xc0024000)
    put(ARGV, '<2I', 0x40438000, 0xc0448000)
    put(SHARED + 0xc0, '<Q', 0xffffffffffffffff)  # must become a type-0 entry
    return bytes(blob)


REPLAY_TARGET = 0x400000000
REPLAY_TEXELS = (0xff010203, 0xff102030, 0xff405060, 0xff708090,
                 0xffa0b0c0, 0xffd0e0f0, 0xff123456, 0xffabcdef)


# Exact CA mask program: its first 64 bytes identify AGX_USC_DEST_IN.
REPLAY_MASK_CODE = bytes.fromhex(
    '4520b002c44010c0400002806aff4620f44c806080048880a00002806aff280a9400'
    '11f01b8222221000a0a30020398a80940011f01b8222221000a1a30021ff')

# Exact CA narrow blur program: its first 64 bytes identify AGX_USC_BLUR_1.
REPLAY_BLUR_CODE = bytes.fromhex(
    '2a0a949999af1b800033000080408001880020ff3a8a80949999af1b800033000082'
    '4080038800210024088b4520b006c44010c0400002806aff4620f44c8060')
REPLAY_CASES = ('pairing', 'transform', 'dense', 'dest-in', 'gauss-blur', 'vibrancy-dark', 'vibrancy-light')


def blend_vibrancy(dst, src):
    """Integer CA darken/lightenSourceOver oracle, including alpha and truncation."""
    d = [(dst >> shift) & 255 for shift in range(0, 32, 8)]
    s = [(src >> shift) & 255 for shift in range(0, 32, 8)]
    y = (54 * d[2] + 183 * d[1] + 19 * d[0]) >> 8
    y1 = 255 - y if sum(s[:3]) > 384 else y
    y2 = y1 * y1 // 255
    f = y2 * y2 // 255
    s = [c * f // 255 for c in s]
    return sum(min(s[c] + d[c] * (255 - s[3]) // 255, 255) << (8 * c)
               for c in range(4))


def replay_blob(case=True):
    """Build a replay case and its full physical target oracle using context mappings."""
    blob = bytearray(data_blob())
    def put(pa, fmt, *values): struct.pack_into(fmt, blob, pa - DATA, *values)
    def page():
        pa = DATA + len(blob)
        blob.extend(bytes(0x1000))
        return pa
    directories, tables = {}, {}
    def map_page(gva):
        ci, di, ti = (gva >> 30) & 0x3ff, (gva >> 21) & 0x1ff, (gva >> 12) & 0x1ff
        if ci not in directories:
            directories[ci] = page()
            put(CTX_PC + ci * 4, '<I', ((directories[ci] >> 12) << 4) | 1)
        if (ci, di) not in tables:
            tables[ci, di] = page()
            put(directories[ci] + di * 8, '<Q', tables[ci, di] | 1)
        pa = page()
        put(tables[ci, di] + ti * 8, '<Q', pa | 1)
        return pa

    state_gva = 0x10000000
    state = map_page(state_gva)
    pds = map_page(0x600001000)
    fragment = map_page(0x680001000)
    # Original cases retain the display hole; new cases map through +0xa000.
    # The extra pages are guards too: compare every byte, including row padding.
    synthetic = isinstance(case, str)
    width, height = ((40, 40) if case == 'dense' else (16, 16)) if synthetic else (640, 1136)
    pitch = ((width + 15) & ~15) * 4 if synthetic else 2560
    target_pages = [map_page(REPLAY_TARGET + offset)
                    for offset in range(0, 0xb000 if synthetic else 0xa000, 0x1000)]
    for pa in target_pages:
        blob[pa - DATA:pa - DATA + 0x1000] = b'\xa5' * 0x1000

    # Register the TA queue first so its type-0 command is retained before RT.
    put(SHARED_9 + 0x8f0, '<I', 2)
    put(SHARED_B + 0x140, '<3I', 1, 0, QUEUE_META + 0x800)
    put(SHARED_B + 0x158, '<3I', 1, 0, QUEUE_META)
    put(QUEUE + 0x800, '<3I', 0, QUEUE_META + 0x830, QUEUE_META + 0x870)
    put(QUEUE + 0x830, '<13I', 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 4)
    put(QUEUE + 0x870, '<I', QUEUE_META + 0x900)
    put(QUEUE + 0x900, '<2I', 0, CONTEXT_ID)
    put(QUEUE + 0x968, '<Q', state_gva)
    # The 3D command pairs with this TA through the parameter buffer address.
    put(QUEUE + 0x900 + 0x14, '<I', 0xc0460730)
    put(QUEUE + 0x300 + 0x10, '<I', 0xc0460730)

    # VDM state: vertex PDS data/code and two extra words; PPP; six vertices.
    stream = []
    for vs, ppp in ((0x1000, state_gva + 0x100), (0x1020, state_gva + 0x110)):
        stream += [0x4c000000 | (1 << 27) | (1 << 26), vs, 0, 0, 0,
                   1 << 8, ppp, 0x6a000600, 6, 0]
    stream += [0xc0000000]
    put(state, '<' + 'I' * len(stream), *stream)
    put(state + 0x100, '<I', 0)  # solid PPP has no texture pointer
    put(state + 0x110, '<I', 0x80001000)
    put(pds, '<IIQ', 20, 0, state_gva + 0x200)
    put(pds + 0x20, '<IIQ', 24, 0, state_gva + 0x300)
    for i, (u, v) in enumerate(((0, 0), (1, 0), (0, 1), (0, 1), (1, 0), (1, 1))):
        put(state + 0x200 + i * 20, '<4fI', u * 8, v * 4, 0, 1, 0xff112233)
        put(state + 0x300 + i * 24, '<6f', 2 + u * 4, v * 2, 0, 1, u, v)
    put(fragment, '<Q', state_gva + 0x400)
    texture_gva = state_gva + 0x500
    q = (texture_gva >> 2) << 16
    # w0 0x60008864: linear BGRA layout, as on the boot logo's real TSW.
    put(state + 0x400, '<4I', 0x60008864, (2 - 1) << 16 | (4 - 1) << 2,
        q & 0xffffffff, q >> 32)
    for y in range(2):
        put(state + 0x500 + y * 64, '<4I', *REPLAY_TEXELS[y * 4:y * 4 + 4])
    # EOT PDS word 2 -> PBE words -> target, not the legacy cmd+0x68 fallback.
    put(QUEUE + 0x300 + 0x1bc, '<2I', 0x1080, 12)
    put(pds + 0x80, '<3I', 0, 0, state_gva + 0x600)
    put(state + 0x600, '<2I', (REPLAY_TARGET >> 2) & 0xffffffff, REPLAY_TARGET >> 34)
    # Pass dimensions are max y/x in low 16 bits, with unrelated flags above.
    put(QUEUE + 0x370, '<2I', 0x60000000 | (height - 1), 0xa0000000 | (width - 1))
    expected = bytearray(b'\xa5' * (len(target_pages) * 0x1000))
    def expect_rect(rect, pixel):
        x0, y0, x1, y1 = rect
        for y in range(y0, y1):
            for x in range(x0, x1):
                struct.pack_into('<I', expected, y * pitch + x * 4, pixel(x, y))
    draws, ta_count = 2, 1
    if case is True:
        expect_rect((0, 0, 8, 4), lambda x, y: 0xff112233)
        expect_rect((2, 0, 6, 2), lambda x, y: REPLAY_TEXELS[y * 4 + x - 2])
    if synthetic:
        assert case in REPLAY_CASES, case
        expected[:] = b'\xa5' * len(expected)
        if case in ('pairing', 'transform', 'dense'):
            # One solid draw; terminate before the original texture draw.
            put(state + 10 * 4, '<I', 0xc0000000)
            draws = 1
            rect = (0, 0, 8, 4)
            if case == 'pairing':
                # Both TAs precede 3D; +0x10 selects the SECOND-to-last TA.
                ta_count = 2
                put(QUEUE + 0x830 + 8 * 4, '<I', ta_count)
                put(QUEUE + 0x874, '<I', QUEUE_META + 0xc00)
                put(QUEUE + 0xc00, '<2I', 0, CONTEXT_ID)
                put(QUEUE + 0xc14, '<I', 0xc0460830)
                put(QUEUE + 0xc68, '<Q', state_gva + 0x700)
                put(state + 0x700, '<11I', 0x4c000000, 0x1100, 0, 0, 0,
                    1 << 8, state_gva + 0x100, 0x6a000600, 6, 0, 0xc0000000)
                put(pds + 0x100, '<IIQ', 20, 0, state_gva + 0x800)
                for i, (u, v) in enumerate(((0, 0), (1, 0), (0, 1), (0, 1), (1, 0), (1, 1))):
                    put(state + 0x800 + i * 20, '<4fI', u * 8, v * 4, 0, 1, 0xffaabbcc)
            elif case == 'transform':
                # W=H=16 orthographic matrix, screen rect (34,50)-(42,54).
                # A 4/4 viewport (different from the pass default 8/8) gives
                # pixel x=x_screen/2-15, y=y_screen/2-23 -> (2,2)-(6,4).
                put(pds + 0x10, '<Q', state_gva + 0x900)
                matrix = [0.] * 16
                matrix[0] = matrix[5] = 2 / 16
                matrix[12], matrix[13], matrix[15] = -4.75, -6.75, 1.
                put(state + 0x900, '<16f', *matrix)
                put(state + 5 * 4, '<I', 6 << 8)
                put(state + 0x100, '<6f', 4, 4, 4, 4, .5, .5)
                for i, (u, v) in enumerate(((0, 0), (1, 0), (0, 1), (0, 1), (1, 0), (1, 1))):
                    put(state + 0x200 + i * 20, '<4fI', 34 + u * 8, 50 + v * 4, 0, 1, 0xff112233)
                rect = (2, 2, 6, 4)
            elif case == 'dense':
                # 40 pixels need 48-pixel/192-byte rows, crossing page boundaries
                # and the 16-row boundary. Eight padding pixels stay untouched.
                for i, (u, v) in enumerate(((0, 0), (1, 0), (0, 1), (0, 1), (1, 0), (1, 1))):
                    put(state + 0x200 + i * 20, '<4fI', u * 40, v * 40, 0, 1, 0xff112233)
                rect = (0, 0, 40, 40)
            expect_rect(rect, lambda x, y: 0xff112233)
        else:
            destination = 0xff907050
            for i in range(6):
                put(state + 0x200 + i * 20 + 16, '<I', destination)
            expect_rect((0, 0, 8, 4), lambda x, y: destination)
            if case == 'dest-in':
                # PPP[7] -> PDS state -> USC offset -> exact 64 code bytes.
                put(state + 15 * 4, '<I', 8 << 8)
                put(state + 0x110 + 7 * 4, '<I', 0x1200)
                put(pds + 0x200, '<I', 0x1000)
                usc = map_page(0x700001000)
                assert len(REPLAY_MASK_CODE) == 64
                fingerprint = 0xcbf29ce484222325
                for byte in REPLAY_MASK_CODE:
                    fingerprint = ((fingerprint ^ byte) * 0x100000001b3) & 0xffffffffffffffff
                assert fingerprint == 0x4ee01acc47551344
                put(usc, '<64s', REPLAY_MASK_CODE)
                alphas = (0, 64, 128, 255, 255, 128, 64, 0)
                for y in range(2):
                    put(state + 0x500 + y * 64, '<4I',
                        *(a << 24 | 0x123456 for a in alphas[y * 4:y * 4 + 4]))
                def masked(x, y):
                    a = alphas[y * 4 + x - 2]
                    return sum(((destination >> c) & 255) * a // 255 << c for c in range(0, 32, 8))
                expect_rect((2, 0, 6, 2), masked)
            elif case == 'gauss-blur':
                put(state + 15 * 4, '<I', 8 << 8)
                put(state + 0x110 + 7 * 4, '<I', 0x1200)
                put(pds + 0x200, '<I', 0x1000)
                usc = map_page(0x700001000)
                assert len(REPLAY_BLUR_CODE) == 64
                fingerprint = 0xcbf29ce484222325
                for byte in REPLAY_BLUR_CODE:
                    fingerprint = ((fingerprint ^ byte) * 0x100000001b3) & 0xffffffffffffffff
                assert fingerprint == 0x115ac3fe908dec8a
                put(usc, '<64s', REPLAY_BLUR_CODE)
                blur_texel = 0xff304050
                for y in range(2):
                    put(state + 0x500 + y * 64, '<4I', *([blur_texel] * 4))
                def blurred_over(x, y):
                    a = (blur_texel >> 24) & 0xff
                    out = 0
                    for c in range(0, 32, 8):
                        sc = (blur_texel >> c) & 0xff
                        dc = (destination >> c) & 0xff
                        v = min(255, sc + dc * (255 - a) // 255)
                        out |= v << c
                    return out
                expect_rect((2, 0, 6, 2), blurred_over)
            else:
                # Luma constants, four sampler words, then the TSW at +28.
                q = (texture_gva >> 2) << 16
                put(state + 0x400, '<11I', 0x3d93a000, 0x3e59a000, 0x3f372000,
                    0xfff, 0x2400, 0, 0, 0x60008864, 1 << 16 | 3 << 2,
                    q & 0xffffffff, q >> 32)
                tint = 0xc0204060 if case == 'vibrancy-dark' else 0xc0d0e0f0
                put(pds + 0x20, '<I', 28)
                for i, (u, v) in enumerate(((0, 0), (1, 0), (0, 1), (0, 1), (1, 0), (1, 1))):
                    put(state + 0x300 + i * 28, '<4fI2f', 2 + u * 4, v * 2, 0, 1, tint, u, v)
                for y in range(2):
                    put(state + 0x500 + y * 64, '<4I', *([0xffffffff] * 4))
                expect_rect((2, 0, 6, 2), lambda x, y: blend_vibrancy(destination, tint))
    return bytes(blob), target_pages, bytes(expected), (width, height), draws, ta_count


def fixture(wait_context=False):
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
    if wait_context:
        # Wait before any doorbell: only the periodic path can consume the bind.
        code += move64(10, SHARED_9 + 0x920)
        code += [0xb9400000 | 10 << 5 | 6,                # ldr w6, [x10]
                 0x34000000 | ((-1 & 0x7ffff) << 5) | 6]  # cbz w6, .-4
    # Acknowledge as the driver does (0xffffff80011852c0), re-enable, then kick ring 1.
    code += store(8, 0x88, 1 << 20) + store(8, 0x188, 1 << 20) + store(9, 0xb00, 0x11)
    code += wait_irq(2)
    code += store(4, 4, 0xface) + [0x14000000]
    return struct.pack('<' + 'I' * len(code), *code)


def run_case(fill, context=True, replay=None, replay_thread=None):
    with tempfile.TemporaryDirectory(prefix='a7agx-', dir='/tmp') as directory:
        root = Path(directory)
        (root / 'fixture.bin').write_bytes(fixture(wait_context=context and fill is None))
        pbe = context and fill is None and replay is None
        if replay is not None:
            blob, target_pages, replay_expected, dims, draws, ta_count = replay_blob(replay)
        else:
            blob, target_pages = data_blob(context, pbe), []
        (root / 'data.bin').write_bytes(blob)
        cmd = [str(BINARY), '-machine', 's5l8960x,boot-entry=0x800000000,boot-args=0x800001000',
               '-kernel', str(root / 'fixture.bin'), '-accel', 'tcg', '-display', 'none', '-S',
               '-device', f'loader,file={root}/data.bin,addr={DATA:#x},force-raw=on',
               '-qmp', f'unix:{root}/qmp.sock,server=on,wait=off',
               '-d', 'unimp,guest_errors', '-D', str(root / 'trace.log')]
        env = os.environ.copy()
        env.pop('VPHONE_AGX_RT_FILL', None)
        env.pop('VPHONE_AGX_PBE_SEARCH', None)
        if replay is not None:
            # Exercise the default, even when the invoking shell sets replay.
            for key in ('VPHONE_AGX_REPLAY', 'VPHONE_AGX_FRAME_DUMP'):
                env.pop(key, None)
            if not replay:
                env['VPHONE_AGX_REPLAY'] = '0'
        if replay_thread is not None:
            env['VPHONE_AGX_REPLAY_THREAD'] = '1' if replay_thread else '0'
        if pbe:
            env['VPHONE_AGX_PBE_SEARCH'] = '1'
        if fill is not None:
            env['VPHONE_AGX_RT_FILL'] = fill
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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
                assert words(QUEUE + 0x30, 1) == [2], 'queue read index not advanced'
                assert words(SHARED_9 + 0x8e0, 1) == [2 if replay is not None else 1], 'host ring 1 read index not advanced'
                if context:
                    assert words(SHARED_9 + 0x920, 1) == [3], 'ring 3 read index not advanced'
                    assert words(PC + ((RT_GVA >> 30) & 0x3ff) * 4, 1) == [0], 'RT mapped in CAT_BASE0'
                assert words(SHARED + 0x50, 1) == [2], 'no completion event posted'
                assert words(SHARED + 0xc0 + 0x28, 1) == [1], 'completion event is not type 1'
                expected = 0xa5a5a5a5 if fill is None else int(fill, 16)
                for page in (RT_PA, RT_SECOND):
                    assert words(page, 4) == [expected] * 3 + [0xa5a5a5a5], 'RT pixels/padding'
                assert words(RT_PA + 0x1000, 1) == [0], 'physical hole overwritten'
                assert words(RT_LAST, 1) == [0xa5a5a5a5], 'probe-only end page overwritten'
                if replay is not None:
                    assert words(QUEUE + 0x830, 1) == [ta_count], 'TA queue not drained'
                    actual = bytearray()
                    for i, pa in enumerate(target_pages):
                        path = root / f'target-{i}.bin'
                        command(fp, 'pmemsave', {'val': pa, 'size': 0x1000, 'filename': str(path)})
                        actual.extend(path.read_bytes())
                    assert actual == replay_expected, f'{replay!r}: replay pixels/padding'
                command(fp, 'quit')
            assert proc.wait(timeout=5) == 0
            trace = (root / 'trace.log').read_text()
            hits = [line for line in trace.splitlines() if 'AGX PBE hit ' in line]
            if pbe:
                assert len(hits) == 1, hits
                assert (f'AGX PBE hit cmd-field +0x40 buf gva 0x{RT_GVA:x} '
                        'off 0x100 width 64 value 0x40047000 surface 0x400470000 shift 4') in hits[0], hits
            else:
                assert not hits, hits
            if replay is not None:
                replay_lines = [line for line in trace.splitlines() if 'AGX replay target ' in line]
                expected_replay = [f'AGX replay target 0x{REPLAY_TARGET:x} draws {draws}'] if replay else []
                assert replay_lines == expected_replay, replay_lines
                if not replay:
                    assert 'AGX replay' not in trace, trace
                assert (f'AGX RT pbe target cmd 0x{QUEUE_META + 0x300:x} '
                        f'surface 0x{REPLAY_TARGET:x} pa 0x{target_pages[0]:x}') in trace, trace
            rt_gva = REPLAY_TARGET if replay is not None else RT_GVA
            rt_pa = target_pages[0] if replay is not None else RT_PA
            dimensions = f'w {dims[0]} h {dims[1]}' if replay is not None else 'w 3 h 2'
            expected_line = (f'AGX RT queue 0x{QUEUE_META:x} cmd 0x{QUEUE_META + 0x300:x} '
                             f'ch {CONTEXT_ID} rt0 gva 0x{rt_gva:x} pa 0x{rt_pa:x} '
                             f'fmt 0x20007fc {dimensions} load 0x2')
            assert expected_line in trace, trace
            catalogue = CTX_PC if context else PC
            source = 'context slot' if context else 'CAT_BASE0 fallback'
            assert (f'context {CONTEXT_ID} catalogue 0x{catalogue:x} ({source})') in trace, trace
            if context:
                bind = f'AGX META: bind slot {CONTEXT_SLOT} catalogue 0x{CTX_PC:x}'
                assert bind in trace, trace
                assert trace.index(bind) < trace.index(expected_line), 'RT traced before bind'
                assert 'AGX META: host ring 3 skipped type 99 slot 0' in trace
                assert 'AGX META: host ring 3 skipped type 3 slot 16' in trace
            if replay is not None:
                return
            assert f'last gva 0x{RT_GVA + 0x2c5000:x} pa 0x{RT_LAST:x} mapped 1 contiguous 0' in trace
            assert f'offset 0x1000 gva 0x{RT_GVA + 0x1000:x} expected 0x{RT_PA + 0x1000:x} pa 0x{RT_SECOND:x}' in trace
        finally:
            if proc.poll() is None: proc.kill(); proc.wait(timeout=5)


def main():
    for context in (True, False):
        run_case(None, context)
        run_case('11223344', context)
    run_case(None, replay=True)
    run_case(None, replay=True, replay_thread=False)
    run_case(None, replay=False)
    for case in REPLAY_CASES:
        run_case(None, replay=case)
        print(f'PASS: AGX replay {case}: exact guest physical pixels and padding')
    print('PASS: AGX default replay draws exact solid/texture pixels; VPHONE_AGX_REPLAY=0 preserves target')
    print('PASS: AGX replay inline fallback (VPHONE_AGX_REPLAY_THREAD=0) matches threaded replay')
    print('PASS: AGX ring 3 no-doorbell context bind and CAT_BASE0 fallback')
    print('PASS: AGX RT trace; opt-in fill follows noncontiguous GPU mappings and preserves padding')
    print('PASS: AGX META boot program -> one type-0 alive entry on firmware ring 0 and AIC IRQ 84; empty program posts nothing')
    print('PASS: AGX doorbell 0x11 -> channel queue registered, type-4 stamp stored, type-1 completion event and IRQ 84')
    print('Synthetic AGX host-interface and quad replay contract only; no iOS rendering inferred.')


if __name__ == '__main__': main()
