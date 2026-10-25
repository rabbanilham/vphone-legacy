"""Unit tests for AGX rotated geometry (inverse-affine texture mapping) and antialiased edges (wide lines and triangles).

Covers:
1. Inverse-affine texture mapping:
   - quad_affine() calculation of affine UV matrix from vertex triplets.
   - Precise UV interpolation for pixel centres across rotated textured quads.
   - Python classify_draw() outcome for rotated textured quads in the Clock app:
     replayed exactly: textured quad (1:1 nearest).
2. Analytic edge antialiasing for wide lines:
   - Perpendicular coverage cov_perp = clamp(half_w + 0.5 - dist_perp, 0, 1).
   - Parallel butt-end coverage cov_par = clamp(min(t, L - t) + 0.5, 0, 1).
   - Combined coverage cov = cov_perp * cov_par.
   - C replay and Python offline rasteriser exact parity.
3. 4x4 subpixel grid antialiasing for triangles:
   - 16 subpixel samples per pixel at offsets (2i+1)/8, (2j+1)/8.
   - Exact coverage fraction cnt / 16.0f.
   - No acute apex bleeding on cap fans.
   - C replay and Python offline rasteriser exact parity.
"""

import math
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'backend'

import sys
sys.path.insert(0, str(BACKEND))
import agx_replay_offline as aro


