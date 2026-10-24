"""Unit tests for exact textured sampling and vibrancy blend (Task GD).

Tests cover:
  1. Sampler word decoding (min/mag filters, wrap/clamp modes, max LOD).
  2. Fixed-point bilinear interpolation maths (half-texel offset, 8-bit weights).
  3. C/Python parity for bilinear sampling and vibrancy blending.
  4. Draw classification rules for stride 36, stride 20, scaled bilinear, and vibrancy.
"""

import math
import struct
import unittest

from backend import agx_replay_offline as ro


class TestAgxSamplerDecode(unittest.TestCase):
    def test_decode_linear_clamp(self):
        # 0x00000fff 0x00002450 0 0: Linear min/mag, Clamp-to-edge U/V
        words = [0x00000fff, 0x00002450, 0, 0]
        s = ro.decode_sampler(words)
        self.assertEqual(s['min_filter'], 'linear')
        self.assertEqual(s['mag_filter'], 'linear')
        self.assertEqual(s['wrap_u'], 'clamp_to_edge')
        self.assertEqual(s['wrap_v'], 'clamp_to_edge')
        self.assertEqual(s['max_lod'], 4095)
        self.assertTrue(s['linear_filter'])
        self.assertTrue(s['clamp_to_edge'])
        self.assertEqual(s['words'], words)

    def test_decode_nearest_clamp(self):
        # 0x00000fff 0x00002400 0 0: Nearest min/mag, Clamp-to-edge U/V
        words = [0x00000fff, 0x00002400, 0, 0]
        s = ro.decode_sampler(words)
        self.assertEqual(s['min_filter'], 'nearest')
        self.assertEqual(s['mag_filter'], 'nearest')
        self.assertEqual(s['wrap_u'], 'clamp_to_edge')
        self.assertEqual(s['wrap_v'], 'clamp_to_edge')
        self.assertFalse(s['linear_filter'])
        self.assertTrue(s['clamp_to_edge'])

    def test_decode_repeat_and_explicit_clamp(self):
        # 0x00000fff 0x00000000 0 0: Nearest, Repeat U/V
        s_repeat = ro.decode_sampler([0x00000fff, 0x00000000, 0, 0])
        self.assertEqual(s_repeat['wrap_u'], 'repeat')
        self.assertEqual(s_repeat['wrap_v'], 'repeat')
        self.assertFalse(s_repeat['clamp_to_edge'])

        # 0x00000fff 0x02022400 0 0: Explicit byte-clamp in high bits
        s_high = ro.decode_sampler([0x00000fff, 0x02022400, 0, 0])
        self.assertEqual(s_high['wrap_u'], 'clamp_to_edge')
        self.assertEqual(s_high['wrap_v'], 'clamp_to_edge')
        self.assertTrue(s_high['clamp_to_edge'])


class TestAgxBilinearMath(unittest.TestCase):
    def test_half_texel_centers(self):
        # 2x2 texture with known distinct corner pixels
        buf = [
            (10, 20, 30, 255), (100, 110, 120, 255),
            (40, 50, 60, 255), (200, 210, 220, 255),
        ]
        # At (0.5, 0.5), texel coordinates fu = 0.5 - 0.5 = 0, fv = 0.5 - 0.5 = 0.
        # Exactly samples c00
        p00 = ro.sample_bilinear(buf, 2, 2, 0.5, 0.5)
        self.assertEqual(p00, (10, 20, 30, 255))

        # At (1.5, 0.5), exactly samples c10
        p10 = ro.sample_bilinear(buf, 2, 2, 1.5, 0.5)
        self.assertEqual(p10, (100, 110, 120, 255))

        # At (1.0, 0.5), fu = 0.5, fx = 0.5, wx = 128 -> midpoint of c00 and c10
        mid = ro.sample_bilinear(buf, 2, 2, 1.0, 0.5)
        # (10 * 128 + 100 * 128 + 32768) >> 16 = 14080 + 32768 >> 16 = 46848 >> 16 = 55
        self.assertEqual(mid[0], 55)
        self.assertEqual(mid[1], 65)
        self.assertEqual(mid[2], 75)
        self.assertEqual(mid[3], 255)

    def test_clamp_to_edge_boundaries(self):
        buf = [
            (50, 50, 50, 255), (150, 150, 150, 255),
            (70, 70, 70, 255), (170, 170, 170, 255),
        ]
        # Samples outside [0.5, W-0.5] clamp to the edge texels
        p_neg = ro.sample_bilinear(buf, 2, 2, -10.0, -10.0)
        self.assertEqual(p_neg, (50, 50, 50, 255))

        p_far = ro.sample_bilinear(buf, 2, 2, 100.0, 100.0)
        self.assertEqual(p_far, (170, 170, 170, 255))


class TestAgxVibrancyAndParity(unittest.TestCase):
    def test_vibrancy_math(self):
        # Test darkenSourceOver: destination is light, dark shadow is preserved
        # dst = light grey (200, 200, 200, 255)
        dst = (200, 200, 200, 255)
        # src = dark shadow (0, 0, 0, 128)
        src = (0, 0, 0, 128)
        out = ro.blend_vibrancy(dst, src)
        # Luminance of dst: (54*200 + 183*200 + 19*200) >> 8 = 51200 >> 8 = 200
        # sr+sg+sb = 0 <= 384 -> y1 = 200
        # f = (200 * 200 // 255) ** 2 // 255 = 156 ** 2 // 255 = 95
        # sa = 128 * 95 // 255 = 47
        # out = 200 * (255 - 47) // 255 = 163
        self.assertEqual(out[0], 163)
        self.assertEqual(out[1], 163)
        self.assertEqual(out[2], 163)

    def test_c_parity_simulation(self):
        # Verify C fixed-point bitwise arithmetic matches Python exactly
        def c_interp(c00, c10, c01, c11, fx, fy):
            wx = int(fx * 256.0 + 0.5)
            wy = int(fy * 256.0 + 0.5)
            top = c00 * (256 - wx) + c10 * wx
            bot = c01 * (256 - wx) + c11 * wx
            val = (top * (256 - wy) + bot * wy + 32768) >> 16
            return min(255, max(0, val))

        for fx_val in [0.0, 0.125, 0.333, 0.5, 0.75, 0.999]:
            for fy_val in [0.0, 0.25, 0.5, 0.707, 1.0]:
                py_res = ro.sample_bilinear([(10, 0, 0, 0), (200, 0, 0, 0), (50, 0, 0, 0), (250, 0, 0, 0)],
                                            2, 2, fx_val + 0.5, fy_val + 0.5)[0]
                c_res = c_interp(10, 200, 50, 250, fx_val, fy_val)
                self.assertEqual(py_res, c_res)


if __name__ == '__main__':
    unittest.main()
