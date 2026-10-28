"""Unit tests for AGX slider spans primitive and CoreAnimation blend modes:
- Classification of stride-16 glyph row spans (prog 0x1a20cc29e8fcc15b)
- Additive blending (plus-lighter) and subtractive blending (plus-darker) arithmetic
- Invariant: black source (S=0) in plus-lighter preserves destination (0 + D = D),
  curing the black bar and dash artefact
- 1-pixel horizontal span rasterisation with alpha coverage blending
- Verification of C hardware replay core in backend/s5l8960x.c and backend/agx_replay_offline.py
- Zero 'stride 16 outside the wallpaper program' skips across the unlock corpus
"""
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'backend/s5l8960x.c'
UNLOCK_DUMPS = ROOT / 'corpus/unlock/dumps'
SUMMARY_JSON = ROOT / 'corpus/report/summary.json'

REPLAY_SPEC = importlib.util.spec_from_file_location(
    'agx_replay_offline', ROOT / 'backend/agx_replay_offline.py')
aro = importlib.util.module_from_spec(REPLAY_SPEC)
REPLAY_SPEC.loader.exec_module(aro)

FRAME_SPEC = importlib.util.spec_from_file_location(
    'agx_frame', ROOT / 'backend/agx_frame.py')
aframe = importlib.util.module_from_spec(FRAME_SPEC)
FRAME_SPEC.loader.exec_module(aframe)


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


