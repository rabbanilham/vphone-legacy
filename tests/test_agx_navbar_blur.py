"""Unit tests for AGX navigation bar backdrop blur modeling (Task F3).

Verifies:
- Display surface textures (e.g. 0x400000000) with 16-row strip layout and +0xa000 hole
  are recognized with strip=True and stride=2560 instead of failing on linear row stride.
- Frame 72 Draw 32 is verified as the 640x128 navigation bar backdrop with 1x1 white texture.
- C hardware model (backend/s5l8960x.c) implements display surface strip detection.
"""

import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'backend'
DUMPS_PATH = Path(os.environ.get('VPHONE_AGX_DUMPS', str(BACKEND / 'evidence/keybag-contract-20261015/probe-164-gfxdump/dumps'))).expanduser()
DUMPS_DIR = DUMPS_PATH if DUMPS_PATH.is_dir() else None
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
        """backend/s5l8960x.c decodes display-surface strips and has no navbar heuristic."""
        src = S5L8960X_C.read_text(encoding='utf-8')

        # Strip detection on AGXSurface layout
        self.assertIn('agx_surface_layout(s, cat, tex->addr, tex->w, &sf);', src)
        self.assertIn('tex->strip = sf.strip;', src)
        self.assertIn('if (sf.strip) {', src)
        self.assertIn('tex->stride = sf.pitch;', src)

        # No screen-geometry guess: the backdrop blur must come from the
        # guest's own offscreen blur passes, not from recognising a quad.
        self.assertNotIn('is_navbar', src)


if __name__ == '__main__':
    unittest.main()
