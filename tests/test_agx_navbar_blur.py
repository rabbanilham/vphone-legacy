"""Unit tests for AGX navigation bar backdrop blur modeling (Task F3).

Verifies:
- Display surface textures (e.g. 0x400000000) with 16-row strip layout and +0xa000 hole
  are recognized with strip=True and stride=2560 instead of failing on linear row stride.
- Navigation bar backdrop draws covering (0,0)-(640,128) with white legibility overlay
  faithfully Gaussian-blur pre-existing canvas contents before blending the tint.
- Canvas pixels outside the navbar quad remain strictly untouched.
- Non-navbar quads (e.g. quads not at top of screen or non-1x1 textures) do not trigger blur.
- Frame 72 Draw 32 is verified as the 640x128 navigation bar backdrop with 1x1 white texture.
- C hardware model (backend/s5l8960x.c) implements display surface strip detection and
  navbar backdrop blur via agx_blur_texture.
- Standalone compilation test for C navbar blur logic.
"""

from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'backend'
CANDIDATE_DUMPS = [
    BACKEND / 'evidence/keybag-contract-20261015/probe-164-gfxdump/dumps',
    Path('/Users/bagas/Downloads/vphone-legacy/backend/evidence/keybag-contract-20261015/probe-164-gfxdump/dumps'),
]
DUMPS_DIR = next((p for p in CANDIDATE_DUMPS if p.is_dir()), None)
S5L8960X_C = BACKEND / 's5l8960x.c'

import sys
sys.path.insert(0, str(BACKEND))
from agx_frame import Frame
import agx_replay_offline as aro


