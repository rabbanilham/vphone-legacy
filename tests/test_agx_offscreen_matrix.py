"""Offscreen CA passes name the vertex matrix once and later draws reuse it.

Small offscreen passes (the Notification Center segmented-control mask, the
grabber glyph) translate pixel-space geometry to the group origin with a
vertex matrix that only the pass's first draw carries; later draws leave the
vertex uniforms loaded. Dropping the matrix drew those draws at screen
coordinates, outside or at the wrong place in the target.

Fixtures are trimmed live AGXF dumps (only the touched pages), zlib-compressed:
  frame-26: grabber, 640x320 target, 4 draws (triangles, line, triangles, line)
  frame-27: segmented control Missed + All segments, 640x320 target
Both the C core (host harness; skipped when it cannot be built) and the Python
mirror are exercised.
"""
import contextlib
import io
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures/agx_offscreen_matrix'
sys.path.insert(0, str(ROOT / 'backend'))
sys.path.insert(0, str(ROOT / 'scripts'))
import agx_replay_offline as replay


def fixture(tmp, name):
    path = Path(tmp) / f'{name}.bin'
    path.write_bytes(zlib.decompress((FIXTURES / f'{name}.agxf.z').read_bytes()))
    return path


def python_render(path):
    out = []
    with patch.object(sys, 'argv', ['replay', str(path), 'unused.png']), \
         patch.object(replay, 'png', side_effect=lambda p, w, h, data: out.append((w, h, bytes(data)))), \
         contextlib.redirect_stdout(io.StringIO()):
        replay.main()
    return out[0]


def c_render(tmp, path):
    import agx_corpus
    try:
        binary = agx_corpus.build_harness(Path(tmp) / 'harness')
    except (subprocess.CalledProcessError, OSError, ValueError) as error:
        raise unittest.SkipTest(f'host harness unavailable: {error}')
    rgba = Path(tmp) / (path.stem + '.rgba')
    subprocess.run([str(binary), str(path), str(rgba)], check=True, capture_output=True)
    data = rgba.read_bytes()
    w, h = struct.unpack_from('<II', data, 4)
    return w, h, data[12:]


def alpha_bbox(render):
    w, h, rgba = render
    xs, ys = [], []
    for y in range(h):
        row = rgba[y * w * 4:(y + 1) * w * 4]
        for x in range(w):
            if row[x * 4 + 3]:
                xs.append(x); ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def rows_with_alpha(render, x0, x1):
    w, h, rgba = render
    return [y for y in range(h)
            if any(rgba[(y * w + x) * 4 + 3] for x in range(x0, x1))]


class OffscreenMatrixTests(unittest.TestCase):
    def check_grabber(self, render):
        # Both triangle halves land in the target: the chevron spans x 0..76
        # (the Python mirror rounds an edge pixel less), not just the first
        # draw's left half (x 0..49).
        x0, y0, x1, y1 = alpha_bbox(render)
        self.assertEqual((x0, y0), (0, 0))
        self.assertTrue(74 <= x1 <= 77 and 22 <= y1 <= 25, (x1, y1))

    def check_segments(self, render):
        # The All segment's 3-slice borders sit at the mask's top and bottom
        # rows, not at the screen rows 48/49 and 104/105 of the display pass.
        self.assertEqual(rows_with_alpha(render, 230, 410), [0, 1, 56, 57])
        self.assertEqual(alpha_bbox(render)[3], 57)

    def test_c_core_grabber(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.check_grabber(c_render(tmp, fixture(tmp, 'frame-26')))

    def test_c_core_segments(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.check_segments(c_render(tmp, fixture(tmp, 'frame-27')))

    def test_python_mirror_grabber(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.check_grabber(python_render(fixture(tmp, 'frame-26')))

    def test_python_mirror_segments(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.check_segments(python_render(fixture(tmp, 'frame-27')))

    def test_display_pass_gate_in_sources(self):
        """Full-screen display passes keep drawing in pixels (no matrix reuse)."""
        c_source = (ROOT / 'backend/s5l8960x.c').read_text()
        self.assertIn('bool offscreen_pass = !(width == 640 && height == 1136);', c_source)
        py_source = (ROOT / 'backend/agx_replay_offline.py').read_text()
        self.assertIn('(pw, ph) != (640, 1136)', py_source)


if __name__ == '__main__':
    unittest.main()
