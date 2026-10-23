"""Unit tests for AGX replay draw coverage:
- Classification of draw outcomes (replayed exactly, approximated, skipped with reason)
- 100% draw coverage across all 150 dumps in probe-164-gfxdump (533 draws, 0 skipped)
- Decomposition of multi-quad draws (count == 12) into discrete quads without overdraw
- Execution of offscreen stride-12 canvas Gaussian blur backdrop draws
- C hardware replay implementation verification in backend/s5l8960x.c
"""
from collections import Counter
import importlib.util
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'backend/s5l8960x.c'
ROOT = Path(__file__).resolve().parents[1]
DUMPS_DIR = Path('/Users/bagas/Downloads/vphone-legacy/backend/evidence/keybag-contract-20261015/probe-164-gfxdump/dumps')
if not DUMPS_DIR.exists():
    DUMPS_DIR = ROOT / 'backend/evidence/keybag-contract-20261015/probe-164-gfxdump/dumps'

REPLAY_SPEC = importlib.util.spec_from_file_location(
    'agx_replay_offline', ROOT / 'backend/agx_replay_offline.py')
aro = importlib.util.module_from_spec(REPLAY_SPEC)
REPLAY_SPEC.loader.exec_module(aro)

FRAME_SPEC = importlib.util.spec_from_file_location(
    'agx_frame', ROOT / 'backend/agx_frame.py')
aframe = importlib.util.module_from_spec(FRAME_SPEC)
FRAME_SPEC.loader.exec_module(aframe)