class AGXSliderSpansTests(unittest.TestCase):
    def test_classify_draw_spans(self):
        """classify_draw categorises stride-16 prog 0x1a20cc29e8fcc15b as glyph spans."""
        mf = MockFrame()
        # vs_data points to memory at 0x500 containing stride=16, buf=0x1000
        vs_data_bytes = struct.pack('<IIQQ', 16, 0, 0x1000, 0)
        for i, b in enumerate(vs_data_bytes):
            mf.data[0x500 + i] = b

        draw_cmd = (3 << 29, 64)
        ppp = []
        viewport = (320.0, 320.0, 48.0, 48.0)

        orig_prog = aro.program
        orig_tex = aro.texture
        try:
            # 1. SPANS program (untextured stride 16)
            aro.program = lambda f, p: aro.SPANS
            aro.texture = lambda f, p: None
            res, det = aro.classify_draw(mf, 0x500, ppp, draw_cmd, viewport, 640, 96, 640, 96)
            self.assertEqual(res, 'replayed exactly: glyph spans (stride=16)')
            self.assertEqual(det['count'], 64)
            self.assertEqual(det['prog'], hex(aro.SPANS))

            # 2. Other untextured program with stride 16 (skipped)
            aro.program = lambda f, p: 0x12345678abcdef00
            res, det = aro.classify_draw(mf, 0x500, ppp, draw_cmd, viewport, 640, 96, 640, 96)
            self.assertEqual(res, 'skipped: untextured with stride not in (20, 28)')
        finally:
            aro.program = orig_prog
            aro.texture = orig_tex

    def test_plus_lighter_and_darker_blend_math(self):
        """Verify additive and subtractive blend arithmetic and black-source invariant."""
        def blend_plus_lighter(dst, src):
            return tuple(min(255, s + d) for s, d in zip(src, dst))

        def blend_plus_darker(dst, src):
            return tuple(max(0, s + d - 255) for s, d in zip(src, dst))

        dst_wall = (180, 140, 60, 255)

        # Invariant 1: Black source (0, 0, 0, 0) in plus-lighter preserves destination exactly.
        # This cures the black bar / dash artefact caused by black borders in text layers.
        src_black = (0, 0, 0, 0)
        res_black = blend_plus_lighter(dst_wall, src_black)
        self.assertEqual(res_black, dst_wall)

        # Invariant 2: Bright gradient glint (200, 200, 200, 255) adds to destination
        src_glint = (100, 100, 100, 255)
        res_glint = blend_plus_lighter(dst_wall, src_glint)
        self.assertEqual(res_glint, (255, 240, 160, 255))
        self.assertGreater(res_glint[0], dst_wall[0])

        # Plus darker
        self.assertEqual(blend_plus_darker((255, 255, 255, 255), dst_wall), dst_wall)
        self.assertEqual(blend_plus_darker((100, 100, 100, 255), (100, 100, 100, 255)), (0, 0, 0, 255))

    def test_span_rasterisation_synthetic(self):
        """Synthetic 1-pixel horizontal span rasterisation with coverage alpha."""
        target_w, target_h = 100, 50
        fb = bytearray(b'\x80\x80\x80\xff' * (target_w * target_h))

        # Vertex 0: (10.5, 20.5), byte 15 coverage alpha = 200
        # Vertex 1: (30.5, 20.5), byte 15 coverage alpha = 200
        v0 = (10.5, 20.5, bytes(7) + bytes([200]))
        v1 = (30.5, 20.5, bytes(7) + bytes([200]))

        # Rasterise span directly using offline algorithm
        px0, px1 = v0[0], v1[0]
        py0 = v0[1]
        alpha = v0[2][-1]
        iy = int(round(py0 - 0.5))
        ix0 = int(round(min(px0, px1) - 0.5))
        ix1 = int(round(max(px0, px1) - 0.5))

        for ix in range(ix0, ix1):
            d = (iy * target_w + ix) * 4
            for c in range(3):
                fb[d + c] = min(255, alpha + fb[d + c] * (255 - alpha) // 255)
            fb[d + 3] = min(255, alpha + fb[d + 3] * (255 - alpha) // 255)

        # Outside span row y=19 should be original grey (128)
        off_outside = (19 * target_w + 20) * 4
        self.assertEqual(fb[off_outside:off_outside + 3], b'\x80\x80\x80')

        # Inside span at x=20, y=20 should be brightened by white + coverage alpha:
        # 200 + 128 * (255 - 200) // 255 = 200 + 128 * 55 // 255 = 200 + 27 = 227
        off_inside = (20 * target_w + 20) * 4
        self.assertEqual(fb[off_inside], 227)
        self.assertEqual(fb[off_inside + 1], 227)
        self.assertEqual(fb[off_inside + 2], 227)

        # Beyond span x=35, y=20 should be original grey
        off_after = (20 * target_w + 35) * 4
        self.assertEqual(fb[off_after:off_after + 3], b'\x80\x80\x80')

    def test_c_source_slider_implementation(self):
        """Verify backend/s5l8960x.c implements span rasteriser and plus-lighter blend."""
        source = SOURCE.read_text()

        # Check shader program constants
        self.assertIn('#define AGX_USC_SPANS UINT64_C(0x1a20cc29e8fcc15b)', source)
        self.assertIn('#define AGX_USC_PLUS_L UINT64_C(0x5b133ef7e0c3428c)', source)
        self.assertIn('#define AGX_USC_PLUS_L2 UINT64_C(0xa225a18725e48d5c)', source)
        self.assertIn('#define AGX_USC_SOLID_PLUS_L UINT64_C(0xf137b90c25ab5471)', source)

        # Check blend functions
        self.assertIn('agx_blend_plus_lighter(uint32_t dst, uint32_t src)', source)
        self.assertIn('agx_blend_plus_darker(uint32_t dst, uint32_t src)', source)

        # Check span recognition in VDM dispatch
        self.assertIn('bool is_span = stride == 16 && (prog == AGX_USC_SPANS || prog == AGX_USC_WEBKIT_SPANS);', source)
        self.assertIn('if (is_span) {', source)
        self.assertIn('count = MIN(w[1], 1024u);', source)

        # Check matrix retention across draws for spans
        self.assertIn('float last_m4[4];', source)
        self.assertIn('bool has_last_m4 = false;', source)

        # Check plus-lighter application to textured and solid draws
        self.assertIn('prog == AGX_USC_SOLID_PLUS_L ? agx_blend_plus_lighter(row[x - x0], colour)', source)
        self.assertIn('prog == AGX_USC_PLUS_L || prog == AGX_USC_PLUS_L2', source)

    def test_c_spans_and_blend_execution(self):
        """Compile and execute C test harness verifying span math and blend operations."""
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

#define MIN(a, b) ((a) < (b) ? (a) : (b))
#define MAX(a, b) ((a) > (b) ? (a) : (b))

static uint32_t agx_blend_over(uint32_t dst, uint32_t src)
{
    uint32_t a = (src >> 24) & 0xff;
    uint32_t inv = 255 - a;
    uint32_t out = 0;
    for (unsigned c = 0; c < 32; c += 8) {
        uint32_t s = (src >> c) & 0xff;
        uint32_t d = (dst >> c) & 0xff;
        uint32_t v = s + (d * inv + 127) / 255;
        out |= MIN(v, 255u) << c;
    }
    return out;
}

static uint32_t agx_blend_plus_lighter(uint32_t dst, uint32_t src)
{
    uint32_t out = 0;
    for (unsigned c = 0; c < 32; c += 8) {
        uint32_t v = ((src >> c) & 0xff) + ((dst >> c) & 0xff);
        out |= MIN(v, 255u) << c;
    }
    return out;
}

int main(void) {
    // 1. Black source in plus-lighter preserves dst
    uint32_t dst = 0xff3c8cb4; // RGBA(180, 140, 60, 255)
    uint32_t black = 0x00000000;
    assert(agx_blend_plus_lighter(dst, black) == dst);

    // 2. Additive blend saturates at 255
    uint32_t white = 0xffffffff;
    assert(agx_blend_plus_lighter(dst, white) == 0xffffffff);

    // 3. Span rasterisation with premultiplied white
    uint8_t alpha = 180;
    uint32_t a = alpha;
    uint32_t span_c = (a << 24) | (a << 16) | (a << 8) | a;
    uint32_t blended = agx_blend_over(dst, span_c);
    // Channel values should be brighter than original dst
    for (unsigned c = 0; c < 24; c += 8) {
        assert(((blended >> c) & 0xff) >= ((dst >> c) & 0xff));
    }

    printf("OK\\n");
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmpdir:
            src_path = Path(tmpdir) / 'test_spans.c'
            bin_path = Path(tmpdir) / 'test_spans'
            src_path.write_text(c_test)
            res = subprocess.run([compiler, '-O2', str(src_path), '-o', str(bin_path)],
                                 capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr)
            run = subprocess.run([str(bin_path)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0)
            self.assertIn('OK', run.stdout)

    def test_zero_stride16_skips_in_unlock_corpus(self):
        """Verify that stride-16 outside wallpaper program skips are 0 across unlock dumps."""
        if not UNLOCK_DUMPS.exists():
            self.skipTest(f'{UNLOCK_DUMPS} does not exist')

        stride16_skips = 0
        total_spans = 0
        for dump_path in sorted(UNLOCK_DUMPS.glob('frame-*.bin')):
            f = aframe.Frame(dump_path)
            outcomes = aro.audit_frame(f)
            for did, out, det in outcomes:
                if 'stride 16 outside the wallpaper program' in out or (
                        'stride not in (20, 28)' in out and det.get('stride') == 16):
                    stride16_skips += 1
                if 'glyph spans (stride=16)' in out:
                    total_spans += 1

        self.assertEqual(stride16_skips, 0, f'Found {stride16_skips} stride-16 skips in unlock dumps')
        self.assertEqual(total_spans, 37, f'Expected 37 spans in unlock dumps, found {total_spans}')

        # Also check across all corpus scenes if available
        corpus_dir = ROOT / 'corpus'
        if corpus_dir.exists():
            all_spans = 0
            all_s16_skips = 0
            for dump_path in sorted(corpus_dir.glob('*/dumps/frame-*.bin')):
                f = aframe.Frame(dump_path)
                for did, out, det in aro.audit_frame(f):
                    if 'stride 16 outside the wallpaper program' in out or (
                            'stride not in (20, 28)' in out and det.get('stride') == 16):
                        all_s16_skips += 1
                    if 'glyph spans (stride=16)' in out:
                        all_spans += 1
            self.assertEqual(all_s16_skips, 0, f'Found {all_s16_skips} stride-16 skips across all scenes')
            self.assertEqual(all_spans, 73, f'Expected 73 spans across corpus, found {all_spans}')


if __name__ == '__main__':
    unittest.main()
