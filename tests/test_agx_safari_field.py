"""Safari address-bar field renders light grey, not dark slate.

The field's rounded fill is drawn twice with one 9-slice texture: first with a
compositing-filter program (tint 0xb8b8b8b8) and then with a src-over program
(tint 0x0d0d0d0d). Replaying the first as src-over painted dark slate (~#3e4350)
under the placeholder. Fixture: frame-28 of a live Safari address-bar focus
capture, trimmed to the pages the Python mirror reads (zlib).
"""
from pathlib import Path
import sys
import tempfile
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
sys.path.insert(0, str(ROOT / 'backend'))
from test_agx_offscreen_matrix import c_render, python_render

FIXTURE = ROOT / 'tests/fixtures/agx_safari_field/frame-28.agxf.z'


def dump(tmp):
    path = Path(tmp) / 'frame-28.bin'
    path.write_bytes(zlib.decompress(FIXTURE.read_bytes()))
    return path


class SafariFieldTests(unittest.TestCase):
    def check(self, render):
        w, h, rgba = render
        # Empty field area right of the placeholder text, inside the rounded rect.
        for x, y in ((440, 60), (30, 90), (300, 52)):
            r, g, b = rgba[(y * w + x) * 4:(y * w + x) * 4 + 3]
            self.assertGreater(min(r, g, b), 0xd0, (x, y, r, g, b))

    def test_c_core(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.check(c_render(tmp, dump(tmp)))

    def test_python_mirror(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.check(python_render(dump(tmp)))


if __name__ == '__main__':
    unittest.main()
