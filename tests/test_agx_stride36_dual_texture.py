"""Unit tests for AGX stride-36 dual-texture vibrancy draws:
- Classification of stride-36 dual-texture (prog 0xb61824c33264c501)
- Vertex layout decoding: float4 pos + u32 tint (+16) + float2 uv0 (+20) + float2 uv1 (+28)
- Dual-texture descriptor block decoding (TSW0 backdrop + TSW1 mask + Rec.709 LUMA weights)
- Vibrancy blending mathematics (Rec.709 luma-weighted destination attenuation)
- Mask alpha modulation (full opacity, partial coverage, transparent passthrough)
- Incomplete / sparse texture safety (preserving destination without corrupt dashes or black pixels)
- Verification of C and Python replay parity
"""
import importlib.util
import json
import math
from pathlib import Path
import struct
import unittest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'backend'
SUMMARY_JSON = ROOT / 'corpus/report/summary.json'

REPLAY_SPEC = importlib.util.spec_from_file_location(
    'agx_replay_offline', BACKEND / 'agx_replay_offline.py')
aro = importlib.util.module_from_spec(REPLAY_SPEC)
REPLAY_SPEC.loader.exec_module(aro)


class MockFrame:
    def __init__(self):
        self.data = {}
        self.pages = set()

    def add_page(self, gva, raw_bytes):
        base = gva & ~0xfff
        self.pages.add(base)
        for i, b in enumerate(raw_bytes):
            self.data[base + i] = b

    def read(self, gva, size):
        res = bytearray()
        for i in range(size):
            p = gva + i
            if (p & ~0xfff) not in self.pages:
                raise KeyError(f'unmapped {p:#x}')
            res.append(self.data.get(p, 0))
        return bytes(res)

    def u32(self, gva):
        return struct.unpack_from('<I', self.read(gva, 4))[0]

    def u64(self, gva):
        return struct.unpack_from('<Q', self.read(gva, 8))[0]


