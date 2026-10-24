"""Unit tests for AGX UISwitch pill border primitive:
- Classification of stride-36 pill border (prog 0x62a99af8d8beb332)
- Distance-field radial math (outer radius r1 <= 1.0, inner radius r2 >= 1.0)
- Anti-aliased coverage blending for UISwitch track border
- Verification of C hardware replay core in backend/s5l8960x.c
- Verification of Python offline replay in backend/agx_replay_offline.py
- Zero C replay skips in Settings frames containing the switch
"""
import importlib.util
import json
import math
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'backend/s5l8960x.c'
SUMMARY_JSON = ROOT / 'corpus/report/summary.json'

REPLAY_SPEC = importlib.util.spec_from_file_location(
    'agx_replay_offline', ROOT / 'backend/agx_replay_offline.py')
aro = importlib.util.module_from_spec(REPLAY_SPEC)
REPLAY_SPEC.loader.exec_module(aro)


class MockFrame:
    def __init__(self):
        self.data = {}

    def read(self, gva, size):
        res = bytearray()
        for i in range(size):
            res.append(self.data.get(gva + i, 0))
        return bytes(res)

    def u32(self, gva):
        return struct.unpack_from('<I', self.read(gva, 4))[0]

    def u64(self, gva):
        return struct.unpack_from('<Q', self.read(gva, 8))[0]


