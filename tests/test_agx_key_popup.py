"""Key-press balloon masks are two-byte pitch-linear (L8A8), not Morton A8.

The iOS 7 keyboard key popup is a CoreGraphics mask: format 3, layout code 4
(TSW word 0 = 0x38000024), 204x228, pitch word 0xcf = 208 texels of two bytes
(byte 1 = alpha). The decoder read it as the 1-byte Morton A8 used by the
256x256 squircle corner masks (layout code 1), so the balloon drew as hatched noise.

Fixture: frame-16 of a live Safari keyboard capture, trimmed to the pages the
replay reads (zlib). Both the C core (host harness; skipped when it cannot be
built) and the Python mirror are exercised.
"""
from pathlib import Path
import sys
import tempfile
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
sys.path.insert(0, str(ROOT / 'backend'))
import agx_replay_offline as replay
from test_agx_offscreen_matrix import c_render, python_render

FIXTURE = ROOT / 'tests/fixtures/agx_key_popup/frame-16.agxf.z'


def dump(tmp):
    path = Path(tmp) / 'frame-16.bin'
    path.write_bytes(zlib.decompress(FIXTURE.read_bytes()))
    return path


def row_transitions(render, y0, y1):
    """Average number of alpha changes along a row: noise flips every few pixels."""
    w, h, rgba = render
    total = 0
    for y in range(y0, y1):
        row = [rgba[(y * w + x) * 4 + 3] for x in range(w)]
        total += sum(1 for a, b in zip(row, row[1:]) if abs(a - b) > 64)
    return total / (y1 - y0)


class KeyPopupTests(unittest.TestCase):
    def check(self, render):
        w, h, rgba = render
        self.assertEqual((w, h), (224, 256))
        # Balloon interior is opaque; hatched noise left it transparent.
        self.assertEqual(rgba[(100 * w + 100) * 4 + 3], 255)
        self.assertLess(row_transitions(render, 40, 220), 12)

    def test_python_mirror(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.check(python_render(dump(tmp)))

    def test_c_core(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.check(c_render(tmp, dump(tmp)))

    def test_decoder_classifies_layouts(self):
        class F:  # only decode_tsw's page lookups are needed
            pages = {0x10ba4000: b''}
            def read(self, *a): return b'\0' * 4
        la = replay.decode_tsw(F(), [0x38000024, 0xe3032c, 0x900700cf, 0x1000042e])
        self.assertTrue(la['la88'] and not la['twiddled'])
        self.assertEqual((la['w'], la['h'], la['stride']), (204, 228, 416))
        # The 1-byte Morton A8 squircle mask keeps its layout.
        a8 = replay.decode_tsw(F(), [0x38000021, 0xff03fc, 0x900700cf, 0x1000042e])
        self.assertTrue(a8 and a8['twiddled'] and not a8['la88'])


if __name__ == '__main__':
    unittest.main()
