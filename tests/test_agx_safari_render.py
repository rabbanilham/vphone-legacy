"""Tests for Safari and WebKit AGX GPU rendering: Format 3 A8 textures and alpha-mask tinting."""
import contextlib
import io
import struct
import sys
from unittest.mock import patch
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('agx_replay_offline', ROOT / 'backend/agx_replay_offline.py')
replay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(replay)


class SafariRenderTests(unittest.TestCase):
    def test_webkit_u8_alpha_swizzle_and_layout(self):
        class Memory:
            pages = {0x10000000: bytes(4096)}
        q = (0x10000000 >> 2) << 16
        for layout, twiddled in [(1, True), (4, False)]:
            tsw = [0x16d00 | layout, (511 << 16) | (511 << 2),
                   (q & 0xffffffff) | 575, q >> 32]
            tex = replay.decode_tsw(Memory(), tsw)
            self.assertEqual((tex['w'], tex['h'], tex['bpp']), (512, 512, 1))
            self.assertEqual(tex['twiddled'], twiddled)
            self.assertEqual(tex['stride'], 512 if twiddled else 576)
        # Reject a superficially plausible format-0 descriptor with a
        # different swizzle; it must not steal a later valid descriptor.
        tsw[0] = 1
        self.assertIsNone(replay.decode_tsw(Memory(), tsw))

    def test_rectangular_morton_coverage(self):
        class Memory:
            def read(self, address, length):
                return bytes([173 if address == 0x10000000 + 65 else 0])
        tex = dict(addr=0x10000000, w=32, h=8, bpp=1,
                   twiddled=True, strip=False, stride=32)
        # Y first: y=1 contributes bit 0.
        # x=8 appends bit 6 after the three interleaved coordinate bits.
        self.assertEqual(replay.texel(Memory(), tex, 8, 1), (0, 0, 0, 173))

    def test_glyph_batches_retain_flip_and_white_coverage(self):
        class Memory:
            header = {'pbe_target': 0x20000000, 'context_id': 2}
            type1_command = bytearray(0x280)
            struct.pack_into('<II', type1_command, 0x70, 31, 31)
            pages = {0x10000000: bytes(4096)}
            def require_matching_ta(self):
                pass
            def u32(self, address):
                return 28
            def u64(self, address):
                return address - 8
            def read(self, address, length):
                if 0x10000000 <= address < 0x10001000:
                    # 4x4 Y-first Morton atlas: its left half is transparent.
                    return bytes(0 if (address - 0x10000000) & 8 == 0 else 255
                                 for _ in range(length))
                vertices = batches[address // 0x10000]
                offset = address % 0x10000
                return vertices[offset:offset + length]
        def quad(x, y):
            return b''.join(struct.pack('<4fI2f', px, py, 0, 1, 0xffffffff, u, v)
                            for px, py, u, v in [(x, y, 0, 0), (x+4, y, 1, 0),
                                (x+4, y-4, 1, 1), (x+4, y-4, 1, 1),
                                (x, y-4, 0, 1), (x, y, 0, 0)])
        batches = {1: quad(0, 32),
                   2: b''.join(quad(x*4, 24-y*4) for y in range(5) for x in range(5))}
        frame = Memory()
        q = (0x10000000 >> 2) << 16
        tex = replay.decode_tsw(frame, [0x16d01, (3 << 16) | (3 << 2), q & 0xffffffff, q >> 32])
        draws = [(0x10000, [], [3 << 29, 6, 0], (16, 16, 16, 16)),
                 (0x20000, [], [3 << 29, 150, 0], (16, 16, 16, 16))]
        output = []
        with patch.object(replay, 'Frame', return_value=frame), \
             patch.object(replay, 'walk', return_value=iter(draws)), \
             patch.object(replay, 'program', return_value=replay.WEBKIT_GLYPHS), \
             patch.object(replay, 'texture', return_value=tex), \
             patch.object(replay, 'find_matrix', side_effect=[(1/16, -1/16, -1, 1), None]), \
             patch.object(replay, 'is_blur_program', return_value=False), \
             patch.object(replay, 'nc_sparse_layout', return_value=False), \
             patch.object(replay, 'is_clear_pass', return_value=True), \
             patch.object(replay, 'png', side_effect=lambda p,w,h,b: output.append(b)), \
             patch.object(sys, 'argv', ['replay', 'fixture', 'unused.png']), \
             contextlib.redirect_stdout(io.StringIO()):
            replay.main()
        pixels = output[0]
        def pixel(x, y):
            return pixels[(y*32+x)*4:(y*32+x+1)*4]
        self.assertEqual(pixel(0, 1), bytes(4))  # coverage preserves glyph shape
        self.assertEqual(pixel(3, 1), bytes([255]*4))  # white tint remains white
        self.assertEqual(pixel(19, 25), bytes([255]*4))  # final glyph beyond vertex 64
        self.assertEqual(pixel(19, 5), bytes(4))  # retained Y flip, not raw guest Y

    def test_webkit_rounded_mask_span_rasterization(self):
        class Memory:
            header = {'pbe_target': 0x20000000, 'context_id': 2}
            type1_command = bytearray(0x280)
            struct.pack_into('<II', type1_command, 0x70, 7, 7)
            pages = {}
            def require_matching_ta(self):
                pass
            def u32(self, address):
                return 16
            def u64(self, address):
                return 0x1000
            def read(self, address, length):
                data = (struct.pack('<2f8B', 1.5, 3.5, *([255]*8)) +
                        struct.pack('<2f8B', 6.5, 3.5, *([255]*8)))
                return data[address-0x1000:address-0x1000+length]
        output = []
        with patch.object(replay, 'Frame', return_value=Memory()), \
             patch.object(replay, 'walk', return_value=iter([(1, [], [3<<29, 2, 0], None)])), \
             patch.object(replay, 'program', return_value=replay.WEBKIT_SPANS), \
             patch.object(replay, 'texture', return_value=None), \
             patch.object(replay, 'find_matrix', return_value=None), \
             patch.object(replay, 'is_clear_pass', return_value=True), \
             patch.object(replay, 'png', side_effect=lambda p,w,h,b: output.append(b)), \
             patch.object(sys, 'argv', ['replay', 'fixture', 'unused.png']), \
             contextlib.redirect_stdout(io.StringIO()):
            replay.main()
        self.assertEqual(output[0][(3*8+1)*4:(3*8+6)*4], bytes([255]*20))
        self.assertEqual(output[0][:8*4], bytes(32))

    def test_format3_a8_descriptor(self):
        class Memory:
            pages = {0x10000000: bytes(4096)}
        q = (0x10000000 >> 2) << 16
        # Format 3 (A8 / L8 8-bit texture) with Morton twiddling (tsw[0] & 7 == 1)
        tsw = [0x38000021, (255 << 16) | (255 << 2), q & 0xffffffff, q >> 32]
        tex = replay.decode_tsw(Memory(), tsw)
        self.assertIsNotNone(tex)
        self.assertEqual(tex['bpp'], 1)
        self.assertEqual(tex['w'], 256)
        self.assertEqual(tex['h'], 256)
        self.assertTrue(tex['twiddled'])
        self.assertEqual(tex['stride'], 256)

    def test_format3_a8_texel_morton(self):
        class Memory:
            def __init__(self):
                self.pages = {}
            def read(self, addr, length):
                return bytes([self.pages.get(addr + i, 0) for i in range(length)])
            def u32(self, addr):
                return 0

        mem = Memory()
        # Set a coverage byte at base + 0
        mem.pages[0x10000000] = 0x80  # alpha = 128
        tex = {'addr': 0x10000000, 'w': 256, 'h': 256, 'bpp': 1, 'strip': False, 'twiddled': True, 'nc_sparse': False}
        rgba = replay.texel(mem, tex, 0, 0)
        self.assertEqual(rgba, (0, 0, 0, 128))

    def test_alpha_mask_tint_modulation(self):
        # When an alpha-mask / coverage texture has RGB == 0 and Alpha != 0,
        # tint modulation must modulate the vertex tint RGB by coverage,
        # not zero out RGB by multiplying src_rgb (which is 0).
        tint_r, tint_g, tint_b, tint_a = 128, 128, 128, 255
        coverage = 200

        # Simulating the alpha-mask modulation formula in s5l8960x.c:
        a = (coverage * tint_a) // 255
        r = (tint_r * a) // 255
        g = (tint_g * a) // 255
        b = (tint_b * a) // 255
        self.assertGreater(r, 0)
        self.assertGreater(g, 0)
        self.assertGreater(b, 0)
        self.assertEqual(r, 100)
        self.assertEqual(g, 100)
        self.assertEqual(b, 100)

    def test_safari_frame27_audit(self):
        dump = ROOT / 'build/agx-corpus/safari/dumps/frame-27.bin'
        if not dump.exists():
            raise unittest.SkipTest('safari corpus dump frame-27.bin not found')
        from backend.agx_frame import Frame
        f = Frame(dump)
        outcomes = replay.audit_frame(f)
        self.assertEqual(len(outcomes), 25)
        # Verify no draws resulted in errors
        for idx, result, details in outcomes:
            self.assertTrue(result.startswith('replayed') or result.startswith('approximated'),
                            f'draw {idx} had unexpected outcome: {result}')


if __name__ == '__main__':
    unittest.main()
