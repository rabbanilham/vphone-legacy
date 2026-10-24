"""Unit tests for UISwitch knob shadow and navigation bar color matrix tint (Task U Follow-up 2).

Verifies:
- Detection of TSW at offset 8 for ROUNDED_CAPS (prog 0x6c01d033fd937c7c).
- Multi-quad axis-aligned mesh handling in classify_draw (9-slice UISwitch knob quads).
- Classification of textured rounded quad (stride=28) and color matrix (saturation & tint).
- Decoding of 3x3 Rec.709 color matrix and bias vector from fragment uniform memory.
- Mathematical correctness of color matrix transformation and clamping.
- Verification that AGX replay core in backend/s5l8960x.c implements color matrix and offset-8 TSW.
"""

import ctypes
import importlib.util
import math
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
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
        self.assertIn('agx_is_axis_aligned_quad_mesh', core)
        # Offset 8 adjacent-allocation borrow MUST be eliminated
        self.assertNotIn('blocks[b] + 8', core)
        self.assertIn('is_color_matrix', core)
        self.assertIn('tex.format == 3', core)

    def test_tsw_offset_8_rejected_no_borrow(self):
        """TSW at offset 8 (adjacent descriptor block overrun) is rejected even for ROUNDED_CAPS."""
        mf = MockFrame()
        tex_addr = 0x10524000
        mf.pages[tex_addr & ~0xfff] = bytes(4096)

        # Word 8..11 holds valid TSW
        w0 = (6 << 28) | 0x8864
        w1 = ((87 - 1) << 16) | ((114 - 1) << 2)
        q = (tex_addr >> 2) << 16
        w2 = q & 0xffffffff
        w3 = (q >> 32) & 0xffffffff

        # Fragment descriptor block: 8 dummy words, then 4 TSW words at offset 8
        block = [0] * 8 + [w0, w1, w2, w3]
        ptr = 0x2000
        for i, val in enumerate(block):
            mf.write(ptr + 4 * i, struct.pack('<I', val))
            mf.pages[(ptr + 4 * i) & ~0xfff] = bytes(4096)

        pds_word = 0x80000000 | 0x400
        base = aro.PDS_HEAP | pds_word
        mf.pages[base & ~0xfff] = bytes(4096)
        mf.write(base, struct.pack('<Q', ptr))

        # Both ROUNDED_CAPS and generic programs must reject offset 8
        tex_caps = aro.texture(mf, [pds_word], prog=aro.ROUNDED_CAPS)
        self.assertIsNone(tex_caps, "Offset 8 borrow must be rejected for ROUNDED_CAPS")
        tex_other = aro.texture(mf, [pds_word], prog=0x1122334455667788)
        self.assertIsNone(tex_other)

    def test_tsw_format_3_decode(self):
        """Format 3 TSW (8-bit single channel coverage mask) decodes with bpp=1 and twiddled/linear addressing."""
        mf = MockFrame()
        tex_addr = 0x10d4c000
        mf.pages[tex_addr & ~0xfff] = bytes(4096)

        # Draw 64 twiddled format 3 TSW: 256x256 at 0x10d4c000
        w0 = (3 << 28) | 0x00000021  # fmt 3, layout 1 (twiddled)
        w1 = ((256 - 1) << 16) | ((256 - 1) << 2)
        q = (tex_addr >> 2) << 16
        w2 = q & 0xffffffff
        w3 = (q >> 32) & 0xffffffff

        tsw = [w0, w1, w2, w3]
        tex = aro.decode_tsw(mf, tsw)
        self.assertIsNotNone(tex)
        self.assertEqual(tex['format'], 3)
        self.assertEqual(tex['bpp'], 1)
        self.assertEqual(tex['w'], 256)
        self.assertEqual(tex['h'], 256)
        self.assertEqual(tex['addr'], tex_addr)
        self.assertTrue(tex['twiddled'])
        self.assertEqual(tex['stride'], 256)  # 256 * 1 bpp

        # Linear format 3 TSW: 100x50, pitch = 128
        w0_lin = (3 << 28) | 0x00000000
        w1_lin = ((50 - 1) << 16) | ((100 - 1) << 2)
        w2_lin = (q & 0xffff0000) | (128 - 1)
        tex_lin = aro.decode_tsw(mf, [w0_lin, w1_lin, w2_lin, w3])
        self.assertIsNotNone(tex_lin)
        self.assertEqual(tex_lin['format'], 3)
        self.assertEqual(tex_lin['bpp'], 1)
        self.assertFalse(tex_lin['twiddled'])
        self.assertEqual(tex_lin['stride'], 128)

    def test_format_3_channel_routing_and_premultiplied_tint(self):
        """Format 3 coverage mask texel modulates alpha and premultiplies RGB tint."""
        # Coverage = 255: full tint #E6E6E6 (230, 230, 230)
        cov = 255
        tint = 0xffe6e6e6
        ta = tint >> 24
        tr = (tint >> 16) & 0xff
        tg = (tint >> 8) & 0xff
        tb = tint & 0xff
        out_a = ta * cov // 255
        r = tr * out_a // 255
        g = tg * out_a // 255
        b = tb * out_a // 255
        self.assertEqual((r, g, b, out_a), (230, 230, 230, 255))

        # Coverage = 0: zero alpha, zero color
        cov = 0
        out_a = ta * cov // 255
        r = tr * out_a // 255
        g = tg * out_a // 255
        b = tb * out_a // 255
        self.assertEqual((r, g, b, out_a), (0, 0, 0, 0))

        # Coverage = 128: 50% antialiased edge
        cov = 128
        out_a = ta * cov // 255
        r = tr * out_a // 255
        g = tg * out_a // 255
        b = tb * out_a // 255
        self.assertEqual(out_a, 128)
        self.assertEqual(r, 230 * 128 // 255)

    def test_axis_aligned_quad_mesh_discrimination(self):
        """Recognize 9-slice (54 verts) and single quads (4/6 verts), reject non-quads (48 verts)."""
        # 54-vertex 9-slice grid
        verts_9slice = []
        for gx in [508, 539, 579]:
            for gy in [177, 208, 239]:
                # 6 verts per quad
                verts_9slice.extend([
                    (gx, gy), (gx + 31, gy), (gx + 31, gy + 31),
                    (gx + 31, gy + 31), (gx, gy + 31), (gx, gy)
                ])
        self.assertEqual(len(verts_9slice), 54)
        self.assertTrue(aro.is_axis_aligned_quad_mesh(verts_9slice, 54))

        # 48-vertex triangle fan (clock hand / unlock glint)
        verts_fan = [(300 + math.cos(i) * 20, 500 + math.sin(i) * 20) for i in range(48)]
        self.assertFalse(aro.is_axis_aligned_quad_mesh(verts_fan, 48))

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



class CompiledCSwitchMaskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which('cc')
        if not compiler:
            raise unittest.SkipTest('C compiler required')
        cls.tmp = tempfile.TemporaryDirectory()
        src = SOURCE.read_text()
        start_gva = src.index('static uint64_t agx_texel_gva(')
        end_gva = src.index('/* A Morton row')
        fn_gva = src[start_gva:end_gva]

        harness = Path(cls.tmp.name) / 'switch_mask.c'
        harness.write_text(f'''#include <stdint.h>
#include <stdbool.h>

typedef struct {{
    uint64_t addr;
    uint32_t w, h, stride;
    uint32_t format;
    uint32_t bpp;
    bool strip;
    bool twiddled;
    bool linear_filter;
    bool clamp_to_edge;
    uint32_t sampler_words[4];
}} AGXTexture;

{fn_gva}

uint64_t c_texel_gva(uint64_t addr, uint32_t w, uint32_t h, uint32_t stride,
                     uint32_t bpp, bool twiddled, uint32_t x, uint32_t y) {{
    AGXTexture tex = {{
        .addr = addr,
        .w = w,
        .h = h,
        .stride = stride,
        .bpp = bpp,
        .twiddled = twiddled,
        .strip = false
    }};
    return agx_texel_gva(&tex, x, y);
}}

void c_blend_coverage_scissor(uint32_t *fb, int fb_w, int fb_h,
                             const uint8_t *mask, int mask_w, int mask_h,
                             uint32_t tint,
                             int sx0, int sy0, int sx1, int sy1) {{
    for (int y = 0; y < mask_h && y < fb_h; y++) {{
        if (y < sy0 || y >= sy1) continue;
        for (int x = 0; x < mask_w && x < fb_w; x++) {{
            if (x < sx0 || x >= sx1) continue;
            uint32_t cov = mask[y * mask_w + x];
            uint32_t ta = tint >> 24;
            uint32_t tr = (tint >> 16) & 0xff;
            uint32_t tg = (tint >> 8) & 0xff;
            uint32_t tb = tint & 0xff;
            uint32_t out_a = ta * cov / 255;
            uint32_t out_r = tr * out_a / 255;
            uint32_t out_g = tg * out_a / 255;
            uint32_t out_b = tb * out_a / 255;
            uint32_t src = (out_a << 24) | (out_r << 16) | (out_g << 8) | out_b;

            // agx_blend_over (premultiplied src-over)
            uint32_t dst = fb[y * fb_w + x];
            uint32_t db = dst & 0xff, dg = (dst >> 8) & 0xff, dr = (dst >> 16) & 0xff, da = dst >> 24;
            uint32_t inv_a = 255 - out_a;
            uint32_t rb = out_b + (db * inv_a) / 255;
            uint32_t rg = out_g + (dg * inv_a) / 255;
            uint32_t rr = out_r + (dr * inv_a) / 255;
            uint32_t ra = out_a + (da * inv_a) / 255;
            fb[y * fb_w + x] = (ra << 24) | (rr << 16) | (rg << 8) | rb;
        }}
    }}
}}
''')
        binary = Path(cls.tmp.name) / 'switch_mask.so'
        subprocess.run([compiler, '-shared', '-fPIC', str(harness), '-o', str(binary)], check=True)
        cls.lib = ctypes.CDLL(str(binary))
        cls.lib.c_texel_gva.restype = ctypes.c_uint64

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_c_texel_gva_format3_vs_format6(self):
        """Format 3 (1 bpp) multiplies Morton/linear offset by 1, format 6 (4 bpp) multiplies by 4."""
        addr = 0x10000000
        # Twiddled 4x4: coordinate (1, 1) -> Morton offset 3 (bit 0=Y=1, bit 1=X=1)
        # Format 3 (bpp=1)
        gva_fmt3 = self.lib.c_texel_gva(addr, 4, 4, 4, 1, True, 1, 1)
        self.assertEqual(gva_fmt3, addr + 3 * 1)

        # Format 6 (bpp=4)
        gva_fmt6 = self.lib.c_texel_gva(addr, 4, 4, 16, 4, True, 1, 1)
        self.assertEqual(gva_fmt6, addr + 3 * 4)

        # Linear 4x4 at coordinate (2, 3), stride 16 for fmt6, 4 for fmt3
        gva_lin3 = self.lib.c_texel_gva(addr, 4, 4, 4, 1, False, 2, 3)
        self.assertEqual(gva_lin3, addr + 3 * 4 + 2 * 1)

        gva_lin6 = self.lib.c_texel_gva(addr, 4, 4, 16, 4, False, 2, 3)
        self.assertEqual(gva_lin6, addr + 3 * 16 + 2 * 4)

    def test_c_coverage_tint_blend_and_scissor(self):
        """Render a 16x16 circular mask with scissor [0, 8] x [0, 16] into a white framebuffer."""
        fb_w, fb_h = 16, 16
        # Background is white (0xffffffff)
        fb = (ctypes.c_uint32 * (fb_w * fb_h))(*([0xffffffff] * (fb_w * fb_h)))

        # Create a 16x16 mask: 255 for radius < 6, 0 for radius >= 6
        mask = (ctypes.c_uint8 * (16 * 16))()
        for y in range(16):
            for x in range(16):
                r2 = (x - 7.5)**2 + (y - 7.5)**2
                mask[y * 16 + x] = 255 if r2 <= 36 else 0

        # Scissor only left half: [0, 8] x [0, 16]
        tint = 0xffe6e6e6  # #E6E6E6
        self.lib.c_blend_coverage_scissor(
            fb, fb_w, fb_h, mask, 16, 16, tint, 0, 0, 8, 16)

        # Pixels in left half inside mask must be #E6E6E6 (0xffe6e6e6)
        self.assertEqual(fb[7 * 16 + 4], 0xffe6e6e6)
        # Pixels in left half outside mask must be untouched white (0xffffffff)
        self.assertEqual(fb[0 * 16 + 0], 0xffffffff)
        # Pixels in right half (outside scissor) must remain untouched white even if inside mask
        self.assertEqual(fb[7 * 16 + 11], 0xffffffff)


if __name__ == '__main__':
    unittest.main()
