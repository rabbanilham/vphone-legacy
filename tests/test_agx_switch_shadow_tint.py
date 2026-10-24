"""Unit tests for UISwitch knob shadow and format-3 coverage mask fidelity.

Verifies:
- Rejection of artificial offset-8 adjacent-descriptor borrow for ROUNDED_CAPS and generic programs.
- Decoding of Format-3 (single-channel 8-bit coverage mask, 1 bpp) TSW in linear and Morton-twiddled addressing.
- Applying coverage once to premultiplied tint RGB and alpha (including nonopaque premultiplied tints).
- Axis-aligned quad mesh gating: requiring all 4 corners for quad and rejecting repeated 3-corner triangles.
- 1-byte mask reading (linear and twiddled) and scissor clipping without hand-copied blend math.
- Verification that AGX replay core in backend/s5l8960x.c implements color matrix and format-3 mask tint helpers.
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
        self.assertIn('agx_apply_mask_tint', core)
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
        """Format 3 coverage mask texel modulates alpha and applies coverage once to premultiplied RGB tint."""
        # Coverage = 255: full tint #E6E6E6 (230, 230, 230)
        res = aro.apply_mask_tint(255, 0xffe6e6e6)
        b, g, r, a = res & 0xff, (res >> 8) & 0xff, (res >> 16) & 0xff, res >> 24
        self.assertEqual((r, g, b, a), (230, 230, 230, 255))

        # Coverage = 0: zero alpha, zero color
        res = aro.apply_mask_tint(0, 0xffe6e6e6)
        b, g, r, a = res & 0xff, (res >> 8) & 0xff, (res >> 16) & 0xff, res >> 24
        self.assertEqual((r, g, b, a), (0, 0, 0, 0))

        # Coverage = 128: 50% antialiased edge
        res = aro.apply_mask_tint(128, 0xffe6e6e6)
        b, g, r, a = res & 0xff, (res >> 8) & 0xff, (res >> 16) & 0xff, res >> 24
        self.assertEqual(a, 128)
        self.assertEqual(r, 230 * 128 // 255)

        # Nonopaque premultiplied tint: tint=0x80800000 (A=128, R=128, G=0, B=0), coverage=128
        # Coverage must be applied once to premultiplied R: R = 128 * 128 // 255 = 64, A = 128 * 128 // 255 = 64
        # (NOT R = 32 which resulted from multiplying tint alpha twice)
        res_premul = aro.apply_mask_tint(128, 0x80800000)
        pb, pg, pr, pa = res_premul & 0xff, (res_premul >> 8) & 0xff, (res_premul >> 16) & 0xff, res_premul >> 24
        self.assertEqual((pr, pg, pb, pa), (64, 0, 0, 64))

    def test_axis_aligned_quad_mesh_discrimination(self):
        """Recognize 9-slice (54 verts) and single quads (4/6 verts), reject repeated 3-corner triangles and non-quads (48 verts)."""
        # 4-corner valid quad
        verts_4 = [(10.0, 20.0), (50.0, 20.0), (10.0, 80.0), (50.0, 80.0)]
        self.assertTrue(aro.is_axis_aligned_quad_mesh(verts_4, 4))

        # 6-vertex valid quad (two triangles with 4 distinct bounding corners)
        verts_6 = [
            (10.0, 20.0), (50.0, 20.0), (10.0, 80.0),
            (50.0, 20.0), (50.0, 80.0), (10.0, 80.0)
        ]
        self.assertTrue(aro.is_axis_aligned_quad_mesh(verts_6, 6))

        # Repeated 3-corner triangle with count=4 (TL, TR, BL, TL; missing BR) - MUST BE REJECTED
        tri_4 = [(10.0, 20.0), (50.0, 20.0), (10.0, 80.0), (10.0, 20.0)]
        self.assertFalse(aro.is_axis_aligned_quad_mesh(tri_4, 4))

        # Repeated 3-corner triangle with count=6 (two triangles sharing only 3 corners) - MUST BE REJECTED
        tri_6 = [
            (10.0, 20.0), (50.0, 20.0), (10.0, 80.0),
            (10.0, 20.0), (50.0, 20.0), (10.0, 80.0)
        ]
        self.assertFalse(aro.is_axis_aligned_quad_mesh(tri_6, 6))

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
        end_row = src.index('static uint32_t agx_blend_vibrancy(')
        fn_gva_and_row = src[start_gva:end_row]

        start_blend = src.index('/* Premultiplied BGRA src-over')
        end_tint = src.index('static inline uint32_t agx_blend_plus_lighter(')
        fn_blend_and_tint = src[start_blend:end_tint]

        start_quad = src.index('static bool agx_is_axis_aligned_quad_mesh(')
        end_quad = src.index('static AGXReplaySnapshot *agx_capture_ta(')
        fn_quad = src[start_quad:end_quad]

        harness = Path(cls.tmp.name) / 'switch_mask.c'
        harness.write_text(f'''#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

#define MIN(a, b) ((a) < (b) ? (a) : (b))
#define MAX(a, b) ((a) > (b) ? (a) : (b))

static inline uint32_t ldl_le_p(const void *p) {{
    uint32_t val;
    memcpy(&val, p, sizeof(val));
    return val;
}}

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

typedef void A7State;

static const uint8_t *g_test_mem = NULL;
static uint64_t g_test_mem_sz = 0;

static bool agx_ctx_read(A7State *s, uint64_t cat, uint64_t gva, void *buf, size_t n) {{
    if (gva + n > g_test_mem_sz || !g_test_mem) return false;
    memcpy(buf, g_test_mem + gva, n);
    return true;
}}

static bool agx_replay_matrix(A7State *s, uint64_t cat, uint64_t vs_data, float m4[4]) {{
    return false;
}}

{fn_gva_and_row}
{fn_blend_and_tint}
{fn_quad}

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

void c_set_test_memory(const uint8_t *mem, uint64_t sz) {{
    g_test_mem = mem;
    g_test_mem_sz = sz;
}}

bool c_texture_row(uint64_t addr, uint32_t w, uint32_t h, uint32_t stride,
                   uint32_t format, uint32_t bpp, bool twiddled,
                   unsigned y, uint32_t *out) {{
    AGXTexture tex = {{
        .addr = addr,
        .w = w,
        .h = h,
        .stride = stride,
        .format = format,
        .bpp = bpp,
        .twiddled = twiddled,
        .strip = false
    }};
    return agx_texture_row(NULL, 0, &tex, y, out);
}}

uint32_t c_apply_mask_tint(uint32_t cov, uint32_t tint) {{
    return agx_apply_mask_tint(cov, tint);
}}

uint32_t c_blend_over(uint32_t dst, uint32_t src) {{
    return agx_blend_over(dst, src);
}}

bool c_is_quad_mesh(uint64_t buf, unsigned count, unsigned stride, bool clip_space) {{
    float vp[4] = {{1.0f, 0.0f, 1.0f, 0.0f}};
    return agx_is_axis_aligned_quad_mesh(NULL, 0, buf, count, stride, 0, vp, clip_space);
}}

void c_render_mask_quad(uint32_t *fb, int fb_w, int fb_h,
                        uint64_t mask_addr, int mask_w, int mask_h, int mask_stride,
                        bool twiddled, uint32_t tint,
                        int sx0, int sy0, int sx1, int sy1) {{
    AGXTexture tex = {{
        .addr = mask_addr,
        .w = (uint32_t)mask_w,
        .h = (uint32_t)mask_h,
        .stride = (uint32_t)mask_stride,
        .format = 3,
        .bpp = 1,
        .twiddled = twiddled,
        .strip = false
    }};
    for (int y = 0; y < mask_h && y < fb_h; y++) {{
        if (y < sy0 || y >= sy1) continue;
        uint32_t texrow[4096];
        if (!agx_texture_row(NULL, 0, &tex, y, texrow)) continue;
        for (int x = 0; x < mask_w && x < fb_w; x++) {{
            if (x < sx0 || x >= sx1) continue;
            uint32_t cov = texrow[x] >> 24;
            uint32_t src = agx_apply_mask_tint(cov, tint);
            uint32_t dst = fb[y * fb_w + x];
            fb[y * fb_w + x] = agx_blend_over(dst, src);
        }}
    }}
}}
''')
        binary = Path(cls.tmp.name) / 'switch_mask.so'
        subprocess.run([compiler, '-shared', '-fPIC', str(harness), '-o', str(binary)], check=True)
        cls.lib = ctypes.CDLL(str(binary))
        cls.lib.c_texel_gva.restype = ctypes.c_uint64
        cls.lib.c_apply_mask_tint.restype = ctypes.c_uint32
        cls.lib.c_blend_over.restype = ctypes.c_uint32
        cls.lib.c_is_quad_mesh.restype = ctypes.c_bool
        cls.lib.c_texture_row.restype = ctypes.c_bool

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

    def test_c_apply_mask_tint_premultiplied_coverage(self):
        """Production agx_apply_mask_tint applies coverage once to premultiplied RGB tint."""
        # Nonopaque premultiplied tint: tint=0x80800000 (A=128, R=128, G=0, B=0), coverage=128
        # R = 128 * 128 / 255 = 64, A = 128 * 128 / 255 = 64 (NOT R = 32)
        res = self.lib.c_apply_mask_tint(128, 0x80800000)
        ta = (res >> 24) & 0xff
        tr = (res >> 16) & 0xff
        tg = (res >> 8) & 0xff
        tb = res & 0xff
        self.assertEqual((tr, tg, tb, ta), (64, 0, 0, 64))

        # Opaque tint #E6E6E6 with full coverage
        res_full = self.lib.c_apply_mask_tint(255, 0xffe6e6e6)
        self.assertEqual(res_full, 0xffe6e6e6)

        # Zero coverage -> transparent black
        res_zero = self.lib.c_apply_mask_tint(0, 0xffe6e6e6)
        self.assertEqual(res_zero, 0)

        # Half coverage on #E6E6E6
        res_half = self.lib.c_apply_mask_tint(128, 0xffe6e6e6)
        self.assertEqual((res_half >> 24) & 0xff, 128)
        self.assertEqual((res_half >> 16) & 0xff, 230 * 128 // 255)

    def test_c_blend_over_production_helper(self):
        """Production agx_blend_over blends premultiplied source over destination."""
        # White background (0xffffffff)
        dst = 0xffffffff
        # Full opaque #E6E6E6 over white
        out = self.lib.c_blend_over(dst, 0xffe6e6e6)
        self.assertEqual(out, 0xffe6e6e6)

        # 50% premultiplied red (0x80800000) over white
        # out = src + dst * (255 - a) / 255
        # r = 128 + 255 * 127 / 255 = 128 + 127 = 255 (byte 2)
        # g = 0 + 255 * 127 / 255 = 127 (byte 1)
        # b = 0 + 255 * 127 / 255 = 127 (byte 0)
        # a = 128 + 255 * 127 / 255 = 255 (byte 3)
        # Word: (255 << 24) | (255 << 16) | (127 << 8) | 127 = 0xffff7f7f
        out_red = self.lib.c_blend_over(dst, 0x80800000)
        self.assertEqual(out_red, 0xffff7f7f)

    def test_c_quad_mesh_rejection_of_repeated_3corner_triangle(self):
        """Production agx_is_axis_aligned_quad_mesh requires all 4 corners and rejects repeated triangles."""
        # 4-corner valid quad: [(0, 0), (10, 0), (0, 10), (10, 10)]
        verts_4 = struct.pack('<8f', 0.0, 0.0, 10.0, 0.0, 0.0, 10.0, 10.0, 10.0)
        self.lib.c_set_test_memory(verts_4, len(verts_4))
        self.assertTrue(self.lib.c_is_quad_mesh(0, 4, 8, True))

        # 6-vertex valid quad (2 triangles forming quad with 4 corners)
        verts_6 = struct.pack('<12f',
                              0.0, 0.0, 10.0, 0.0, 0.0, 10.0,
                              10.0, 0.0, 10.0, 10.0, 0.0, 10.0)
        self.lib.c_set_test_memory(verts_6, len(verts_6))
        self.assertTrue(self.lib.c_is_quad_mesh(0, 6, 8, True))

        # Repeated 3-corner triangle with count=4 (TL, TR, BL, TL; missing BR) - MUST BE REJECTED
        tri_4 = struct.pack('<8f', 0.0, 0.0, 10.0, 0.0, 0.0, 10.0, 0.0, 0.0)
        self.lib.c_set_test_memory(tri_4, len(tri_4))
        self.assertFalse(self.lib.c_is_quad_mesh(0, 4, 8, True))

        # Repeated 3-corner triangle with count=6 (two identical triangles sharing only 3 corners)
        tri_6 = struct.pack('<12f',
                            0.0, 0.0, 10.0, 0.0, 0.0, 10.0,
                            0.0, 0.0, 10.0, 0.0, 0.0, 10.0)
        self.lib.c_set_test_memory(tri_6, len(tri_6))
        self.assertFalse(self.lib.c_is_quad_mesh(0, 6, 8, True))

        # Count not 4, 6, or 54 (e.g. 48 clock hand / glint fans)
        self.assertFalse(self.lib.c_is_quad_mesh(0, 48, 8, True))

    def test_c_texture_row_1byte_linear_and_twiddled(self):
        """Production agx_texture_row reads 1-byte Format 3 mask into alpha with 0x00ffffff RGB."""
        # Linear 1-byte texture: 8x4 at addr 0, stride 8
        linear_data = bytes([y * 8 + x for y in range(4) for x in range(8)])
        self.lib.c_set_test_memory(linear_data, len(linear_data))
        out_row = (ctypes.c_uint32 * 8)()
        ok = self.lib.c_texture_row(0, 8, 4, 8, 3, 1, False, 2, out_row)
        self.assertTrue(ok)
        for x in range(8):
            expected_cov = 2 * 8 + x
            expected_val = (expected_cov << 24) | 0x00ffffff
            self.assertEqual(out_row[x], expected_val)

        # Twiddled 1-byte texture: 4x4 at addr 0, stride 4
        # Coordinate (1, 1) has Morton index: bit 0 = Y & 1 = 1, bit 1 = X & 1 = 1 -> offset 3
        # GPU page caching reads 4096 bytes per page
        twiddled_data = bytearray(4096)
        twiddled_data[3] = 0xdd  # value at (1, 1)
        self.lib.c_set_test_memory(bytes(twiddled_data), 4096)
        out_row_tw = (ctypes.c_uint32 * 4)()
        ok_tw = self.lib.c_texture_row(0, 4, 4, 4, 3, 1, True, 1, out_row_tw)
        self.assertTrue(ok_tw)
        self.assertEqual(out_row_tw[1], (0xdd << 24) | 0x00ffffff)

    def test_c_render_mask_quad_and_scissor(self):
        """Render a 16x16 circular mask with scissor [0, 8] x [0, 16] using production C pipeline."""
        fb_w, fb_h = 16, 16
        # Background is white (0xffffffff)
        fb = (ctypes.c_uint32 * (fb_w * fb_h))(*([0xffffffff] * (fb_w * fb_h)))

        # Create a 16x16 linear mask: 255 for radius < 6, 0 for radius >= 6
        mask_bytes = bytearray(16 * 16)
        for y in range(16):
            for x in range(16):
                r2 = (x - 7.5)**2 + (y - 7.5)**2
                mask_bytes[y * 16 + x] = 255 if r2 <= 36 else 0

        self.lib.c_set_test_memory(bytes(mask_bytes), len(mask_bytes))

        # Scissor only left half: [0, 8] x [0, 16]
        tint = 0xffe6e6e6  # #E6E6E6
        self.lib.c_render_mask_quad(
            fb, fb_w, fb_h, 0, 16, 16, 16, False, tint, 0, 0, 8, 16)

        # Pixels in left half inside mask must be #E6E6E6 (0xffe6e6e6)
        self.assertEqual(fb[7 * 16 + 4], 0xffe6e6e6)
        # Pixels in left half outside mask must be untouched white (0xffffffff)
        self.assertEqual(fb[0 * 16 + 0], 0xffffffff)
        # Pixels in right half (outside scissor) must remain untouched white even if inside mask
        self.assertEqual(fb[7 * 16 + 11], 0xffffffff)


if __name__ == '__main__':
    unittest.main()
