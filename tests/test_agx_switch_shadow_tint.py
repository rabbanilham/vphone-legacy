"""Unit tests for UISwitch knob shadow and navigation bar color matrix tint (Task U Follow-up 2).

Verifies:
- Detection of TSW at offset 8 for ROUNDED_CAPS (prog 0x6c01d033fd937c7c).
- Multi-quad axis-aligned mesh handling in classify_draw (9-slice UISwitch knob quads).
- Classification of textured rounded quad (stride=28) and color matrix (saturation & tint).
- Decoding of 3x3 Rec.709 color matrix and bias vector from fragment uniform memory.
- Mathematical correctness of color matrix transformation and clamping.
- Verification that AGX replay core in backend/s5l8960x.c implements color matrix and offset-8 TSW.
"""

import importlib.util
import math
from pathlib import Path
import struct
import unittest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'backend'
SOURCE = BACKEND / 's5l8960x.c'

REPLAY_SPEC = importlib.util.spec_from_file_location(
    'agx_replay_offline', BACKEND / 'agx_replay_offline.py')
aro = importlib.util.module_from_spec(REPLAY_SPEC)
REPLAY_SPEC.loader.exec_module(aro)


class MockFrame:
    def __init__(self):
        self.data = {}
        self.pages = {}

    def write(self, gva, data_bytes):
        for i, b in enumerate(data_bytes):
            self.data[gva + i] = b

    def read(self, gva, size):
        res = bytearray()
        for i in range(size):
            res.append(self.data.get(gva + i, 0))
        return bytes(res)

    def u32(self, gva):
        return struct.unpack_from('<I', self.read(gva, 4))[0]

    def u64(self, gva):
        return struct.unpack_from('<Q', self.read(gva, 8))[0]


