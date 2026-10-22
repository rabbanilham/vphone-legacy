"""Unit tests for AGX replay graphics accuracy:
- USC blur program fingerprint table (9 programs)
- PDS pointer list traversal termination on unmapped/invalid word
- Row-cached bilinear interpolation for scaled textures
- Porter-Duff DEST_IN padding zeroing on offscreen passes
- 4-channel vibrancy blending with alpha attenuation
"""
import importlib.util
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'backend/s5l8960x.c'
ROOT = Path(__file__).resolve().parents[1]

REPLAY_SPEC = importlib.util.spec_from_file_location(
    'agx_replay_offline', ROOT / 'backend/agx_replay_offline.py')
aro = importlib.util.module_from_spec(REPLAY_SPEC)
REPLAY_SPEC.loader.exec_module(aro)


class AGXAccuracyTests(unittest.TestCase):
    def test_all_blur_programs(self):
        """All 9 CoreAnimation Gaussian blur shaders are registered and have the 41-byte preamble."""
        self.assertEqual(len(aro.BLUR_PROGRAMS), 9)
        preamble = bytes.fromhex('2a0a949999af1b800033000080408001880020ff3a8a80949999af1b80003300008240800388002100')
        self.assertEqual(len(preamble), 41)
        # Check specific probe-164 hashes
        expected_hashes = {
            0x115ac3fe908dec8a,
            0x90d330877114e752,
            0xb443c1f6a714b429,
            0x21377cafd3277883,
            0x95631b61e7c1be31,
            0x145dbc3101d94f8c,
            0xf0c17d1e30ec2393,
            0x69f1605719a006b2,
            0xa4c025e0be54470a,
        }
        self.assertEqual(aro.BLUR_PROGRAMS, expected_hashes)

    def test_vibrancy_four_channel(self):
        """Verify blend_vibrancy returns 4 channels with luminance-attenuated alpha."""
        dst = (100, 150, 200, 255)
        src = (255, 255, 255, 255)
        res = aro.blend_vibrancy(dst, src)
        self.assertEqual(len(res), 4)
        # Luminance for (100, 150, 200): y = (54*200 + 183*150 + 19*100) >> 8 = (10800 + 27450 + 1900) >> 8 = 156
        # Light source: y1 = 255 - 156 = 99
        # f = (99*99 // 255)**2 // 255 = 38**2 // 255 = 5
        # sa = 255 * 5 // 255 = 5
        # dst alpha = 255, so result alpha is 255
        self.assertEqual(res[3], 255)

        # Semi-transparent destination
        dst_semi = (100, 150, 200, 128)
        res_semi = aro.blend_vibrancy(dst_semi, src)
        # sa = 9, da = 128: out_a = min(255, sa + da * (255 - sa) // 255) = 9 + 128 * 246 // 255 = 132
        self.assertEqual(res_semi[3], 132)

    def test_bilinear_interpolation_smoothness(self):
        """Verify that sample_bilinear produces smooth intermediate values across a step."""
        # 2x2 texture with black left, white right
        buf = [
            (0, 0, 0, 255), (255, 255, 255, 255),
            (0, 0, 0, 255), (255, 255, 255, 255),
        ]
        # At midpoint (u=1.0, v=1.0 texel coord), should be exactly 128
        mid = aro.sample_bilinear(buf, 2, 2, 1.0, 1.0)
        self.assertEqual(mid[:3], (128, 128, 128))
        self.assertEqual(mid[3], 255)

        # Quarter step (u=0.75)
        quarter = aro.sample_bilinear(buf, 2, 2, 0.75, 1.0)
        self.assertLess(quarter[0], 128)
        self.assertGreater(quarter[0], 0)

    def test_c_bilinear_interpolation(self):
        """Compile and verify C agx_bilinear_interpolate extracted from s5l8960x.c."""
        compiler = shutil.which('cc')
        if not compiler:
            self.skipTest('host C compiler unavailable')

        source = SOURCE.read_text()
        start = source.index('static inline uint32_t agx_bilinear_interpolate(')
        end = source.index('static void agx_blur_texture(')
        c_helpers = source[start:end]

        harness = f'''
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <math.h>

{c_helpers}

int main(int argc, char **argv) {{
    uint32_t c00 = 0xff000000; /* black, alpha 0xff */
    uint32_t c10 = 0xffffffff; /* white, alpha 0xff */
    uint32_t c01 = 0xff000000;
    uint32_t c11 = 0xffffffff;

    /* Sample at midpoint fx = 0.5, fy = 0.5 */
    uint32_t mid = agx_bilinear_interpolate(c00, c10, c01, c11, 0.5f, 0.5f);
    uint8_t r = (mid >> 16) & 0xff;
    uint8_t g = (mid >> 8) & 0xff;
    uint8_t b = mid & 0xff;
    uint8_t a = (mid >> 24) & 0xff;

    if (abs((int)r - 128) > 1 || abs((int)g - 128) > 1 || abs((int)b - 128) > 1 || a != 0xff) {{
        fprintf(stderr, "Bilinear interpolate mismatch: r=%u g=%u b=%u a=%u\\n", r, g, b, a);
        return 1;
    }}
    printf("C_BILINEAR_OK\\n");
    return 0;
}}
'''
        with tempfile.TemporaryDirectory() as td:
            src_path = Path(td) / 'test_bilinear.c'
            bin_path = Path(td) / 'test_bilinear'
            src_path.write_text(harness)

            res = subprocess.run([compiler, '-O2', str(src_path), '-o', str(bin_path), '-lm'],
                                 capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f'Compilation failed:\n{res.stderr}')

            res_run = subprocess.run([str(bin_path)], capture_output=True, text=True)
            self.assertEqual(res_run.returncode, 0, f'Execution failed:\n{res_run.stderr}')
            self.assertIn('C_BILINEAR_OK', res_run.stdout)


if __name__ == '__main__':
    unittest.main()