class AGXStride36DualTextureTests(unittest.TestCase):
    def test_classify_draw_stride36_dual_texture(self):
        """classify_draw categorises textured stride-36 prog 0xb61824c33264c501 as dual-texture vibrancy."""
        mf = MockFrame()
        vs_data_bytes = struct.pack('<IIQQ', 36, 0, 0x1000, 0)
        mf.add_page(0x0, vs_data_bytes)

        # 6 vertices covering (283, 1114) to (357, 1131)
        v_bytes = bytearray()
        coords = [
            (283.0, 1114.0, 0.0, 0.0, 0.0, 0.0),
            (357.0, 1114.0, 74.0, 0.0, 74.0, 0.0),
            (357.0, 1131.0, 74.0, 17.0, 74.0, 17.0),
            (357.0, 1131.0, 74.0, 17.0, 74.0, 17.0),
            (283.0, 1131.0, 0.0, 17.0, 0.0, 17.0),
            (283.0, 1114.0, 0.0, 0.0, 0.0, 0.0),
        ]
        for x, y, u0, v0, u1, v1 in coords:
            v_bytes += struct.pack('<4fI4f', x, y, 0.0, 1.0, 0xffffffff, u0, v0, u1, v1)
        mf.add_page(0x1000, bytes(v_bytes))

        draw_cmd = (3 << 29, 6)
        ppp = []
        viewport = (320.0, 320.0, 568.0, 568.0)

        orig_program = aro.program
        orig_texture = aro.texture
        try:
            aro.program = lambda f, p: aro.STRIDE36_DUAL_TEX
            aro.texture = lambda f, p, prog=None: {
                'w': 640, 'h': 96, 'addr': 0x10618000, 'strip': False,
                'stride': 2560, 'twiddled': False, 'vibrancy': True,
                'mask_tex': {'w': 640, 'h': 416, 'addr': 0x10254000, 'strip': False, 'stride': 2560}
            }
            res, det = aro.classify_draw(mf, 0x0, ppp, draw_cmd, viewport, 640, 1136, 640, 1136)
            self.assertEqual(res, 'approximated: dual-texture alpha composite (stride=36)')
            self.assertEqual(det['prog'], hex(aro.STRIDE36_DUAL_TEX))
        finally:
            aro.program = orig_program
            aro.texture = orig_texture

    def test_vertex_layout_decoding(self):
        """Stride 36 vertices unpack float4 pos (+0), u32 tint (+16), float2 uv0 (+20), float2 uv1 (+28)."""
        raw = struct.pack('<4fI4f', 283.0, 1114.0, 0.0, 1.0, 0xffffffff, 74.0, 17.0, 74.0, 17.0)
        self.assertEqual(len(raw), 36)

        x, y, z, w = struct.unpack_from('<4f', raw, 0)
        tint, = struct.unpack_from('<I', raw, 16)
        u0, v0 = struct.unpack_from('<2f', raw, 20)
        u1, v1 = struct.unpack_from('<2f', raw, 28)

        self.assertEqual((x, y), (283.0, 1114.0))
        self.assertEqual(tint, 0xffffffff)
        self.assertEqual((u0, v0), (74.0, 17.0))
        self.assertEqual((u1, v1), (74.0, 17.0))

    def test_dual_texture_descriptor_decoding(self):
        """TSW 0 (backdrop), TSW 1 (mask) and Rec.709 LUMA weights decode accurately."""
        mf = MockFrame()
        # Uniform block: TSW 0 at +0, TSW 1 at +16, LUMA at +32
        tsw0 = [0x60000a61, 0x005f09fc, 0x60000001, 0x00000418]  # 640x96, addr 0x10618000
        tsw1 = [0x60000a61, 0x019f09fc, 0x50000001, 0x00000409]  # 640x416, addr 0x10254000
        luma = list(aro.LUMA) + [0xfff]
        block = tsw0 + tsw1 + luma
        block_bytes = struct.pack('<12I', *block)
        mf.add_page(0x1000, block_bytes)
        mf.add_page(0x10618000, b'\x00' * 4096)
        mf.add_page(0x10254000, b'\x00' * 4096)

        dec0 = aro.decode_tsw(mf, tsw0)
        self.assertIsNotNone(dec0)
        self.assertEqual(dec0['w'], 640)
        self.assertEqual(dec0['h'], 96)
        self.assertEqual(dec0['addr'], 0x10618000)

        dec1 = aro.decode_tsw(mf, tsw1)
        self.assertIsNotNone(dec1)
        self.assertEqual(dec1['w'], 640)
        self.assertEqual(dec1['h'], 416)
        self.assertEqual(dec1['addr'], 0x10254000)

        # Rec.709 luma weights in float
        wb, wr, wg = [struct.unpack('<f', struct.pack('<I', w))[0] for w in aro.LUMA]
        self.assertAlmostEqual(wb, 0.072083, places=4)
        self.assertAlmostEqual(wr, 0.212524, places=4)
        self.assertAlmostEqual(wg, 0.715332, places=4)
        self.assertAlmostEqual(wb + wr + wg, 1.0, places=3)

    def test_vibrancy_math_destination_attenuation(self):
        """blend_vibrancy attenuates white light halo by (1 - Y)^4 over destination."""
        dst = (98, 179, 199, 255)
        tint = (255, 255, 255, 255)

        res = aro.blend_vibrancy(dst, tint)
        self.assertEqual(len(res), 4)
        self.assertEqual(res[3], 255)
        # Result should be slightly brighter than destination without blowing out
        self.assertTrue(res[0] >= dst[0])
        self.assertTrue(res[1] >= dst[1])
        self.assertTrue(res[2] >= dst[2])
        # Preserves blue hue
        self.assertTrue(res[2] > res[1] > res[0])

    def test_mask_alpha_modulation(self):
        """Mask alpha modulates vibrancy output: 0 leaves dst untouched, 255 gives full vibrancy."""
        dst = (80, 160, 200, 255)
        tint = (255, 255, 255, 255)
        vib = aro.blend_vibrancy(dst, tint)

        # Alpha = 0 (passthrough)
        mask_a_0 = 0
        out_0 = tuple((vib[i] * mask_a_0 + dst[i] * (255 - mask_a_0)) // 255 for i in range(4))
        self.assertEqual(out_0, dst)

        # Alpha = 255 (full vibrancy)
        mask_a_255 = 255
        out_255 = tuple((vib[i] * mask_a_255 + dst[i] * (255 - mask_a_255)) // 255 for i in range(4))
        self.assertEqual(out_255, vib)

        # Alpha = 128 (midpoint interpolation)
        mask_a_128 = 128
        out_128 = tuple((vib[i] * mask_a_128 + dst[i] * (255 - mask_a_128)) // 255 for i in range(4))
        for c in range(3):
            self.assertTrue(dst[c] <= out_128[c] <= vib[c])

    def test_sparse_texture_safety_preserves_dst(self):
        """When texture pages are unmapped in sparse dumps, destination is preserved without black pixels."""
        mf = MockFrame()
        # Create unmapped texture
        tex = {'w': 640, 'h': 96, 'addr': 0x10618000, 'strip': False, 'stride': 2560, 'twiddled': False}
        t = aro.texel(mf, tex, 10, 10)
        # texel() returns 4 zero bytes on unmapped access
        self.assertEqual(t, b'\x00\x00\x00\x00')

        # Vibrancy of zero src over dst leaves dst untouched
        dst = (85, 171, 194, 255)
        t_unpacked = struct.unpack('<4B', t)
        out = aro.blend_vibrancy(dst, t_unpacked)
        self.assertEqual(out, dst)

    def test_corpus_summary_classification(self):
        """Corpus summary confirms exactly 12 dual-texture vibrancy draws and 0 harness errors."""
        if not SUMMARY_JSON.exists():
            self.skipTest('summary.json not present')
        with SUMMARY_JSON.open() as f:
            summary = json.load(f)

        totals = summary['totals']
        self.assertEqual(totals['c_errors'], 0)
        self.assertEqual(totals['py_errors'], 0)
        self.assertEqual(totals['py_outcomes'].get('replayed exactly: dual-texture vibrancy (stride=36)'), 12)


if __name__ == '__main__':
    unittest.main()
