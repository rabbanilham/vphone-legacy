"""Calendar day grid: a tiled hour-line strip must not be read as texel UVs.

The grid is one quad textured with a 4x2048 strip (a 214-grey line every 100
rows on white). Its UVs are normalized on V (0.416..0.784) and tile on U
(0..134.5, 4 texels x 134.5 = the 538 px quad). The replay treated any UV above
1 as texel units, so V collapsed onto row 0 and the whole grid came out flat
#d6d6d6 instead of white with thin hour lines.

The fixture is a trimmed live AGXF dump (touched pages only, zlib). The C core
(host harness; skipped when it cannot be built) and the Python mirror are both
checked, plus the texel-unit cases the heuristic must keep.
"""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_agx_offscreen_matrix import c_render, python_render, FIXTURES  # noqa: E402
import agx_replay_offline as replay  # noqa: E402
import zlib  # noqa: E402

GRID_FIXTURE = ROOT / 'tests/fixtures/agx_tiled_pattern/calendar-grid.agxf.z'
HOUR_ROWS = [342, 442, 542, 642, 742, 842, 942, 1042]


def grid_column(render, x=400):
    w, h, rgba = render
    return {y: rgba[(y * w + x) * 4] for y in range(295, 1046)}


class TiledPatternTests(unittest.TestCase):
    def fixture(self, tmp):
        path = Path(tmp) / 'calendar-grid.bin'
        path.write_bytes(zlib.decompress(GRID_FIXTURE.read_bytes()))
        return path

    def check_grid(self, render):
        column = grid_column(render)
        self.assertEqual([y for y, v in column.items() if v != 255], HOUR_ROWS)
        self.assertTrue(all(column[y] == 214 for y in HOUR_ROWS))

    def test_c_core_calendar_grid(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.check_grid(c_render(tmp, self.fixture(tmp)))

    def test_python_mirror_calendar_grid(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.check_grid(python_render(self.fixture(tmp)))

    def test_is_tiled_pattern_heuristic(self):
        tex = {'w': 4, 'h': 2048}
        # The calendar quad: u tiles, v normalized, normalized extent == quad.
        self.assertTrue(replay.is_tiled_pattern(tex, 0, 134.5, 0.416, 0.784, 538, 754))
        # Vertical tiling is the mirror case.
        self.assertTrue(replay.is_tiled_pattern({'w': 2048, 'h': 4}, 0.416, 0.784, 0, 134.5, 754, 538))
        # Texel-unit rectangle textures (NC mask: u 0..624, v 0..58) stay texel UVs.
        self.assertFalse(replay.is_tiled_pattern({'w': 672, 'h': 416}, 0, 624, 0, 58, 624, 58))
        # Fully normalized UVs never need the override.
        self.assertFalse(replay.is_tiled_pattern(tex, 0, 1, 0, 1, 538, 754))
        # One axis above 1 but the normalized extent does not match the quad.
        self.assertFalse(replay.is_tiled_pattern(tex, 0, 3, 0, 0.5, 538, 754))

    def test_c_source_has_matching_heuristic(self):
        c_source = (ROOT / 'backend/s5l8960x.c').read_text()
        self.assertIn('bool tiled = MAX(u1, v1) > 1.0001f &&', c_source)
        self.assertIn('nc_normalized || tiled ||', c_source)


if __name__ == '__main__':
    unittest.main()
