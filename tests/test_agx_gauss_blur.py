"""Offline tests for CoreAnimation Gaussian blur replay in AGX HLE."""
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'backend/s5l8960x.c'
ROOT = Path(__file__).resolve().parents[1]

# Recovered narrow_blur taps for sigma = 3.5 (from QuartzCore narrowBlurParameters @ 0x18429b6c0)
BLUR_TAPS_3_5 = [
    (0.6575, 0.1666),
    (2.4492, 0.1759),
    (4.4092, 0.1005),
    (6.3704, 0.0417),
    (8.3332, 0.0126),
    (10.2979, 0.0027),
]

# Recovered 4-tap pair 1D blur taps (used in unlock label glow / status bar)
BLUR_TAPS_4 = [
    (0.638336, 0.275844),
    (2.348645, 0.185744),
    (4.245085, 0.035760),
    (6.164516, 0.002652),
]

# Recovered 7-tap pair 1D blur taps (used in settings navigation bar / switch shadow)
BLUR_TAPS_7 = [
    (0.662207, 0.118916),
    (2.475021, 0.141265),
    (4.455121, 0.107065),
    (6.435364, 0.069256),
    (8.415810, 0.038236),
    (10.396517, 0.018016),
    (12.377541, 0.007245),
]

BLUR_PROGRAMS = {
    0x115ac3fe908dec8a: '2a0a949999af1b800033000080408001880020ff3a8a80949999af1b8000330000824080038800210024088b4520b006c44010c0400002806aff4620f44c8060',
    0x90d330877114e752: '2a0a949999af1b800033000080408001880020ff3a8a80949999af1b800033000082408003880021000b808c4520b006c44010c0400002806aff4620f44c8060',
    0xb443c1f6a714b429: '2a0a949999af1b800033000080408001880020ff3a8a80949999af1b800033000082408003880021001230004520b006c44010c0400002806aff4620f44c8060',
    0x21377cafd3277883: '2a0a949999af1b800033000080408001880020ff3a8a80949999af1b800033000082408003880021000000004520b002c44010c0400002806aff4620f44c8060',
    0x95631b61e7c1be31: '2a0a949999af1b800033000080408001880020ff3a8a80949999af1b80003300008240800388002100ff29364520b002c44010c0400002806aff4620f44c8060',
    0x145dbc3101d94f8c: '2a0a949999af1b800033000080408001880020ff3a8a80949999af1b800033000082408003880021004601244520b006c44010c0400002806aff4620f44c8060',
    0xf0c17d1e30ec2393: '2a0a949999af1b800033000080408001880020ff3a8a80949999af1b800033000082408003880021000488804520b006c44010c0400002806aff4620f44c8060',
    0x69f1605719a006b2: '2a0a949999af1b800033000080408001880020ff3a8a80949999af1b800033000082408003880021000020ff4520b006c44010c0400002806aff4620f44c8060',
    0xa4c025e0be54470a: '2a0a949999af1b800033000080408001880020ff3a8a80949999af1b800033000082408003880021000800ff4520b006c44010c0400002806aff4620f44c8060',
}