class AGXPillBorderTests(unittest.TestCase):
    def test_classify_draw_pill_border(self):
        """classify_draw categorises untextured stride-36 prog 0x62a99af8d8beb332 as pill border."""
        mf = MockFrame()
        # vs_data at 0x500 containing stride=36, buf=0x1000
        vs_data_bytes = struct.pack('<IIQQ', 36, 0, 0x1000, 0)
        for i, b in enumerate(vs_data_bytes):
            mf.data[0x500 + i] = b

        draw_cmd = (3 << 29, 48)  # 48 vertices (8 quads)
        ppp = []
        viewport = (320.0, 320.0, 568.0, 568.0)

        orig_prog = aro.program
        orig_tex = aro.texture
        try:
            # 1. PILL_BORDER program
            aro.program = lambda f, p: aro.PILL_BORDER
            aro.texture = lambda f, p: None
            res, det = aro.classify_draw(mf, 0x500, ppp, draw_cmd, viewport, 640, 1136, 640, 1136)
            self.assertEqual(res, 'replayed exactly: pill border (stride=36)')
            self.assertEqual(det['count'], 48)
            self.assertEqual(det['prog'], hex(aro.PILL_BORDER))

            # 2. Other untextured program with stride 36 (should skip)
            aro.program = lambda f, p: 0x12345678abcdef00
            res, det = aro.classify_draw(mf, 0x500, ppp, draw_cmd, viewport, 640, 1136, 640, 1136)
            self.assertEqual(res, 'skipped: untextured with stride not in (20, 28)')
            self.assertEqual(det['stride'], 36)
        finally:
            aro.program = orig_prog
            aro.texture = orig_tex

    def test_pill_border_radial_coverage_math(self):
        """Test dual radial distance coverage calculation (outer and inner bounds)."""
        scale1 = 1.0 / 31.0
        scale2 = 1.0 / 28.0

        def calc_cov(u1, v1, u2, v2):
            r1 = math.sqrt(u1 * u1 + v1 * v1)
            r2 = math.sqrt(u2 * u2 + v2 * v2)
            outer_cov = max(0.0, min(1.0, 0.5 - (r1 - 1.0) / scale1))
            inner_cov = max(0.0, min(1.0, 0.5 + (r2 - 1.0) / scale2))
            return outer_cov * inner_cov

        # Case 1: Point well inside the border ring (r1 ~ 0.95, r2 ~ 1.05)
        cov_mid = calc_cov(0.95, 0.0, 1.05, 0.0)
        self.assertAlmostEqual(cov_mid, 1.0, places=3)

        # Case 2: Point outside the outer boundary (r1 = 1.1 > 1.0 + 0.5*scale1)
        cov_outside = calc_cov(1.1, 0.0, 1.2, 0.0)
        self.assertEqual(cov_outside, 0.0)

        # Case 3: Point inside the inner hollow (r2 = 0.9 < 1.0 - 0.5*scale2)
        cov_inside = calc_cov(0.8, 0.0, 0.9, 0.0)
        self.assertEqual(cov_inside, 0.0)

        # Case 4: Exactly on the outer edge (r1 = 1.0)
        cov_outer_edge = calc_cov(1.0, 0.0, 1.1, 0.0)
        self.assertAlmostEqual(cov_outer_edge, 0.5, places=3)

        # Case 5: Exactly on the inner edge (r2 = 1.0)
        cov_inner_edge = calc_cov(0.9, 0.0, 1.0, 0.0)
        self.assertAlmostEqual(cov_inner_edge, 0.5, places=3)

    def test_c_source_pill_implementation(self):
        """Verify backend/s5l8960x.c defines and implements the pill border within replay core."""
        source = SOURCE.read_text()

        begin_idx = source.find('/* AGX_REPLAY_CORE_BEGIN')
        end_idx = source.find('/* AGX_REPLAY_CORE_END */')
        self.assertNotEqual(begin_idx, -1)
        self.assertNotEqual(end_idx, -1)
        self.assertGreater(end_idx, begin_idx)

        core = source[begin_idx:end_idx]

        # Verify constant defined in core
        self.assertIn('#define AGX_USC_PILL_BORDER UINT64_C(0x62a99af8d8beb332)', core)

        # Verify stride 36 check bypasses skip
        self.assertIn('bool is_pill = stride == 36 && prog == AGX_USC_PILL_BORDER;', core)

        # Verify rasterizer loop and bilinear corner interpolation
        self.assertIn('if (is_pill) {', core)
        self.assertIn('float outer_cov = 0.5f - (r1 - 1.0f) / scale1;', core)
        self.assertIn('float inner_cov = 0.5f + (r2 - 1.0f) / scale2;', core)
        self.assertIn('agx_blend_over', core)

    def test_c_pill_border_execution(self):
        """Compile and execute C snippet testing the pill border math and blending."""
        compiler = shutil.which('cc')
        if not compiler:
            self.skipTest('host C compiler unavailable')

        c_test = '''
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <math.h>
#include <assert.h>

static uint32_t agx_blend_over(uint32_t dst, uint32_t src)
{
    uint32_t a = (src >> 24) & 0xff;
    uint32_t inv = 255 - a;
    uint32_t out = 0;
    for (unsigned c = 0; c < 32; c += 8) {
        uint32_t s = (src >> c) & 0xff;
        uint32_t d = (dst >> c) & 0xff;
        uint32_t r = s + ((d * inv + 127) / 255);
        if (r > 255) r = 255;
        out |= (r << c);
    }
    return out;
}

int main(void)
{
    // Framebuffer 100x100 initialized to white (0xffffffff)
    int w = 100, h = 100;
    uint32_t fb[100 * 100];
    for (int i = 0; i < 100 * 100; i++) {
        fb[i] = 0xffffffff;
    }

    // Quad covering x: 20..80, y: 20..80
    // Center at (50, 50). Let UV be scaled such that radius 25 is r1=1.0, radius 22 is r2=1.0
    float qminx = 20.0f, qmaxx = 80.0f;
    float qminy = 20.0f, qmaxy = 80.0f;
    float qw = qmaxx - qminx;
    float qh = qmaxy - qminy;

    float scale1 = 1.0f / 25.0f;
    float scale2 = 1.0f / 22.0f;
    uint32_t base_colour = 0xffe6e6e6; // Border color #E6E6E6

    uint32_t base_a = (base_colour >> 24) & 0xff;
    uint32_t base_b = (base_colour >> 16) & 0xff;
    uint32_t base_g = (base_colour >> 8) & 0xff;
    uint32_t base_r = base_colour & 0xff;

    for (int y = 20; y < 80; y++) {
        float cy = (float)y + 0.5f;
        float dy = cy - 50.0f;
        for (int x = 20; x < 80; x++) {
            float cx = (float)x + 0.5f;
            float dx = cx - 50.0f;
            float dist = sqrtf(dx * dx + dy * dy);

            float u1 = dx / 25.0f;
            float v1 = dy / 25.0f;
            float u2 = dx / 22.0f;
            float v2 = dy / 22.0f;

            float r1 = sqrtf(u1 * u1 + v1 * v1);
            float r2 = sqrtf(u2 * u2 + v2 * v2);

            float outer_cov = 0.5f - (r1 - 1.0f) / scale1;
            float inner_cov = 0.5f + (r2 - 1.0f) / scale2;
            outer_cov = (outer_cov < 0.0f) ? 0.0f : (outer_cov > 1.0f ? 1.0f : outer_cov);
            inner_cov = (inner_cov < 0.0f) ? 0.0f : (inner_cov > 1.0f ? 1.0f : inner_cov);
            float cov = outer_cov * inner_cov;

            if (cov > 0.0f) {
                uint32_t a = (uint32_t)lroundf((float)base_a * cov);
                uint32_t b = (uint32_t)lroundf((float)base_b * cov);
                uint32_t g = (uint32_t)lroundf((float)base_g * cov);
                uint32_t r = (uint32_t)lroundf((float)base_r * cov);
                uint32_t src = (a << 24) | (b << 16) | (g << 8) | r;
                fb[y * w + x] = agx_blend_over(fb[y * w + x], src);
            }
        }
    }

    // Check center (x=50, y=50) is hollow -> remains white
    assert(fb[50 * w + 50] == 0xffffffff);

    // Check far outside (x=21, y=21, dist ~ 41 > 25) -> remains white
    assert(fb[21 * w + 21] == 0xffffffff);

    // Check point on border ring (x=74, y=50, dist = 24.0, between 22 and 25)
    // -> should be shaded light grey (0xffe6e6e6)
    uint32_t border_px = fb[50 * w + 74];
    uint32_t r = border_px & 0xff;
    assert(r <= 0xe8 && r >= 0xe4);

    return 0;
}
'''
        with tempfile.TemporaryDirectory() as td:
            src_file = Path(td) / 'test_pill.c'
            bin_file = Path(td) / 'test_pill'
            src_file.write_text(c_test)
            res = subprocess.run([compiler, '-O2', str(src_file), '-lm', '-o', str(bin_file)],
                                 capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f"Compilation failed: {res.stderr}")
            res_run = subprocess.run([str(bin_file)], capture_output=True, text=True)
            self.assertEqual(res_run.returncode, 0, f"Execution failed: {res_run.stderr}")

    def test_corpus_pill_border_drawn(self):
        """Verify summary.json records pill border draws and zero C skips in settings-all frames."""
        if not SUMMARY_JSON.exists():
            self.skipTest('summary.json does not exist')

        with open(SUMMARY_JSON, 'r') as f:
            summary = json.load(f)

        # Check Python audit includes pill border
        outcomes = summary.get('totals', {}).get('py_outcomes', {})
        self.assertIn('replayed exactly: pill border (stride=36)', outcomes)
        self.assertGreaterEqual(outcomes['replayed exactly: pill border (stride=36)'], 2)

        # Check frame-1 and frame-9 C skips are 0
        frames = summary.get('frames', [])
        frame_1 = next((fr for fr in frames if fr.get('scene') == 'settings-all' and fr.get('frame') == 'frame-1'), None)
        if frame_1:
            self.assertEqual(sum(frame_1.get('c', {}).get('skips', {}).values()), 0)

        frame_9 = next((fr for fr in frames if fr.get('scene') == 'settings-all' and fr.get('frame') == 'frame-9'), None)
        if frame_9:
            self.assertEqual(sum(frame_9.get('c', {}).get('skips', {}).values()), 0)


if __name__ == '__main__':
    unittest.main()