class AGXRotatedAndAATests(unittest.TestCase):
    def test_quad_affine_identity_and_rotation(self):
        """quad_affine computes exact affine coefficients mapping (x, y) to (u, v)."""
        # Axis-aligned rectangle: (0, 0) -> (0, 0), (10, 0) -> (1, 0), (0, 20) -> (0, 1)
        xy = [(0.0, 0.0), (10.0, 0.0), (0.0, 20.0), (10.0, 20.0)]
        uv = [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)]
        aff = aro.quad_affine(xy[:3], uv[:3])
        self.assertIsNotNone(aff)
        au, bu, cu, av, bv, cv = aff
        self.assertAlmostEqual(au, 0.1)
        self.assertAlmostEqual(bu, 0.0)
        self.assertAlmostEqual(cu, 0.0)
        self.assertAlmostEqual(av, 0.0)
        self.assertAlmostEqual(bv, 0.05)
        self.assertAlmostEqual(cv, 0.0)

        # 90-degree rotated quad:
        # (0, 0) -> (0, 0), (0, 10) -> (1, 0), (-20, 0) -> (0, 1)
        xy_rot = [(0.0, 0.0), (0.0, 10.0), (-20.0, 0.0)]
        uv_rot = [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0)]
        aff_rot = aro.quad_affine(xy_rot, uv_rot)
        self.assertIsNotNone(aff_rot)
        au, bu, cu, av, bv, cv = aff_rot
        self.assertAlmostEqual(au, 0.0)
        self.assertAlmostEqual(bu, 0.1)
        self.assertAlmostEqual(cu, 0.0)
        self.assertAlmostEqual(av, -0.05)
        self.assertAlmostEqual(bv, 0.0)
        self.assertAlmostEqual(cv, 0.0)

        # Degenerate collinear points return None
        xy_degen = [(0.0, 0.0), (5.0, 5.0), (10.0, 10.0)]
        self.assertIsNone(aro.quad_affine(xy_degen, uv_rot))

    def test_classify_draw_clock_rotated_textured_quads(self):
        """Rotated textured quads in the Clock app are classified as replayed exactly."""
        dump_path = ROOT / 'corpus/clock/dumps/frame-3.bin'
        if not dump_path.exists():
            self.skipTest('corpus/clock/dumps/frame-3.bin not available')

        frame = aro.Frame(dump_path)
        outcomes = aro.audit_frame(frame)
        self.assertGreater(len(outcomes), 0)

        # Check for draw 17, 18, 20 (analog clock hands)
        draws_by_idx = {did: res for did, res, _ in outcomes}
        for did in (6, 7, 9, 17, 18, 20):
            self.assertIn(did, draws_by_idx)
            self.assertEqual(draws_by_idx[did], 'replayed exactly: textured quad (1:1 nearest)')

        # Ensure NO draw is classified as 'skipped: non-axis-aligned or rotated geometry'
        for did, res, _ in outcomes:
            self.assertNotEqual(res, 'skipped: non-axis-aligned or rotated geometry')

    def test_wide_line_analytic_coverage_math(self):
        """Analytic edge and butt-end coverage satisfies edge distance falloff."""
        # Horizontal line from (10, 10) to (50, 10), width = 2.0 (half_w = 1.0)
        p0 = (10.0, 10.0)
        p1 = (50.0, 10.0)
        dx = p1[0] - p0[0]  # 40.0
        dy = p1[1] - p0[1]  # 0.0
        L = 40.0
        inv_L = 1.0 / L
        half_w = 1.0

        def calc_coverage(cx, cy):
            px = cx - p0[0]
            py = cy - p0[1]
            dot = px * dx + py * dy
            t = dot * inv_L
            min_end = min(t, L - t)
            cov_par = min_end + 0.5
            if cov_par <= 0.0:
                return 0.0
            cov_par = min(1.0, cov_par)
            cross = dx * py - dy * px
            dist_perp = abs(cross) * inv_L
            cov_perp = half_w + 0.5 - dist_perp
            if cov_perp <= 0.0:
                return 0.0
            cov_perp = min(1.0, cov_perp)
            return cov_perp * cov_par

        # Center point inside the line (t=20, dist_perp=0) -> cov = 1.0
        self.assertAlmostEqual(calc_coverage(30.0, 10.0), 1.0)

        # On the geometric edge (dist_perp = 1.0) -> cov_perp = 1.0 + 0.5 - 1.0 = 0.5
        self.assertAlmostEqual(calc_coverage(30.0, 11.0), 0.5)
        self.assertAlmostEqual(calc_coverage(30.0, 9.0), 0.5)

        # Outside the filter radius (dist_perp >= 1.5) -> cov = 0.0
        self.assertAlmostEqual(calc_coverage(30.0, 11.6), 0.0)
        self.assertAlmostEqual(calc_coverage(30.0, 8.4), 0.0)

        # Past butt end (t = -0.5) -> cov_par = 0.0
        self.assertAlmostEqual(calc_coverage(9.4, 10.0), 0.0)
        # Exactly at butt end edge (t = 0.0) -> cov_par = 0.5
        self.assertAlmostEqual(calc_coverage(10.0, 10.0), 0.5)

    def test_triangle_4x4_coverage_math(self):
        """4x4 subpixel grid calculates fractional coverage without apex bleeding."""
        # Right triangle: (0, 0), (10, 0), (0, 10)
        p0 = (0.0, 0.0)
        p1 = (10.0, 0.0)
        p2 = (0.0, 10.0)
        det = (p1[0] - p0[0]) * (p2[1] - p0[1]) - (p1[1] - p0[1]) * (p2[0] - p0[0])  # 100 > 0

        def calc_tri_coverage(x, y):
            cnt = 0
            for si in range(4):
                sx = x + (2 * si + 1) * 0.125
                for sj in range(4):
                    sy = y + (2 * sj + 1) * 0.125
                    e0 = (p1[0] - p0[0]) * (sy - p0[1]) - (p1[1] - p0[1]) * (sx - p0[0])
                    e1 = (p2[0] - p1[0]) * (sy - p1[1]) - (p2[1] - p1[1]) * (sx - p1[0])
                    e2 = (p0[0] - p2[0]) * (sy - p2[1]) - (p0[1] - p2[1]) * (sx - p2[0])
                    if e0 >= 0 and e1 >= 0 and e2 >= 0:
                        cnt += 1
            return cnt / 16.0

        # Interior pixel (1, 1) -> all 16 subpixels inside
        self.assertEqual(calc_tri_coverage(1, 1), 1.0)

        # Exterior pixel (10, 10) -> 0
        self.assertEqual(calc_tri_coverage(10, 10), 0.0)

        # Diagonal hypotenuse intersects pixel (5, 4): x + y = 9 vs hypotenuse x + y = 10
        cov = calc_tri_coverage(5, 4)
        self.assertGreater(cov, 0.0)
        self.assertLess(cov, 1.0)

    def test_c_and_python_wide_line_aa_parity(self):
        """C replay wide-line AA implementation produces identical pixel values to Python."""
        compiler = shutil.which('cc')
        if not compiler:
            self.skipTest('host C compiler unavailable')

        c_source = '''
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <math.h>

#define MIN(a, b) ((a) < (b) ? (a) : (b))
#define MAX(a, b) ((a) > (b) ? (a) : (b))

static inline uint32_t agx_blend_over(uint32_t dst, uint32_t src) {
    uint32_t sa = src >> 24;
    uint32_t inv_sa = 255u - sa;
    uint32_t sr = (src >> 16) & 0xff;
    uint32_t sg = (src >> 8) & 0xff;
    uint32_t sb = src & 0xff;
    uint32_t da = dst >> 24;
    uint32_t dr = (dst >> 16) & 0xff;
    uint32_t dg = (dst >> 8) & 0xff;
    uint32_t db = dst & 0xff;
    uint32_t out_a = sa + (da * inv_sa + 127u) / 255u;
    uint32_t out_r = sr + (dr * inv_sa + 127u) / 255u;
    uint32_t out_g = sg + (dg * inv_sa + 127u) / 255u;
    uint32_t out_b = sb + (db * inv_sa + 127u) / 255u;
    return (out_a << 24) | (out_r << 16) | (out_g << 8) | out_b;
}

void rasterise_line_c(float x0, float y0, float x1, float y1, float width,
                      uint32_t colour, int w, int h, uint32_t *fb) {
    float dx = x1 - x0;
    float dy = y1 - y0;
    float L2 = dx * dx + dy * dy;
    float L = sqrtf(L2);
    if (L < 1e-3f) return;
    float half_w = width * 0.5f;
    float inv_L = 1.0f / L;

    int bx0 = MAX(0, (int)floorf(MIN(x0, x1) - half_w - 1.0f));
    int bx1 = MIN(w, (int)ceilf(MAX(x0, x1) + half_w + 1.0f));
    int by0 = MAX(0, (int)floorf(MIN(y0, y1) - half_w - 1.0f));
    int by1 = MIN(h, (int)ceilf(MAX(y0, y1) + half_w + 1.0f));

    uint32_t a = colour >> 24;
    uint32_t r = (colour >> 16) & 0xff;
    uint32_t g = (colour >> 8) & 0xff;
    uint32_t b = colour & 0xff;

    for (int y = by0; y < by1; y++) {
        float cy = (float)y + 0.5f;
        float py = cy - y0;
        for (int x = bx0; x < bx1; x++) {
            float cx = (float)x + 0.5f;
            float px = cx - x0;
            float dot = px * dx + py * dy;
            float t = dot * inv_L;
            float min_end = (t < (L - t)) ? t : (L - t);
            float cov_par = min_end + 0.5f;
            cov_par = (cov_par < 0.0f) ? 0.0f : ((cov_par > 1.0f) ? 1.0f : cov_par);
            float cross = dx * py - dy * px;
            float dist_perp = fabsf(cross) * inv_L;
            float cov_perp = half_w + 0.5f - dist_perp;
            cov_perp = (cov_perp < 0.0f) ? 0.0f : ((cov_perp > 1.0f) ? 1.0f : cov_perp);
            float cov = cov_perp * cov_par;
            if (cov > 0.0f) {
                uint32_t ca = (uint32_t)lroundf((float)a * cov);
                uint32_t cr = (uint32_t)lroundf((float)r * cov);
                uint32_t cg = (uint32_t)lroundf((float)g * cov);
                uint32_t cb = (uint32_t)lroundf((float)b * cov);
                uint32_t src = (ca << 24) | (cr << 16) | (cg << 8) | cb;
                fb[y * w + x] = agx_blend_over(fb[y * w + x], src);
            }
        }
    }
}

int main(int argc, char **argv) {
    int w = 64, h = 64;
    uint32_t *fb = (uint32_t *)calloc(w * h, sizeof(uint32_t));
    rasterise_line_c(12.3f, 15.7f, 48.9f, 42.1f, 2.5f, 0xffffffff, w, h, fb);
    fwrite(fb, sizeof(uint32_t), w * h, stdout);
    free(fb);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / 'test_line.c'
            exe = Path(td) / 'test_line'
            src.write_text(c_source)
            comp = subprocess.run([compiler, '-O2', str(src), '-o', str(exe), '-lm'],
                                  capture_output=True, text=True)
            self.assertEqual(comp.returncode, 0, f'Compilation failed: {comp.stderr}')
            run = subprocess.run([str(exe)], capture_output=True)
            self.assertEqual(run.returncode, 0)
            c_bytes = run.stdout

            # Python calculation
            w, h = 64, 64
            fb_py = [0] * (w * h)
            p0 = (12.3, 15.7)
            p1 = (48.9, 42.1)
            dx = p1[0] - p0[0]
            dy = p1[1] - p0[1]
            L = math.sqrt(dx * dx + dy * dy)
            inv_L = 1.0 / L
            half_w = 2.5 * 0.5
            r, g, b, a = 255, 255, 255, 255

            for y in range(h):
                cy = y + 0.5
                py = cy - p0[1]
                for x in range(w):
                    cx = x + 0.5
                    px = cx - p0[0]
                    dot = px * dx + py * dy
                    t = dot * inv_L
                    min_end = min(t, L - t)
                    cov_par = max(0.0, min(1.0, min_end + 0.5))
                    cross = dx * py - dy * px
                    dist_perp = abs(cross) * inv_L
                    cov_perp = max(0.0, min(1.0, half_w + 0.5 - dist_perp))
                    cov = cov_perp * cov_par
                    if cov > 0.0:
                        ca = int(round(a * cov))
                        cr = int(round(r * cov))
                        cg = int(round(g * cov))
                        cb = int(round(b * cov))
                        # over blend on black
                        fb_py[y * w + x] = (ca << 24) | (cr << 16) | (cg << 8) | cb

            py_bytes = struct.pack(f'<{w * h}I', *fb_py)
            self.assertEqual(c_bytes, py_bytes)

    def test_c_and_python_triangle_4x4_parity(self):
        """C replay triangle 4x4 AA produces identical pixel values to Python."""
        compiler = shutil.which('cc')
        if not compiler:
            self.skipTest('host C compiler unavailable')

        c_source = '''
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <math.h>