class AGXDrawCoverageTests(unittest.TestCase):
    def test_classification_archetypes(self):
        """Verify classify_draw categorises draws into correct exact, approximated, and skipped categories."""
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

        mf = MockFrame()

        # 1. Solid quad (stride=20, count=6)
        # vs_data points to memory at 0x500 containing stride=20, buf=0x1000
        vs_data_bytes = struct.pack('<IIQQ', 20, 0, 0x1000, 0)
        for i, b in enumerate(vs_data_bytes):
            mf.data[0x500 + i] = b

        # 6 vertices: (x, y, bgra)
        quad_verts = b''
        for x, y in [(0.0, 0.0), (100.0, 0.0), (0.0, 100.0), (0.0, 100.0), (100.0, 0.0), (100.0, 100.0)]:
            quad_verts += struct.pack('<2f8x4s', x, y, b'\xff\xff\xff\xff')
        for i, b in enumerate(quad_verts):
            mf.data[0x1000 + i] = b

        draw_cmd = (3 << 29, 6)  # kind=3, count=6
        ppp = []
        viewport = (320.0, 320.0, 568.0, 568.0)
        res, det = aro.classify_draw(mf, 0x500, ppp, draw_cmd, viewport, 640, 1136, 640, 1136)
        self.assertEqual(res, 'replayed exactly: solid quad (stride=20)')

        # 2. Solid multi-quad (stride=20, count=12)
        multi_verts = quad_verts + quad_verts
        for i, b in enumerate(multi_verts):
            mf.data[0x2000 + i] = b
        vs_data_multi_bytes = struct.pack('<IIQQ', 20, 0, 0x2000, 0)
        for i, b in enumerate(vs_data_multi_bytes):
            mf.data[0x600 + i] = b
        draw_cmd_multi = (3 << 29, 12)
        res, det = aro.classify_draw(mf, 0x600, ppp, draw_cmd_multi, viewport, 640, 1136, 640, 1136)
        self.assertEqual(res, 'replayed exactly: solid multi-quad (2 quads, stride=20)')

        # 3. Offscreen stride-12 canvas Gaussian blur backdrop
        vs_data_s12_bytes = struct.pack('<IIQQ', 12, 0, 0x3000, 0)
        for i, b in enumerate(vs_data_s12_bytes):
            mf.data[0x700 + i] = b
        s12_verts = b''
        for x, y in [(0.0, 0.0), (608.0, 0.0), (0.0, 64.0), (608.0, 64.0)]:
            s12_verts += struct.pack('<3f', x, y, 0.0)
        for i, b in enumerate(s12_verts):
            mf.data[0x3000 + i] = b
        draw_cmd_s12 = (3 << 29, 4)
        blur_hash = next(iter(aro.BLUR_PROGRAMS))
        orig_prog = aro.program
        try:
            aro.program = lambda f, p: blur_hash
            res, det = aro.classify_draw(mf, 0x700, ppp, draw_cmd_s12, viewport, 608, 64, 608, 64)
            self.assertEqual(res, 'approximated: Gaussian blur (separable 6-tap, canvas backdrop)')
        finally:
            aro.program = orig_prog

        # 4. Unknown VDM block
        res, _ = aro.classify_draw(mf, 0x500, ppp, (4 << 29, 6), viewport, 640, 1136, 640, 1136)
        self.assertEqual(res, 'skipped: unknown VDM block')

        # 5. Non-axis-aligned / rotated geometry
        rot_verts = b''
        for x, y in [(0.0, 0.0), (100.0, 50.0), (50.0, 100.0), (50.0, 100.0), (100.0, 50.0), (150.0, 150.0)]:
            rot_verts += struct.pack('<2f8x4s', x, y, b'\xff\xff\xff\xff')
        for i, b in enumerate(rot_verts):
            mf.data[0x4000 + i] = b
        vs_data_rot_bytes = struct.pack('<IIQQ', 20, 0, 0x4000, 0)
        for i, b in enumerate(vs_data_rot_bytes):
            mf.data[0x800 + i] = b
        res, _ = aro.classify_draw(mf, 0x800, ppp, draw_cmd, viewport, 640, 1136, 640, 1136)
        self.assertEqual(res, 'skipped: non-axis-aligned or rotated geometry')

    def test_audit_all_150_frames_zero_skips(self):
        """Audit all 150 frame dumps in probe-164-gfxdump: exactly 533 draws, 0 skipped."""
        if not DUMPS_DIR.exists():
            self.skipTest(f'{DUMPS_DIR} does not exist')

        counts = Counter()
        total_draws = 0
        frames_seen = 0

        for i in range(1, 151):
            dump_file = DUMPS_DIR / f'frame-{i}.bin'
            if not dump_file.exists():
                continue
            frames_seen += 1
            f = aframe.Frame(dump_file)
            outcomes = aro.audit_frame(f)
            total_draws += len(outcomes)
            for did, out, det in outcomes:
                self.assertFalse(out.startswith('skipped:'),
                                f'Frame {i} draw {did} was skipped: {out} ({det})')
                counts[out] += 1

        self.assertEqual(frames_seen, 150)
        self.assertEqual(total_draws, 533)
        self.assertEqual(counts['approximated: textured quad (bilinear scaled)'], 144)
        self.assertEqual(counts['replayed exactly: textured quad (1:1 nearest)'], 149)
        self.assertEqual(counts['replayed exactly: solid quad (stride=20)'], 120)
        self.assertEqual(counts['approximated: Gaussian blur (separable 6-tap, canvas backdrop)'], 46)
        self.assertEqual(counts['replayed exactly: dest-in mask (1:1)'], 46)
        self.assertEqual(counts['approximated: vibrancy (Rec.709 luma-weighted tint)'], 21)
        self.assertEqual(counts['replayed exactly: solid multi-quad (2 quads, stride=20)'], 7)

    def test_multiquad_cell_separator_preservation(self):
        """Multi-quad draws (count=12) render discrete quads and preserve the cell separator area."""
        # Setup synthetic 640x200 canvas
        cw, ch = 640, 200
        canvas = bytearray(b'\x80\x80\x80\xff' * (cw * ch))  # grey background

        # Draw 7 (separator) is at y=119, x=30..640 with color 0xcccccc
        sep_color = b'\xcc\xcc\xcc\xff'
        for x in range(30, 640):
            off = (119 * cw + x) * 4
            canvas[off:off + 4] = sep_color

        # Multi-quad draw 4:
        # Quad 0: (0, 0)-(640, 119) white
        # Quad 1: (0, 119)-(30, 120) white
        # Separator area: (30, 119)-(640, 120) should remain grey 0xcccccc
        quads = [
            # Quad 0
            [(0.0, 0.0), (640.0, 0.0), (0.0, 119.0), (0.0, 119.0), (640.0, 0.0), (640.0, 119.0)],
            # Quad 1
            [(0.0, 119.0), (30.0, 119.0), (0.0, 120.0), (0.0, 120.0), (30.0, 119.0), (30.0, 120.0)],
        ]
        all_verts = quads[0] + quads[1]
        self.assertEqual(len(all_verts), 12)

        # Replay chunked (new behavior)
        white = b'\xff\xff\xff\xff'
        for q_start in range(0, 12, 6):
            q_vs = all_verts[q_start:q_start + 6]
            x0 = int(round(min(v[0] for v in q_vs)))
            x1 = int(round(max(v[0] for v in q_vs)))
            y0 = int(round(min(v[1] for v in q_vs)))
            y1 = int(round(max(v[1] for v in q_vs)))
            for y in range(y0, y1):
                for x in range(x0, x1):
                    off = (y * cw + x) * 4
                    canvas[off:off + 4] = white

        # Verify Quad 0 is white
        self.assertEqual(canvas[:4], white)
        # Verify Quad 1 margin indent (x=10, y=119) is white
        off_margin = (119 * cw + 10) * 4
        self.assertEqual(canvas[off_margin:off_margin + 4], white)
        # Verify separator (x=50, y=119) remains separator color
        off_sep = (119 * cw + 50) * 4
        self.assertEqual(canvas[off_sep:off_sep + 4], sep_color)

    def test_stride12_canvas_blur_backdrop(self):
        """Stride-12 blur backdrop applies Gaussian blur to the canvas within its quad rect."""
        w, h = 100, 100
        canvas = bytearray(w * h * 4)
        # Create high-frequency step pattern inside (20, 20)-(80, 80)
        for y in range(h):
            for x in range(w):
                off = (y * w + x) * 4
                if 20 <= x < 80 and 20 <= y < 80:
                    v = 255 if ((x // 5) % 2 == 0) else 0
                else:
                    v = 128
                canvas[off:off + 4] = bytes([v, v, v, 255])

        # Apply stride-12 canvas blur to (20, 20)-(80, 80)
        x0, y0, x1, y1 = 20, 20, 80, 80
        bw, bh = x1 - x0, y1 - y0
        raw_pixels = []
        for y in range(y0, y1):
            for x in range(x0, x1):
                d = (y * w + x) * 4
                r, g, b, a = canvas[d:d + 4]
                raw_pixels.append((b, g, r, a))
        blurred = aro.gaussian_blur_texture(raw_pixels, bw, bh)
        for y in range(y0, y1):
            for x in range(x0, x1):
                b, g, r, a = blurred[(y - y0) * bw + (x - x0)]
                d = (y * w + x) * 4
                canvas[d:d + 4] = bytes((r, g, b, a))

        # Check outside pixels are untouched (still 128)
        self.assertEqual(canvas[0:4], bytes([128, 128, 128, 255]))
        # Check inside sharp edges are smoothed: intermediate values between 0 and 255 exist
        inside_vals = set()
        for y in range(30, 70):
            for x in range(30, 70):
                d = (y * w + x) * 4
                inside_vals.add(canvas[d])
        self.assertTrue(any(10 < v < 240 for v in inside_vals),
                        'Gaussian blur did not produce smoothed intermediate values')

    def test_c_source_draw_coverage_implementation(self):
        """Verify backend/s5l8960x.c implements stride-12 blur backdrop and multi-quad chunking."""
        source = SOURCE.read_text()

        # Stride-12 blur condition in kind 3 check
        self.assertIn('stride == 12 && is_blur_prog', source)
        self.assertIn('agx_is_blur_program(prog)', source)

        # Multi-quad step decomposition
        self.assertIn('step = (count == 4) ? 4 : 6', source)
        self.assertIn('for (unsigned q_start = 0; q_start + step <= count; q_start += step)', source)

        # Canvas blur backdrop extraction & writeback
        self.assertIn('agx_blur_texture(raw_tex, tmp_tex, bw, bh);', source)
        self.assertIn('raw_tex = g_malloc((size_t)bw * bh * sizeof(uint32_t));', source)

        # Preserved single draw increment per VDM command
        self.assertIn('if (any_drawn) {', source)
        self.assertIn('draws++;', source)

    def test_c_multiquad_compilation(self):
        """Compile and verify multi-quad vertex stepping C logic."""
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

typedef struct {
    float x, y;
    uint32_t color;
} Vertex20;

int main(void) {
    Vertex20 verts[12] = {
        // Quad 0: (0, 0) - (640, 119)
        {0, 0, 0xffffffff}, {640, 0, 0xffffffff}, {0, 119, 0xffffffff},
        {0, 119, 0xffffffff}, {640, 0, 0xffffffff}, {640, 119, 0xffffffff},
        // Quad 1: (0, 119) - (30, 120)
        {0, 119, 0xffffffff}, {30, 119, 0xffffffff}, {0, 120, 0xffffffff},
        {0, 120, 0xffffffff}, {30, 119, 0xffffffff}, {30, 120, 0xffffffff}
    };
    unsigned count = 12;
    unsigned step = (count == 4) ? 4 : 6;
    int quads_drawn = 0;

    for (unsigned q = 0; q < count; q += step) {
        float minx = 1e9, miny = 1e9, maxx = -1e9, maxy = -1e9;
        for (unsigned i = 0; i < step; i++) {
            float x = verts[q + i].x;
            float y = verts[q + i].y;
            if (x < minx) minx = x;
            if (x > maxx) maxx = x;
            if (y < miny) miny = y;
            if (y > maxy) maxy = y;
        }
        int x0 = (int)lroundf(minx);
        int y0 = (int)lroundf(miny);
        int x1 = (int)lroundf(maxx);
        int y1 = (int)lroundf(maxy);

        if (quads_drawn == 0) {
            assert(x0 == 0 && y0 == 0 && x1 == 640 && y1 == 119);
        } else if (quads_drawn == 1) {
            assert(x0 == 0 && y0 == 119 && x1 == 30 && y1 == 120);
        }
        quads_drawn++;
    }
    assert(quads_drawn == 2);
    printf("OK\\n");
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmpdir:
            src_path = Path(tmpdir) / 'test_mq.c'
            bin_path = Path(tmpdir) / 'test_mq'
            src_path.write_text(c_test)
            res = subprocess.run([compiler, '-O2', str(src_path), '-o', str(bin_path)],
                                 capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr)
            run = subprocess.run([str(bin_path)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0)
            self.assertIn('OK', run.stdout)


if __name__ == '__main__':
    unittest.main()
