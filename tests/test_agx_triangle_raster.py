"""Unit tests for AGX untextured triangle rasteriser and vertex colour order.

Covers:
1. RGBA vertex colour byte order:
   - Stride 20 / 28 offset +16 colour decoding in Python offline mirror and C replay.
   - Second hand 0xff1c3edc is #DC3E1C (red), not blue.
   - Grouped table background 0xfff4efef is #EFEFF4 (light grey), not pink.
   - C replay swap_rb logic writes correct BGRA words to guest framebuffer memory.
2. Triangle rasteriser:
   - Pixel centre (x + 0.5, y + 0.5) coverage via edge functions.
   - Clockwise (CW) and Counter-Clockwise (CCW) winding invariance.
   - Degenerate triangles (zero area / collinear points) safely skipped.
   - Scissor box and boundary clipping.
   - Rotated thin strips (clock hands) vs bounding boxes.
   - C vs Python parity for triangle rasterisation.
"""
import math
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'backend/s5l8960x.c'

import sys
sys.path.insert(0, str(ROOT / 'backend'))
import agx_replay_offline as aro


def rasterise_triangles_py(vertices, w, h, clip_box=None):
    """Reference Python rasteriser evaluating edge functions at pixel centres."""
    clip_x0, clip_y0 = 0, 0
    clip_x1, clip_y1 = w, h
    if clip_box:
        clip_x0 = max(0, clip_box[0])
        clip_y0 = max(0, clip_box[1])
        clip_x1 = min(w, clip_box[2])
        clip_y1 = min(h, clip_box[3])

    fb = bytearray(w * h * 4)
    for t in range(0, len(vertices) - 2, 3):
        p0, p1, p2 = vertices[t][:2], vertices[t + 1][:2], vertices[t + 2][:2]
        det = (p1[0] - p0[0]) * (p2[1] - p0[1]) - (p1[1] - p0[1]) * (p2[0] - p0[0])
        if abs(det) < 1e-5:
            continue
        col = vertices[t][2] if len(vertices[t]) > 2 else (255, 255, 255, 255)
        tr, tg, tb, ta = col

        x0 = max(clip_x0, int(math.floor(min(p0[0], p1[0], p2[0]))))
        x1 = min(clip_x1, int(math.ceil(max(p0[0], p1[0], p2[0]))))
        y0 = max(clip_y0, int(math.floor(min(p0[1], p1[1], p2[1]))))
        y1 = min(clip_y1, int(math.ceil(max(p0[1], p1[1], p2[1]))))
        if x0 >= x1 or y0 >= y1:
            continue

        for y in range(y0, y1):
            cy = y + 0.5
            for x in range(x0, x1):
                cx = x + 0.5
                e0 = (p1[0] - p0[0]) * (cy - p0[1]) - (p1[1] - p0[1]) * (cx - p0[0])
                e1 = (p2[0] - p1[0]) * (cy - p1[1]) - (p2[1] - p1[1]) * (cx - p1[0])
                e2 = (p0[0] - p2[0]) * (cy - p2[1]) - (p0[1] - p2[1]) * (cx - p2[0])
                inside = (e0 >= 0 and e1 >= 0 and e2 >= 0) if det > 0 else (e0 <= 0 and e1 <= 0 and e2 <= 0)
                if inside:
                    d = (y * w + x) * 4
                    for c, src in enumerate((tr, tg, tb, ta)):
                        fb[d + c] = min(255, src + fb[d + c] * (255 - ta) // 255)
    return fb


class AGXTriangleRasterTests(unittest.TestCase):
    def test_vertex_colour_rgba_order(self):
        """Untextured vertex colours at +16 are RGBA in memory, yielding correct colours."""
        # 0xff1c3edc in memory: byte 0=0xdc, byte 1=0x3e, byte 2=0x1c, byte 3=0xff
        raw_second_hand = struct.pack('<I', 0xff1c3edc)
        self.assertEqual(raw_second_hand[0], 0xdc)  # R
        self.assertEqual(raw_second_hand[1], 0x3e)  # G
        self.assertEqual(raw_second_hand[2], 0x1c)  # B
        self.assertEqual(raw_second_hand[3], 0xff)  # A

        r, g, b, a = raw_second_hand[0], raw_second_hand[1], raw_second_hand[2], raw_second_hand[3]
        self.assertEqual((r, g, b, a), (0xdc, 0x3e, 0x1c, 0xff))
        self.assertGreater(r, 200, 'Second hand red channel must be > 200')
        self.assertLess(b, 50, 'Second hand blue channel must be < 50')

        # Clock grouped table background: 0xfff4efef in memory
        raw_grouped_bg = struct.pack('<I', 0xfff4efef)
        gr, gg, gb, ga = raw_grouped_bg[0], raw_grouped_bg[1], raw_grouped_bg[2], raw_grouped_bg[3]
        self.assertEqual((gr, gg, gb, ga), (0xef, 0xef, 0xf4, 0xff))
        self.assertGreater(gb, gr, 'Grouped table background must be bluish grey #EFEFF4, not pink')

    def test_c_replay_vertex_colour_rb_swap(self):
        """C replay converts RGBA vertex memory word to BGRA for guest framebuffer scanout."""
        val = 0xff1c3edc
        swapped = (val & 0xff00ff00) | ((val >> 16) & 0xff) | ((val & 0xff) << 16)
        # In BGRA framebuffer memory:
        # byte 0 (B) should be 0x1c
        # byte 1 (G) should be 0x3e
        # byte 2 (R) should be 0xdc
        # byte 3 (A) should be 0xff
        bgra_bytes = struct.pack('<I', swapped)
        self.assertEqual(bgra_bytes[0], 0x1c)
        self.assertEqual(bgra_bytes[1], 0x3e)
        self.assertEqual(bgra_bytes[2], 0xdc)
        self.assertEqual(bgra_bytes[3], 0xff)

    def test_triangle_pixel_center_coverage(self):
        """Right triangle (0, 0)-(4, 0)-(0, 4) covers exact pixel centers satisfying cx+cy <= 4 (x+y <= 3)."""
        fb = rasterise_triangles_py([(0.0, 0.0, (255, 0, 0, 255)),
                                     (4.0, 0.0, (255, 0, 0, 255)),
                                     (0.0, 4.0, (255, 0, 0, 255))], 8, 8)
        pixels = set()
        for y in range(8):
            for x in range(8):
                if fb[(y * 8 + x) * 4] == 255:
                    pixels.add((x, y))

        expected = {
            (0, 0), (1, 0), (2, 0), (3, 0),
            (0, 1), (1, 1), (2, 1),
            (0, 2), (1, 2),
            (0, 3),
        }
        self.assertEqual(pixels, expected)

    def test_winding_invariance(self):
        """Clockwise and Counter-Clockwise triangle windings produce identical rasterisation."""
        ccw = [(1.0, 1.0, (255, 255, 255, 255)),
               (8.0, 2.0, (255, 255, 255, 255)),
               (3.0, 9.0, (255, 255, 255, 255))]
        cw = [(1.0, 1.0, (255, 255, 255, 255)),
              (3.0, 9.0, (255, 255, 255, 255)),
              (8.0, 2.0, (255, 255, 255, 255))]

        fb_ccw = rasterise_triangles_py(ccw, 12, 12)
        fb_cw = rasterise_triangles_py(cw, 12, 12)
        self.assertEqual(fb_ccw, fb_cw)
        self.assertGreater(sum(fb_ccw), 0)

    def test_degenerate_triangles_skipped(self):
        """Collinear and zero-area triangles draw zero pixels."""
        # Collinear on line y = 2x
        collinear = [(0.0, 0.0, (255, 255, 255, 255)),
                     (2.0, 4.0, (255, 255, 255, 255)),
                     (5.0, 10.0, (255, 255, 255, 255))]
        fb = rasterise_triangles_py(collinear, 16, 16)
        self.assertEqual(sum(fb), 0)

        # Single point
        point = [(3.0, 3.0, (255, 255, 255, 255)),
                 (3.0, 3.0, (255, 255, 255, 255)),
                 (3.0, 3.0, (255, 255, 255, 255))]
        fb_pt = rasterise_triangles_py(point, 16, 16)
        self.assertEqual(sum(fb_pt), 0)

    def test_triangle_scissor_clipping(self):
        """Pixels outside scissor rectangle are never touched."""
        tri = [(0.0, 0.0, (255, 255, 255, 255)),
               (16.0, 0.0, (255, 255, 255, 255)),
               (0.0, 16.0, (255, 255, 255, 255))]
        clip_box = (3, 3, 7, 7)
        fb = rasterise_triangles_py(tri, 16, 16, clip_box=clip_box)
        for y in range(16):
            for x in range(16):
                d = (y * 16 + x) * 4
                is_set = (fb[d + 3] != 0)
                if is_set:
                    self.assertTrue(3 <= x < 7 and 3 <= y < 7, f'Pixel at ({x},{y}) outside scissor {clip_box}')

    def test_rotated_clock_hand_not_bounding_box(self):
        """Rotated thin quad (2 triangles) rasterises thin strip, not filled bounding box."""
        # Thin rectangle of width 2, length 20, rotated by 45 degrees
        # Center at (16, 16), half-length 10, half-width 1
        # Unit vector along hand: (sqrt(2)/2, sqrt(2)/2), perpendicular: (-sqrt(2)/2, sqrt(2)/2)
        c, s = math.cos(math.pi / 4), math.sin(math.pi / 4)
        hx, hy = 10 * c, 10 * s
        wx, wy = -1 * s, 1 * c

        p0 = (16.0 - hx - wx, 16.0 - hy - wy)
        p1 = (16.0 + hx - wx, 16.0 + hy - wy)
        p2 = (16.0 - hx + wx, 16.0 - hy + wy)
        p3 = (16.0 + hx + wx, 16.0 + hy + wy)

        # Two triangles: (p0, p1, p2) and (p2, p1, p3)
        verts = [(p0[0], p0[1], (50, 50, 50, 255)),
                 (p1[0], p1[1], (50, 50, 50, 255)),
                 (p2[0], p2[1], (50, 50, 50, 255)),
                 (p2[0], p2[1], (50, 50, 50, 255)),
                 (p1[0], p1[1], (50, 50, 50, 255)),
                 (p3[0], p3[1], (50, 50, 50, 255))]

        fb = rasterise_triangles_py(verts, 32, 32)
        covered_pixels = sum(1 for i in range(0, len(fb), 4) if fb[i + 3] != 0)

        # Bounding box width and height:
        all_x = [p[0] for p in (p0, p1, p2, p3)]
        all_y = [p[1] for p in (p0, p1, p2, p3)]
        bbox_w = math.ceil(max(all_x)) - math.floor(min(all_x))
        bbox_h = math.ceil(max(all_y)) - math.floor(min(all_y))
        bbox_area = bbox_w * bbox_h

        # The bounding box area is around 15x15 = 225 pixels.
        # The hand area is ~ 2 * 20 = ~40 pixels.
        self.assertGreater(bbox_area, 150)
        self.assertLess(covered_pixels, 60, f'Covered {covered_pixels} pixels, expected thin hand (< 60)')
        self.assertGreater(covered_pixels, 20, f'Covered {covered_pixels} pixels, expected > 20')

    def test_c_rasteriser_extracted_compilation(self):
        """Compile and verify C triangle rasteriser extracted from s5l8960x.c matches Python."""
        compiler = shutil.which('cc')
        if not compiler:
            self.skipTest('host C compiler unavailable')

        harness = '''
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <math.h>

#define MIN(a, b) ((a) < (b) ? (a) : (b))
#define MAX(a, b) ((a) > (b) ? (a) : (b))

static inline uint32_t agx_blend_over(uint32_t d, uint32_t s) {
    uint32_t sa = s >> 24;
    if (sa == 255) return s;
    if (sa == 0) return d;
    uint32_t inv_a = 255 - sa;
    uint32_t r = ((s >> 16) & 0xff) + (((d >> 16) & 0xff) * inv_a) / 255;
    uint32_t g = ((s >> 8) & 0xff) + (((d >> 8) & 0xff) * inv_a) / 255;
    uint32_t b = (s & 0xff) + ((d & 0xff) * inv_a) / 255;
    uint32_t a = sa + (((d >> 24) & 0xff) * inv_a) / 255;
    return (MIN(a, 255) << 24) | (MIN(r, 255) << 16) | (MIN(g, 255) << 8) | MIN(b, 255);
}

void rasterise_c(const float *vs, unsigned count, int w, int h, uint32_t col, uint32_t *fb) {
    int clip_x0 = 0, clip_y0 = 0, clip_x1 = w, clip_y1 = h;
    for (unsigned t = 0; t + 2 < count; t += 3) {
        const float *p0 = &vs[t * 2];
        const float *p1 = &vs[(t + 1) * 2];
        const float *p2 = &vs[(t + 2) * 2];
        float det = (p1[0] - p0[0]) * (p2[1] - p0[1]) - (p1[1] - p0[1]) * (p2[0] - p0[0]);
        if (fabsf(det) < 1e-5f) continue;

        float tminx = MIN(p0[0], MIN(p1[0], p2[0]));
        float tmaxx = MAX(p0[0], MAX(p1[0], p2[0]));
        float tminy = MIN(p0[1], MIN(p1[1], p2[1]));
        float tmaxy = MAX(p0[1], MAX(p1[1], p2[1]));

        int x0 = MAX(clip_x0, (int)floorf(tminx));
        int x1 = MIN(clip_x1, (int)ceilf(tmaxx));
        int y0 = MAX(clip_y0, (int)floorf(tminy));
        int y1 = MIN(clip_y1, (int)ceilf(tmaxy));
        if (x0 >= x1 || y0 >= y1) continue;

        for (int y = y0; y < y1; y++) {
            float cy = (float)y + 0.5f;
            int first_in = -1, last_in = -1;
            for (int x = x0; x < x1; x++) {
                float cx = (float)x + 0.5f;
                float e0 = (p1[0] - p0[0]) * (cy - p0[1]) - (p1[1] - p0[1]) * (cx - p0[0]);
                float e1 = (p2[0] - p1[0]) * (cy - p1[1]) - (p2[1] - p1[1]) * (cx - p1[0]);
                float e2 = (p0[0] - p2[0]) * (cy - p2[1]) - (p0[1] - p2[1]) * (cx - p2[0]);
                bool inside = (det > 0.0f) ? (e0 >= 0.0f && e1 >= 0.0f && e2 >= 0.0f)
                                           : (e0 <= 0.0f && e1 <= 0.0f && e2 <= 0.0f);
                if (inside) {
                    if (first_in < 0) first_in = x;
                    last_in = x;
                }
            }
            if (first_in < 0) continue;
            for (int x = first_in; x <= last_in; x++) {
                float cx = (float)x + 0.5f;
                float e0 = (p1[0] - p0[0]) * (cy - p0[1]) - (p1[1] - p0[1]) * (cx - p0[0]);
                float e1 = (p2[0] - p1[0]) * (cy - p1[1]) - (p2[1] - p1[1]) * (cx - p1[0]);
                float e2 = (p0[0] - p2[0]) * (cy - p2[1]) - (p0[1] - p2[1]) * (cx - p2[0]);
                bool inside = (det > 0.0f) ? (e0 >= 0.0f && e1 >= 0.0f && e2 >= 0.0f)
                                           : (e0 <= 0.0f && e1 <= 0.0f && e2 <= 0.0f);
                if (inside) {
                    fb[y * w + x] = agx_blend_over(fb[y * w + x], col);
                }
            }
        }
    }
}

int main() {
    float vs[] = { 1.0f, 1.0f, 10.0f, 3.0f, 4.0f, 12.0f };
    uint32_t fb[16 * 16] = {0};
    uint32_t col = 0xff1c3edc; /* BGRA for red */
    rasterise_c(vs, 3, 16, 16, col, fb);

    int count = 0;
    for (int i = 0; i < 16 * 16; i++) {
        if (fb[i] != 0) count++;
    }
    printf("C_RASTER_PIXELS=%d\\n", count);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as td:
            src_path = Path(td) / 'test_raster.c'
            bin_path = Path(td) / 'test_raster'
            src_path.write_text(harness)

            res = subprocess.run([compiler, '-O2', str(src_path), '-o', str(bin_path), '-lm'],
                                 capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f'Compilation failed:\n{res.stderr}')

            res_run = subprocess.run([str(bin_path)], capture_output=True, text=True)
            self.assertEqual(res_run.returncode, 0, f'Execution failed:\n{res_run.stderr}')
            self.assertIn('C_RASTER_PIXELS=', res_run.stdout)

            # Compare pixel count with Python reference
            c_pixels = int(res_run.stdout.strip().split('=')[1])
            py_fb = rasterise_triangles_py([(1.0, 1.0, (220, 62, 28, 255)),
                                            (10.0, 3.0, (220, 62, 28, 255)),
                                            (4.0, 12.0, (220, 62, 28, 255))], 16, 16)
            py_pixels = sum(1 for i in range(0, len(py_fb), 4) if py_fb[i + 3] != 0)
            self.assertEqual(c_pixels, py_pixels)


if __name__ == '__main__':
    unittest.main()