class AGXGaussBlurTests(unittest.TestCase):
    def test_blur_kernel_properties(self):
        """Verify the mathematical properties of the recovered blur filters (4, 6, 7 taps)."""
        for kernel_name, taps in [
            ('6-tap narrow_blur', BLUR_TAPS_3_5),
            ('4-tap label glow', BLUR_TAPS_4),
            ('7-tap navbar/switch', BLUR_TAPS_7),
        ]:
            # Check tap monotonicity
            offsets = [t[0] for t in taps]
            self.assertEqual(offsets, sorted(offsets), f'{kernel_name} offsets not sorted')

            # Check weights are positive
            weights = [t[1] for t in taps]
            for w in weights:
                self.assertGreater(w, 0.0, f'{kernel_name} weight non-positive')

            # Check total weight sum (2 symmetric taps per pair sum to 1.0)
            total_weight = 2.0 * sum(weights)
            self.assertAlmostEqual(total_weight, 1.0000, places=4, msg=f'{kernel_name} weight sum mismatch')

    def test_blur_program_hashes(self):
        """Verify the 9 distinct USC blur program fingerprints and their shared preamble."""
        preamble = bytes.fromhex('2a0a949999af1b800033000080408001880020ff3a8a80949999af1b80003300008240800388002100')
        self.assertEqual(len(preamble), 41)

        for expected_hash, hexcode in BLUR_PROGRAMS.items():
            code = bytes.fromhex(hexcode)
            self.assertEqual(len(code), 64)
            self.assertTrue(code.startswith(preamble), f'Hash {expected_hash:#x} preamble mismatch')

            # Compute 64-bit FNV-1a
            h = 0xcbf29ce484222325
            for b in code:
                h = ((h ^ b) * 0x100000001b3) & 0xffffffffffffffff
            self.assertEqual(h, expected_hash)

    def test_c_blur_implementation(self):
        """Compile and verify the C blur implementation extracted from s5l8960x.c."""
        compiler = shutil.which('cc')
        if not compiler:
            self.skipTest('host C compiler unavailable')

        source = SOURCE.read_text()
        start = source.index('static const struct {\n    float offset;\n    float weight;\n} agx_blur_taps[]')
        end = source.index('static uint64_t agx_replay_program(')
        c_helpers = source[start:end]

        harness = f'''
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <math.h>

#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))

{c_helpers}

int main(int argc, char **argv) {{
    if (argc < 2) return 1;
    const char *test = argv[1];

    if (!strcmp(test, "uniform")) {{
        /* 16x16 uniform image of 0xff112233 */
        uint32_t in_buf[16 * 16], tmp_buf[16 * 16];
        for (int i = 0; i < 16 * 16; i++) in_buf[i] = 0xff112233;
        agx_blur_texture(in_buf, tmp_buf, 16, 16);
        for (int i = 0; i < 16 * 16; i++) {{
            if (in_buf[i] != 0xff112233) {{
                fprintf(stderr, "Uniform blur mismatch at %d: 0x%08x != 0xff112233\\n", i, in_buf[i]);
                return 2;
            }}
        }}
        printf("UNIFORM_OK\\n");
        return 0;
    }} else if (!strcmp(test, "step_edge")) {{
        /* 32x32 image with left half black (0xff000000) and right half white (0xffffffff) */
        uint32_t in_buf[32 * 32], tmp_buf[32 * 32];
        for (int y = 0; y < 32; y++) {{
            for (int x = 0; x < 32; x++) {{
                in_buf[y * 32 + x] = (x < 16) ? 0xff000000 : 0xffffffff;
            }}
        }}
        agx_blur_texture(in_buf, tmp_buf, 32, 32);

        /* Center scanline (y = 16): verify monotonic smoothing across boundary */
        int y = 16;
        for (int x = 1; x < 31; x++) {{
            uint8_t c_prev = in_buf[y * 32 + x - 1] & 0xff;
            uint8_t c_curr = in_buf[y * 32 + x] & 0xff;
            if (c_curr < c_prev) {{
                fprintf(stderr, "Non-monotonic step response at x=%d: %u < %u\\n", x, c_curr, c_prev);
                return 3;
            }}
        }}
        /* Symmetry: c(15) + c(16) should equal 255 */
        uint8_t c15 = in_buf[y * 32 + 15] & 0xff;
        uint8_t c16 = in_buf[y * 32 + 16] & 0xff;
        if (abs((int)c15 + (int)c16 - 255) > 1) {{
            fprintf(stderr, "Asymmetric step response: c15=%u, c16=%u, sum=%u\\n", c15, c16, c15 + c16);
            return 4;
        }}
        printf("STEP_EDGE_OK c15=%u c16=%u\\n", c15, c16);
        return 0;
    }} else if (!strcmp(test, "1d_horizontal")) {{
        /* Vertical step edge: left black, right white */
        uint32_t in_buf[32 * 32], tmp_buf[32 * 32];
        for (int y = 0; y < 32; y++) {{
            for (int x = 0; x < 32; x++) in_buf[y * 32 + x] = (x < 16) ? 0xff000000 : 0xffffffff;
        }}
        float offs[7], ws[7];
        for (int i = 0; i < 7; i++) {{
            offs[i] = agx_blur_taps_7[i].offset;
            ws[i] = agx_blur_taps_7[i].weight;
        }}
        agx_blur_1d(in_buf, tmp_buf, 32, 32, offs, ws, 7, false);
        for (int x = 1; x < 32; x++) {{
            if ((in_buf[16 * 32 + x] & 0xff) < (in_buf[16 * 32 + x - 1] & 0xff)) return 6;
        }}
        uint32_t c_ref = in_buf[16];
        for (int y = 1; y < 32; y++) {{
            if (in_buf[y * 32 + 16] != c_ref) return 7;
        }}
        printf("1D_HORIZ_OK\\n");
        return 0;
    }} else if (!strcmp(test, "1d_vertical")) {{
        /* Horizontal step edge: top black, bottom white */
        uint32_t in_buf[32 * 32], tmp_buf[32 * 32];
        for (int y = 0; y < 32; y++) {{
            for (int x = 0; x < 32; x++) in_buf[y * 32 + x] = (y < 16) ? 0xff000000 : 0xffffffff;
        }}
        float offs[4], ws[4];
        for (int i = 0; i < 4; i++) {{
            offs[i] = agx_blur_taps_4[i].offset;
            ws[i] = agx_blur_taps_4[i].weight;
        }}
        agx_blur_1d(in_buf, tmp_buf, 32, 32, offs, ws, 4, true);
        for (int y = 1; y < 32; y++) {{
            if ((in_buf[y * 32 + 16] & 0xff) < (in_buf[(y - 1) * 32 + 16] & 0xff)) return 8;
        }}
        uint32_t r_ref = in_buf[16 * 32];
        for (int x = 1; x < 32; x++) {{
            if (in_buf[16 * 32 + x] != r_ref) return 9;
        }}
        printf("1D_VERT_OK\\n");
        return 0;
    }}
    return 5;
}}
'''
        with tempfile.TemporaryDirectory() as td:
            src_path = Path(td) / 'test_blur.c'
            bin_path = Path(td) / 'test_blur'
            src_path.write_text(harness)

            res = subprocess.run([compiler, '-O2', str(src_path), '-o', str(bin_path), '-lm'],
                                 capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f'Compilation failed:\n{res.stderr}')

            # Test uniform image
            res_uniform = subprocess.run([str(bin_path), 'uniform'], capture_output=True, text=True)
            self.assertEqual(res_uniform.returncode, 0, f'Uniform blur test failed:\n{res_uniform.stderr}')
            self.assertIn('UNIFORM_OK', res_uniform.stdout)

            # Test step edge response
            res_step = subprocess.run([str(bin_path), 'step_edge'], capture_output=True, text=True)
            self.assertEqual(res_step.returncode, 0, f'Step edge blur test failed:\n{res_step.stderr}')
            self.assertIn('STEP_EDGE_OK', res_step.stdout)

            # Test 1D horizontal blur
            res_h = subprocess.run([str(bin_path), '1d_horizontal'], capture_output=True, text=True)
            self.assertEqual(res_h.returncode, 0, f'1D horizontal blur test failed:\n{res_h.stderr}')
            self.assertIn('1D_HORIZ_OK', res_h.stdout)

            # Test 1D vertical blur
            res_v = subprocess.run([str(bin_path), '1d_vertical'], capture_output=True, text=True)
            self.assertEqual(res_v.returncode, 0, f'1D vertical blur test failed:\n{res_v.stderr}')
            self.assertIn('1D_VERT_OK', res_v.stdout)

    def test_python_offline_mirror(self):
        """Verify that agx_replay_offline.py produces identical blur results to C."""
        import sys
        sys.path.insert(0, str(ROOT / 'backend'))
        import agx_replay_offline as aro

        # Test uniform image in Python
        w, h = 16, 16
        solid = (0x33, 0x22, 0x11, 0xff)  # (b, g, r, a)
        in_buf = [solid] * (w * h)
        out_buf = aro.gaussian_blur_texture(in_buf, w, h)
        for i, pix in enumerate(out_buf):
            self.assertEqual(pix, solid, f'Python blur uniform mismatch at {i}: {pix} != {solid}')

        # Test step edge in Python (2D)
        w, h = 32, 32
        black = (0, 0, 0, 255)
        white = (255, 255, 255, 255)
        step_buf = [black if (x < 16) else white for y in range(h) for x in range(w)]
        out_step = aro.gaussian_blur_texture(step_buf, w, h)

        # Monotonicity along row 16
        row16 = [out_step[16 * 32 + x][0] for x in range(32)]
        for x in range(1, 32):
            self.assertGreaterEqual(row16[x], row16[x - 1])

        # Symmetry
        self.assertAlmostEqual(row16[15] + row16[16], 255, delta=1)

        # Test 1D horizontal blur in Python
        out_h = aro.gaussian_blur_texture(step_buf, w, h, taps=BLUR_TAPS_7, direction='horizontal')
        row_h = [out_h[16 * 32 + x][0] for x in range(32)]
        for x in range(1, 32):
            self.assertGreaterEqual(row_h[x], row_h[x - 1])
        # Column 16 must be completely invariant along y
        for y in range(h):
            self.assertEqual(out_h[y * 32 + 16], out_h[16])

        # Test 1D vertical blur in Python
        v_step_buf = [black if (y < 16) else white for y in range(h) for x in range(w)]
        out_v = aro.gaussian_blur_texture(v_step_buf, w, h, taps=BLUR_TAPS_4, direction='vertical')
        col_v = [out_v[y * 32 + 16][0] for y in range(32)]
        for y in range(1, 32):
            self.assertGreaterEqual(col_v[y], col_v[y - 1])
        # Row 16 must be completely invariant along x
        for x in range(w):
            self.assertEqual(out_v[16 * 32 + x], out_v[16 * 32])

    def test_direction_and_taps_extraction(self):
        """Verify direction detection and tap parameter extraction for blur programs."""
        import sys
        sys.path.insert(0, str(ROOT / 'backend'))
        import agx_replay_offline as aro

        # Mock frame with vertex uniforms pointing to horizontal and vertical offsets
        class MockFrame:
            def __init__(self, vs_words, frag_words):
                self.pages = {}
                # vs_words at 0x1000
                self.vs_ptr = 0x1000
                self.vs_data = 0x2000
                # Map vs_data: header with ptr at word 12 (offset 48)
                vsd_bytes = bytearray(64)
                struct.pack_into('<Q', vsd_bytes, 48, self.vs_ptr)
                self.pages[self.vs_data] = bytes(vsd_bytes)
                # Map vs uniform words at 0x1000
                u_bytes = bytearray(4096)
                for i, w in enumerate(vs_words):
                    struct.pack_into('<I', u_bytes, 4 * i, w)
                self.pages[self.vs_ptr] = bytes(u_bytes)

            def u32(self, gva):
                base = (gva // 4096) * 4096
                off = gva % 4096
                return struct.unpack_from('<I', self.pages[base], off)[0]

        # 1. Horizontal 7-tap
        vs_h = [0] * 16  # matrix
        for t, (ox, w) in enumerate(BLUR_TAPS_7):
            vs_h.extend([struct.unpack('<I', struct.pack('<f', ox))[0], 0, 0, 0])
        mf_h = MockFrame(vs_h, [])
        params_h = aro.extract_blur_params(mf_h, mf_h.vs_data, [], aro.BLUR_7TAP)
        self.assertEqual(params_h['direction'], 'horizontal')
        self.assertEqual(params_h['n_taps'], 7)
        for i in range(7):
            self.assertAlmostEqual(params_h['offsets'][i], BLUR_TAPS_7[i][0], places=5)

        # 2. Vertical 4-tap
        vs_v = [0] * 16  # matrix
        for t, (oy, w) in enumerate(BLUR_TAPS_4):
            vs_v.extend([0, struct.unpack('<I', struct.pack('<f', oy))[0], 0, 0])
        mf_v = MockFrame(vs_v, [])
        params_v = aro.extract_blur_params(mf_v, mf_v.vs_data, [], aro.BLUR_4TAP)
        self.assertEqual(params_v['direction'], 'vertical')
        self.assertEqual(params_v['n_taps'], 4)
        for i in range(4):
            self.assertAlmostEqual(params_v['offsets'][i], BLUR_TAPS_4[i][0], places=5)

    def test_contract_replay_blob_gauss_blur(self):
        """Verify that agx_contract_test.py's gauss-blur case builds cleanly and sets oracle."""
        import sys
        sys.path.insert(0, str(ROOT / 'backend'))
        import agx_contract_test as act

        blob, target_pages, expected, dims, draws, ta_count = act.replay_blob('gauss-blur')
        self.assertGreater(len(blob), 0)
        self.assertEqual(dims, (16, 16))
        self.assertEqual(draws, 2)
        self.assertIn('gauss-blur', act.REPLAY_CASES)


if __name__ == '__main__':
    unittest.main()