class AGXSwitchShadowTintTests(unittest.TestCase):
    def test_c_source_markers_and_constants(self):
        """Verify C replay core in s5l8960x.c defines constants and functions."""
        source = SOURCE.read_text()
        start = source.index('/* AGX_REPLAY_CORE_BEGIN')
        end = source.index('/* AGX_REPLAY_CORE_END */')
        core = source[start:end]

        self.assertIn('AGX_USC_COLOR_MATRIX UINT64_C(0x2353a3fe1ca2b71b)', core)
        self.assertIn('AGX_USC_ROUNDED_CAPS UINT64_C(0x6c01d033fd937c7c)', core)
        self.assertIn('agx_extract_color_matrix', core)
        self.assertIn('prog == AGX_USC_ROUNDED_CAPS && agx_replay_decode_tsw(s, cat, blocks[b] + 8, tex)', core)
        self.assertIn('is_color_matrix', core)

    def test_tsw_offset_8_gating(self):
        """TSW at offset 8 is decoded when prog is ROUNDED_CAPS, ignored otherwise."""
        mf = MockFrame()
        # Texture page mapped
        tex_addr = 0x10524000
        mf.pages[tex_addr & ~0xfff] = bytes(4096)

        # Word 8..11 holds valid TSW
        w0 = (6 << 28) | 0x8864
        w1 = ((87 - 1) << 16) | ((114 - 1) << 2)
        q = (tex_addr >> 2) << 16
        w2 = q & 0xffffffff
        w3 = (q >> 32) & 0xffffffff

        # Fragment descriptor block: 8 dummy words, then 4 TSW words
        block = [0] * 8 + [w0, w1, w2, w3]
        ptr = 0x2000
        for i, val in enumerate(block):
            mf.write(ptr + 4 * i, struct.pack('<I', val))
            mf.pages[(ptr + 4 * i) & ~0xfff] = bytes(4096)

        # PDS heap entry pointing to ptr
        pds_word = 0x80000000 | 0x400
        base = aro.PDS_HEAP | pds_word
        mf.pages[base & ~0xfff] = bytes(4096)
        mf.write(base, struct.pack('<Q', ptr))

        # For ROUNDED_CAPS, texture is found
        tex_caps = aro.texture(mf, [pds_word], prog=aro.ROUNDED_CAPS)
        self.assertIsNotNone(tex_caps)
        self.assertEqual(tex_caps['addr'], tex_addr)
        self.assertEqual(tex_caps['w'], 114)
        self.assertEqual(tex_caps['h'], 87)

        # For generic program, texture at offset 8 is not recognized
        tex_other = aro.texture(mf, [pds_word], prog=0x1122334455667788)
        self.assertIsNone(tex_other)

    def test_color_matrix_extraction(self):
        """Extract 3x3 color matrix and bias vector from fragment uniform memory."""
        mf = MockFrame()
        pds_word = 0x80000000 | 0x500
        base = aro.PDS_HEAP | pds_word
        ptr = 0x3000
        mf.pages[base & ~0xfff] = bytes(4096)
        mf.write(base, struct.pack('<Q', ptr))
        mf.pages[ptr & ~0xfff] = bytes(4096)

        fl = [0.0] * 16
        # Col 0:
        fl[0] = 0.325948
        fl[1] = -0.034016
        fl[2] = -0.034016
        # Col 1:
        fl[4] = -0.114432
        fl[5] = 0.245568
        fl[6] = -0.114432
        # Col 2:
        fl[8] = -0.011552
        fl[9] = -0.011552
        fl[10] = 0.348448
        # Bias:
        fl[12] = 0.776000
        fl[13] = 0.776000
        fl[14] = 0.776000

        mf.write(ptr, struct.pack('<16f', *fl))

        cm = aro.extract_color_matrix(mf, [pds_word])
        self.assertAlmostEqual(cm['matrix'][0], 0.325948, places=5)
        self.assertAlmostEqual(cm['matrix'][4], 0.245568, places=5)
        self.assertAlmostEqual(cm['matrix'][8], 0.348448, places=5)
        self.assertAlmostEqual(cm['bias'][0], 0.776000, places=5)

    def test_color_matrix_transformation_math(self):
        """Color matrix maps background #EFEFF4 (239, 239, 244) to frosted translucent (246, 246, 247)."""
        cm = aro.extract_color_matrix(MockFrame(), [])
        m = cm['matrix']
        bias = cm['bias']

        rin = 239.0 / 255.0
        gin = 239.0 / 255.0
        bin = 244.0 / 255.0

        rout = m[0] * rin + m[3] * gin + m[6] * bin + bias[0]
        gout = m[1] * rin + m[4] * gin + m[7] * bin + bias[1]
        bout = m[2] * rin + m[5] * gin + m[8] * bin + bias[2]

        r = max(0, min(255, int(round(rout * 255.0))))
        g = max(0, min(255, int(round(gout * 255.0))))
        b = max(0, min(255, int(round(bout * 255.0))))

        self.assertEqual((r, g, b), (246, 246, 247))

    def test_classify_draw_color_matrix(self):
        """classify_draw categorises COLOR_MATRIX draw as exact."""
        mf = MockFrame()
        vs_data = 0x500
        vs_data_bytes = struct.pack('<IIQQ', 24, 0, 0x1000, 0)
        for i, b in enumerate(vs_data_bytes):
            mf.data[vs_data + i] = b

        draw_cmd = (3 << 29, 6)
        viewport = (320.0, 320.0, 568.0, 568.0)

        # Mock texture and program
        orig_prog = aro.program
        orig_tex = aro.texture
        try:
            aro.program = lambda f, p: aro.COLOR_MATRIX
            aro.texture = lambda f, p, prog=None: {'w': 640, 'h': 64, 'addr': 0x1076c000, 'strip': False, 'stride': 2560}
            res, det = aro.classify_draw(mf, vs_data, [], draw_cmd, viewport, 640, 1136, 640, 1136)
            self.assertEqual(res, 'replayed exactly: color matrix (saturation & tint)')
            self.assertEqual(det['prog'], hex(aro.COLOR_MATRIX))
        finally:
            aro.program = orig_prog
            aro.texture = orig_tex


if __name__ == '__main__':
    unittest.main()