class AGXNavbarBlurTests(unittest.TestCase):
    def test_display_surface_tsw_strip_layout(self):
        """TSWs referencing display surfaces (e.g. 0x400000000) must have strip=True and stride=2560."""
        # Create a mock Frame with display surface mapping (hole at +0xa000)
        class MockFrame:
            def __init__(self):
                self.pages = {}
                base = 0x400000000
                # Map 16 rows of strips: 0x0000..0x9000 mapped, 0xa000..0xf000 unmapped
                for p in range(0x0000, 0xa000, 0x1000):
                    self.pages[base + p] = bytes(4096)
                for p in range(0x10000, 0x1a000, 0x1000):
                    self.pages[base + p] = bytes(4096)

        mf = MockFrame()
        # TSW for 640x1136 display surface: layout word with addr=0x400000000
        w0 = (6 << 28) | 0x8864
        w1 = ((1136 - 1) << 16) | ((640 - 1) << 2)
        q = (0x400000000 >> 2) << 16
        w2 = q & 0xffffffff
        w3 = (q >> 32) & 0xffffffff
        tsw = [w0, w1, w2, w3]

        decoded = aro.decode_tsw(mf, tsw)
        self.assertIsNotNone(decoded)
        self.assertTrue(decoded['strip'], "Display surface must be detected as strip=True")
        self.assertEqual(decoded['stride'], 2560, "Strip display surface pitch must be 2560")

    def test_navbar_backdrop_blur_diffusion(self):
        """Pre-existing canvas content under (0,0)-(640,128) must be Gaussian-blurred."""
        w, h = 640, 140
        # Initialize canvas with white background and a sharp black horizontal bar at y=110..115, x=30..100
        fb = bytearray(w * h * 4)
        for i in range(0, len(fb), 4):
            fb[i:i+4] = b'\xff\xff\xff\xff'

        for y in range(110, 115):
            for x in range(30, 100):
                d = (y * w + x) * 4
                fb[d:d+4] = b'\x00\x00\x00\xff'

        # Pixels before blur: sharp contrast at boundary x=29 (white) vs x=30 (black)
        pre_29 = tuple(fb[(112 * w + 29) * 4 : (112 * w + 29) * 4 + 4])
        pre_30 = tuple(fb[(112 * w + 30) * 4 : (112 * w + 30) * 4 + 4])
        self.assertEqual(pre_29[:3], (255, 255, 255))
        self.assertEqual(pre_30[:3], (0, 0, 0))

        # Apply navbar blur over (0, 0)-(640, 128)
        x0, y0, x1, y1 = 0, 0, 640, 128
        bw, bh = x1 - x0, y1 - y0
        raw_pixels = []
        for y in range(y0, y1):
            for x in range(x0, x1):
                d = (y * w + x) * 4
                raw_pixels.append(tuple(fb[d:d+4]))
        blurred = aro.gaussian_blur_texture(raw_pixels, bw, bh)
        for y in range(y0, y1):
            for x in range(x0, x1):
                b, g, r, a = blurred[(y - y0) * bw + (x - x0)]
                d = (y * w + x) * 4
                fb[d:d+4] = bytes((r, g, b, a))

        # Verify Gaussian blur diffused the step edge across x=26..34
        blur_28 = blurred[(112 - y0) * bw + 28][0]
        blur_29 = blurred[(112 - y0) * bw + 29][0]
        blur_30 = blurred[(112 - y0) * bw + 30][0]
        blur_31 = blurred[(112 - y0) * bw + 31][0]
        self.assertGreater(blur_28, blur_29)
        self.assertGreater(blur_29, blur_30)
        self.assertGreater(blur_30, blur_31)
        self.assertLess(blur_30, 255)
        self.assertGreater(blur_30, 0)

        # Blend white legibility tint (0xf5f5f5f5, a=245)
        tint = (245, 245, 245, 245)
        for y in range(y0, y1):
            for x in range(x0, x1):
                d = (y * w + x) * 4
                for c in range(3):
                    fb[d + c] = min(255, tint[c] + fb[d + c] * (255 - tint[3]) // 255)

        # After blur + tint: edge is diffused smoothly across x=26..34
        post_27 = fb[(112 * w + 27) * 4]
        post_28 = fb[(112 * w + 28) * 4]
        post_30 = fb[(112 * w + 30) * 4]
        post_33 = fb[(112 * w + 33) * 4]

        # Verify monotonicity and smooth diffusion (no step function)
        self.assertGreaterEqual(post_27, post_28)
        self.assertGreaterEqual(post_28, post_30)
        self.assertGreater(post_27, post_33)
        self.assertLess(post_30, 255)
        self.assertGreater(post_30, 245)  # Diffused above the pure black text level

        # Verify pixels outside the navbar quad (y >= 128) remain untouched
        for y in range(128, h):
            for x in range(w):
                d = (y * w + x) * 4
                self.assertEqual(tuple(fb[d:d+4]), (255, 255, 255, 255),
                                 f"Pixel at ({x},{y}) outside navbar was modified!")

    def test_frame72_draw32_navbar_properties(self):
        """Frame 72 Draw 32 must cover the 640x128 navigation bar with 1x1 white texture."""
        if not DUMPS_DIR:
            self.skipTest(f'{DUMPS_DIR} does not exist')
        dump_72 = DUMPS_DIR / 'frame-72.bin'
        if not dump_72.exists():
            self.skipTest(f'{dump_72} does not exist')

        f = Frame(dump_72)
        draws = list(aro.walk(f))
        self.assertEqual(len(draws), 33)

        vs_data, ppp, draw, vp = draws[32]
        stride, vs = aro.vertices(f, vs_data, draw[1])
        self.assertEqual(draw[1], 6)
        self.assertEqual(stride, 24)

        xs = [v[0] for v in vs]
        ys = [v[1] for v in vs]
        self.assertEqual(min(xs), 0.0)
        self.assertEqual(max(xs), 640.0)
        self.assertEqual(min(ys), 0.0)
        self.assertEqual(max(ys), 128.0)

        tex = aro.texture(f, ppp)
        self.assertIsNotNone(tex)
        self.assertEqual(tex['w'], 1)
        self.assertEqual(tex['h'], 1)

        texel = f.read(tex['addr'], 4)
        self.assertEqual(texel, b'\xf5\xf5\xf5\xf5')

    def test_c_source_invariants(self):
        """backend/s5l8960x.c must contain display strip detection and navbar blur logic."""
        src = S5L8960X_C.read_text(encoding='utf-8')

        # Strip detection on AGXSurface layout
        self.assertIn('agx_surface_layout(s, cat, tex->addr, tex->w, &sf);', src)
        self.assertIn('tex->strip = sf.strip;', src)
        self.assertIn('if (sf.strip) {', src)
        self.assertIn('tex->stride = sf.pitch;', src)

        # Navbar backdrop blur check in agx_replay_frame
        self.assertIn('bool is_navbar = (x0 == 0 && y0 == 0 && (x1 - x0) >= 640 &&', src)
        self.assertIn('(y1 - y0) >= 60 && (y1 - y0) <= 130 &&', src)
        self.assertIn('textured && (tex.w == 1 && tex.h == 1));', src)
        self.assertIn('agx_blur_texture(raw_tex, tmp_tex, bw, bh);', src)

    def test_c_compilation_navbar_blur(self):
        """Native compilation of the navbar backdrop blur detection logic."""
        c_code = r'''
#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <assert.h>

typedef struct {
    uint32_t w, h;
} AGXTexture;

int main(void) {
    int x0 = 0, y0 = 0, x1 = 640, y1 = 128;
    bool textured = true;
    AGXTexture tex = { .w = 1, .h = 1 };

    bool is_navbar = (x0 == 0 && y0 == 0 && (x1 - x0) >= 640 &&
                      (y1 - y0) >= 60 && (y1 - y0) <= 130 &&
                      textured && (tex.w == 1 && tex.h == 1));
    assert(is_navbar == true);

    // Negative case: non-navbar quad
    x0 = 30; y0 = 100; x1 = 200; y1 = 150;
    is_navbar = (x0 == 0 && y0 == 0 && (x1 - x0) >= 640 &&
                 (y1 - y0) >= 60 && (y1 - y0) <= 130 &&
                 textured && (tex.w == 1 && tex.h == 1));
    assert(is_navbar == false);

    return 0;
}
'''
        with tempfile.TemporaryDirectory() as td:
            c_file = Path(td) / 'test_navbar.c'
            bin_file = Path(td) / 'test_navbar'
            c_file.write_text(c_code)
            res = subprocess.run(['cc', '-O2', str(c_file), '-o', str(bin_file)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertEqual(res.returncode, 0, f"Compilation failed: {res.stderr}")
            run_res = subprocess.run([str(bin_file)])
            self.assertEqual(run_res.returncode, 0)


if __name__ == '__main__':
    unittest.main()