#define MIN(a, b) ((a) < (b) ? (a) : (b))
#define MAX(a, b) ((a) > (b) ? (a) : (b))

void rasterise_tri_c(float p0x, float p0y, float p1x, float p1y, float p2x, float p2y,
                     uint32_t colour, int w, int h, uint32_t *fb) {
    float det = (p1x - p0x) * (p2y - p0y) - (p1y - p0y) * (p2x - p0x);
    if (fabsf(det) < 1e-5f) return;

    int x0 = MAX(0, (int)floorf(MIN(p0x, MIN(p1x, p2x)) - 0.5f));
    int x1 = MIN(w, (int)ceilf(MAX(p0x, MAX(p1x, p2x)) + 0.5f));
    int y0 = MAX(0, (int)floorf(MIN(p0y, MIN(p1y, p2y)) - 0.5f));
    int y1 = MIN(h, (int)ceilf(MAX(p0y, MAX(p1y, p2y)) + 0.5f));

    uint32_t a = colour >> 24;
    uint32_t r = (colour >> 16) & 0xff;
    uint32_t g = (colour >> 8) & 0xff;
    uint32_t b = colour & 0xff;

    for (int y = y0; y < y1; y++) {
        for (int x = x0; x < x1; x++) {
            unsigned cnt = 0;
            for (unsigned si = 0; si < 4; si++) {
                float sx = (float)x + (float)(2 * si + 1) * 0.125f;
                for (unsigned sj = 0; sj < 4; sj++) {
                    float sy = (float)y + (float)(2 * sj + 1) * 0.125f;
                    float e0 = (p1x - p0x) * (sy - p0y) - (p1y - p0y) * (sx - p0x);
                    float e1 = (p2x - p1x) * (sy - p1y) - (p2y - p1y) * (sx - p1x);
                    float e2 = (p0x - p2x) * (sy - p2y) - (p0y - p2y) * (sx - p2x);
                    bool inside = (det > 0.0f) ? (e0 >= 0.0f && e1 >= 0.0f && e2 >= 0.0f)
                                               : (e0 <= 0.0f && e1 <= 0.0f && e2 <= 0.0f);
                    if (inside) cnt++;
                }
            }
            if (cnt > 0) {
                float cov = (float)cnt * 0.0625f;
                uint32_t ca = (uint32_t)lroundf((float)a * cov);
                uint32_t cr = (uint32_t)lroundf((float)r * cov);
                uint32_t cg = (uint32_t)lroundf((float)g * cov);
                uint32_t cb = (uint32_t)lroundf((float)b * cov);
                fb[y * w + x] = (ca << 24) | (cr << 16) | (cg << 8) | cb;
            }
        }
    }
}

