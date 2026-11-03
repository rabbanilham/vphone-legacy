"""Pure-Python tests for scripts/glitch_audit.py heuristics, on synthetic images."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import glitch_audit as g  # noqa: E402


def wallpaper(w=640, h=1136):
    """Horizontal-ish gradient so wallpaper is never flat."""
    rgb = bytearray()
    for y in range(h):
        for x in range(w):
            rgb += bytes((40 + x * 120 // w, 90 + y * 100 // h, 200 - x * 60 // w))
    return g.Img(w, h, bytes(rgb))


def draw_tile(img, x0, y0, size, radius, color):
    rgb = bytearray(img.rgb)
    for y in range(y0, y0 + size):
        for x in range(x0, x0 + size):
            dx = max(x0 + radius - x, x - (x0 + size - 1 - radius), 0)
            dy = max(y0 + radius - y, y - (y0 + size - 1 - radius), 0)
            if dx * dx + dy * dy <= radius * radius:
                i = (y * img.w + x) * 3
                rgb[i:i + 3] = bytes(color)
    return g.Img(img.w, img.h, bytes(rgb))


class Heuristics(unittest.TestCase):
    def test_new_dark_rect_flagged_only_when_new(self):
        a = wallpaper()
        b = g.Img(a.w, a.h, a.rgb).fill(160, 320, 480, 640, (20, 20, 20))
        found = g.find_new_flat_rects(b, a)
        self.assertEqual(len(found), 1)
        x0, y0, x1, y1 = found[0]['bbox']
        self.assertTrue(x0 <= 176 and y0 <= 336 and x1 >= 464 and y1 >= 624)
        self.assertEqual(g.find_new_flat_rects(b, b), [])      # already present
        self.assertEqual(g.find_new_flat_rects(b, None), [])   # no previous frame

    def test_grey_rect_and_small_rect(self):
        a = wallpaper()
        grey = g.Img(a.w, a.h, a.rgb).fill(100, 100, 300, 300, (128, 128, 128))
        self.assertEqual(len(g.find_new_flat_rects(grey, a)), 1)
        tiny = g.Img(a.w, a.h, a.rgb).fill(100, 100, 140, 140, (20, 20, 20))
        self.assertEqual(g.find_new_flat_rects(tiny, a), [])

    def test_corners(self):
        cx, cy = 240, 306
        x0, y0 = cx - 60, cy - 60
        rounded = draw_tile(wallpaper(), x0, y0, 120, 27, (240, 200, 30))
        square = draw_tile(wallpaper(), x0, y0, 120, 0, (240, 200, 30))
        box = g.find_tile_bbox(rounded, cx, cy)
        self.assertEqual(box, (x0, y0, x0 + 120, y0 + 120))
        self.assertEqual(g.hard_corners(rounded, box), [])
        self.assertEqual(len(g.hard_corners(square, g.find_tile_bbox(square, cx, cy))), 4)
        self.assertEqual(g.find_hard_cornered_tiles(rounded, [(cx, cy)]), [])
        self.assertEqual(len(g.find_hard_cornered_tiles(square, [(cx, cy)])), 1)
        self.assertIsNone(g.find_tile_bbox(wallpaper(), cx, cy))

    def test_black_and_unchanged(self):
        black = g.Img.blank(640, 1136)
        self.assertTrue(g.is_black(black))
        self.assertFalse(g.is_black(wallpaper()))
        w = wallpaper()
        flags = g.analyze_sequence([w, w, w, w, w, w], stuck=3)
        self.assertEqual([bool(f) for f in flags], [False, False, False, False, True, True])
        self.assertEqual(flags[4][0]['kind'], 'unchanged')
        self.assertEqual(g.analyze_sequence([black], 3)[0][0]['kind'], 'black')


class Io(unittest.TestCase):
    def test_png_roundtrip_and_ppm(self):
        img = wallpaper(64, 48)
        with tempfile.TemporaryDirectory() as d:
            ppm, png = Path(d) / 'a.ppm', Path(d) / 'a.png'
            ppm.write_bytes(b'P6\n64 48\n255\n' + img.rgb)
            g.write_png(png, 64, 48, img.rgb)
            self.assertEqual(g.read_png(png).rgb, img.rgb)
            self.assertEqual(g.load_image(ppm).rgb, img.rgb)
            self.assertEqual(g.load_image(png).rgb, img.rgb)

    def test_mtt_events_match_player(self):
        ev = g.mtt_events('begin', [(320, 568)])
        self.assertEqual(ev[0]['data'], dict(type='data', slot=0, **{'tracking-id': 0}, axis='x', value=16383 - 0 if False else 320 * 32767 // 640))
        self.assertEqual(ev[2]['data']['type'], 'begin')
        self.assertEqual(len(ev), 3)

    def test_scenarios_wellformed(self):
        for name, sc in g.SCENARIOS.items():
            self.assertIn(sc['start'], ('lock', 'home'))
            self.assertTrue(any(s[0] == 'shot' for s in sc['steps']), name)
            for s in sc['steps']:
                self.assertIn(s[0], ('tap', 'swipe', 'key', 'wait', 'shot'))


class Report(unittest.TestCase):
    def test_analyze_writes_reports(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / 'sc').mkdir()
            a = wallpaper(640, 1136)
            g.write_png(d / 'sc/01-a.png', 640, 1136, a.rgb)
            b = g.Img(640, 1136, a.rgb).fill(160, 320, 480, 640, (20, 20, 20))
            g.write_png(d / 'sc/02-b.png', 640, 1136, b.rgb)
            class A: pass
            args = A(); args.dir = str(d); args.stuck = 3
            g.cmd_analyze(args)
            rep = json.loads((d / 'report.json').read_text())
            self.assertEqual(rep['flagged_frames'], 1)
            self.assertIn('02-b.png', (d / 'report.html').read_text())


if __name__ == '__main__':
    unittest.main()