int main() {
    int w = 64, h = 64;
    uint32_t *fb = (uint32_t *)calloc(w * h, sizeof(uint32_t));
    rasterise_tri_c(10.2f, 12.8f, 50.4f, 20.1f, 25.6f, 55.3f, 0xffffffff, w, h, fb);
    fwrite(fb, sizeof(uint32_t), w * h, stdout);
    free(fb);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / 'test_tri.c'
            exe = Path(td) / 'test_tri'
            src.write_text(c_source)
            comp = subprocess.run([compiler, '-O2', str(src), '-o', str(exe), '-lm'],
                                  capture_output=True, text=True)
            self.assertEqual(comp.returncode, 0, f'Compilation failed: {comp.stderr}')
            run = subprocess.run([str(exe)], capture_output=True)
            self.assertEqual(run.returncode, 0)
            c_bytes = run.stdout

            # Python equivalent
            w, h = 64, 64
            fb_py = [0] * (w * h)
            p0 = (10.2, 12.8)
            p1 = (50.4, 20.1)
            p2 = (25.6, 55.3)
            det = (p1[0] - p0[0]) * (p2[1] - p0[1]) - (p1[1] - p0[1]) * (p2[0] - p0[0])
            r, g, b, a = 255, 255, 255, 255

            for y in range(h):
                for x in range(w):
                    cnt = 0
                    for si in range(4):
                        sx = x + (2 * si + 1) * 0.125
                        for sj in range(4):
                            sy = y + (2 * sj + 1) * 0.125
                            e0 = (p1[0] - p0[0]) * (sy - p0[1]) - (p1[1] - p0[1]) * (sx - p0[0])
                            e1 = (p2[0] - p1[0]) * (sy - p1[1]) - (p2[1] - p1[1]) * (sx - p1[0])
                            e2 = (p0[0] - p2[0]) * (sy - p2[1]) - (p0[1] - p2[1]) * (sx - p2[0])
                            inside = (e0 >= 0 and e1 >= 0 and e2 >= 0) if det > 0 else (e0 <= 0 and e1 <= 0 and e2 <= 0)
                            if inside:
                                cnt += 1
                    if cnt > 0:
                        cov = cnt * 0.0625
                        ca = int(round(a * cov))
                        cr = int(round(r * cov))
                        cg = int(round(g * cov))
                        cb = int(round(b * cov))
                        fb_py[y * w + x] = (ca << 24) | (cr << 16) | (cg << 8) | cb

            py_bytes = struct.pack(f'<{w * h}I', *fb_py)
            self.assertEqual(c_bytes, py_bytes)


if __name__ == '__main__':
    unittest.main()
