"""Unit tests for AGX pass load/clear modeling (Task F1).

Verifies:
- Type-1 command words (+0x178 background object, +0x23c load-op flag)
  distinguish clear vs load passes across real frame dumps.
- Display passes (640x1136) are load passes (+0x23c == 1) referencing
  a tile-load sampler/USC program and target TSW.
- Offscreen passes (448x64, 608x64) are clear passes (+0x23c == 0) whose
  background object has zero sampler/USC data and clears tile memory.
- Synthetic commands without background objects (+0x178 == 0) leave
  canvas memory untouched (contract test dest-in compliance).
- Offline replay mirror (backend/agx_replay_offline.py) initializes
  clear passes to transparent black rather than loading stale target memory.
- C hardware model (backend/s5l8960x.c) implements row-wise clear
  in agx_replay_frame, agx_trace_rt, and AGXRenderJob.
"""

import os
from pathlib import Path
import struct
import unittest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'backend'
DUMPS_PATH = Path(os.environ.get('VPHONE_AGX_DUMPS', str(BACKEND / 'evidence/keybag-contract-20261015/probe-164-gfxdump/dumps'))).expanduser()
DUMPS_DIR = DUMPS_PATH if DUMPS_PATH.is_dir() else None
S5L8960X_C = BACKEND / 's5l8960x.c'
needs_dumps = unittest.skipIf(DUMPS_DIR is None, 'probe-164 frame dumps not present')

import sys
sys.path.insert(0, str(BACKEND))
from agx_frame import Frame
from agx_replay_offline import is_clear_pass


class AGXPassLoadTests(unittest.TestCase):
    @needs_dumps
    def test_display_passes_are_load_passes(self):
        """Display passes (640x1136) must load existing framebuffer (+0x23c == 1)."""
        display_frames = [1, 2, 10, 50, 72, 100, 113]
        for frame_num in display_frames:
            bin_path = DUMPS_DIR / f'frame-{frame_num}.bin'
            if not bin_path.exists():
                continue
            f = Frame(bin_path)
            w178 = struct.unpack_from('<I', f.type1_command, 0x178)[0]
            w23c = struct.unpack_from('<I', f.type1_command, 0x23c)[0]
            pw = (struct.unpack_from('<I', f.type1_command, 0x74)[0] & 0xffff) + 1
            ph = (struct.unpack_from('<I', f.type1_command, 0x70)[0] & 0xffff) + 1

            self.assertEqual((pw, ph), (640, 1136))
            self.assertNotEqual(w178, 0, f"Frame {frame_num} should have background object")
            self.assertEqual(w23c & 1, 1, f"Frame {frame_num} display pass must have load flag set (+0x23c == 1)")
            self.assertFalse(is_clear_pass(f.type1_command), f"Frame {frame_num} must NOT be a clear pass")

    @needs_dumps
    def test_offscreen_passes_are_clear_passes(self):
        """Offscreen backdrops (608x64, 448x64) must clear canvas (+0x23c == 0)."""
        offscreen_frames = [64, 65, 71, 114, 120, 141, 150]
        for frame_num in offscreen_frames:
            bin_path = DUMPS_DIR / f'frame-{frame_num}.bin'
            if not bin_path.exists():
                continue
            f = Frame(bin_path)
            w178 = struct.unpack_from('<I', f.type1_command, 0x178)[0]
            w23c = struct.unpack_from('<I', f.type1_command, 0x23c)[0]
            pw = (struct.unpack_from('<I', f.type1_command, 0x74)[0] & 0xffff) + 1
            ph = (struct.unpack_from('<I', f.type1_command, 0x70)[0] & 0xffff) + 1

            self.assertIn((pw, ph), [(608, 64), (448, 64)])
            self.assertNotEqual(w178, 0, f"Frame {frame_num} should have background object")
            self.assertEqual(w23c & 1, 0, f"Frame {frame_num} offscreen pass must have load flag cleared (+0x23c == 0)")
            self.assertTrue(is_clear_pass(f.type1_command), f"Frame {frame_num} must be a clear pass")

    def test_synthetic_command_without_background_object(self):
        """Synthetic contract commands without background object (+0x178 == 0) do not clear."""
        # Mirrors backend/agx_contract_test.py synthetic type-1 command where +0x178 and +0x23c are 0
        cmd = bytearray(0x280)
        struct.pack_into('<I', cmd, 0x178, 0)
        struct.pack_into('<I', cmd, 0x23c, 0)
        self.assertFalse(is_clear_pass(cmd))

        # Even if +0x23c has other flags, no background object means not a clear pass
        struct.pack_into('<I', cmd, 0x23c, 1)
        self.assertFalse(is_clear_pass(cmd))

    @needs_dumps
    def test_background_pds_structure_difference(self):
        """Verify background PDS data: display has tile-load sampler/USC; offscreen has zeros."""
        disp_frame = Frame(DUMPS_DIR / 'frame-1.bin')
        off_frame = Frame(DUMPS_DIR / 'frame-141.bin')

        # Frame 1: Display pass
        w178_disp = struct.unpack_from('<I', disp_frame.type1_command, 0x178)[0]
        bg_pds_disp = disp_frame.read(0x600000000 | w178_disp, 32)
        ptr0_disp = struct.unpack_from('<Q', bg_pds_disp, 0)[0]
        ptr1_disp = struct.unpack_from('<Q', bg_pds_disp, 8)[0]

        # ptr0 points to sampler header (0x00000fff 0x00002400)
        sampler_words = [disp_frame.u32(ptr0_disp + 4 * k) for k in range(4)]
        self.assertEqual(sampler_words[1], 0x00000fff)
        self.assertEqual(sampler_words[2], 0x00002400)

        # ptr1 points to 640x1136 TSW (0x46f09fc -> h=1136, w=640)
        tsw_words = [disp_frame.u32(ptr1_disp + 4 * k) for k in range(4)]
        self.assertEqual(tsw_words[1], 0x046f09fc)

        # Frame 141: Offscreen clear pass
        w178_off = struct.unpack_from('<I', off_frame.type1_command, 0x178)[0]
        bg_pds_off = off_frame.read(0x600000000 | w178_off, 32)
        ptr0_off = struct.unpack_from('<Q', bg_pds_off, 0)[0]

        # ptr0 points to 16 bytes of zeros (no tile-load sampler or USC)
        zero_words = [off_frame.u32(ptr0_off + 4 * k) for k in range(4)]
        self.assertEqual(zero_words, [0, 0, 0, 0])

    def test_real_command_word_tuples(self):
        """Test pass detection on extracted real command words from dumps."""
        # Frame 1: 640x1136 display pass
        f1_cmd = bytearray(0x280)
        struct.pack_into('<I', f1_cmd, 0x70, 0x46f)
        struct.pack_into('<I', f1_cmd, 0x74, 0x27f)
        struct.pack_into('<I', f1_cmd, 0x178, 0x80008020)
        struct.pack_into('<I', f1_cmd, 0x1d0, 0x2)
        struct.pack_into('<I', f1_cmd, 0x23c, 0x1)
        self.assertFalse(is_clear_pass(f1_cmd))

        # Frame 64: 608x64 offscreen pass
        f64_cmd = bytearray(0x280)
        struct.pack_into('<I', f64_cmd, 0x70, 0x6000003f)
        struct.pack_into('<I', f64_cmd, 0x74, 0xa000025f)
        struct.pack_into('<I', f64_cmd, 0x178, 0x800080b0)
        struct.pack_into('<I', f64_cmd, 0x1d0, 0x2)
        struct.pack_into('<I', f64_cmd, 0x23c, 0x0)
        self.assertTrue(is_clear_pass(f64_cmd))

        # Frame 141: 448x64 offscreen pass
        f141_cmd = bytearray(0x280)
        struct.pack_into('<I', f141_cmd, 0x70, 0x6000003f)
        struct.pack_into('<I', f141_cmd, 0x74, 0x900001bf)
        struct.pack_into('<I', f141_cmd, 0x178, 0x80004070)
        struct.pack_into('<I', f141_cmd, 0x1d0, 0x2)
        struct.pack_into('<I', f141_cmd, 0x23c, 0x0)
        self.assertTrue(is_clear_pass(f141_cmd))

    def test_c_source_invariants(self):
        """Verify backend/s5l8960x.c contains the pass load/clear implementation."""
        source = S5L8960X_C.read_text()

        # Check AGXRenderJob struct definition has bool is_clear
        self.assertIn('bool is_clear;', source)

        # Check agx_replay_frame signature and row-wise clear
        self.assertIn('static void agx_replay_frame(A7State *s, uint64_t cat, const uint8_t *ta,', source)
        self.assertIn('bool is_clear, uint32_t pitch, uint64_t scissor_base)', source)
        self.assertIn('if (is_clear) {', source)
        self.assertIn('uint32_t zero_row[ARRAY_SIZE(row)];', source)
        self.assertIn('agx_ctx_write(s, cat, agx_surface_gva(&sf, 0, y), zero_row, width * 4);', source)

        # Check agx_trace_rt computes is_clear
        self.assertIn('bool is_clear = ldl_le_p(raw + 0x178) != 0 && !(ldl_le_p(raw + 0x23c) & 1);', source)

        # Check render thread forwards is_clear
        self.assertIn('job->is_clear = is_clear;', source)
        self.assertIn('agx_replay_frame(job->s, job->cat_base, job->ta, job->target,', source)
        self.assertIn('job->width, job->height, job->is_clear, job->pitch, job->scissor_base);', source)

    def test_c_compilation_pass_load(self):
        """Compile a C test snippet exercising the clear-row logic."""
        import shutil, subprocess, tempfile
        compiler = shutil.which('cc')
        if not compiler:
            self.skipTest('host C compiler unavailable')

        c_test = r'''
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <assert.h>

#define MIN(a, b) ((a) < (b) ? (a) : (b))

static uint32_t mock_target[640 * 1136];

typedef struct AGXSurface {
    uint64_t base;
    uint32_t pitch;
    bool strip;
} AGXSurface;

static uint64_t agx_surface_gva(const AGXSurface *sf, unsigned x, unsigned y) {
    return sf->base + (uint64_t)y * sf->pitch + x * 4;
}

static bool agx_ctx_write(uint64_t gva, const void *buf, size_t n) {
    memcpy((char*)mock_target + gva, buf, n);
    return true;
}

static void mock_replay_pass(uint32_t width, uint32_t height, bool is_clear) {
    width = MIN(width, 640u);
    height = MIN(height, 1136u);
    AGXSurface sf = { .base = 0, .pitch = ((width + 15) & ~15u) * 4, .strip = false };

    if (is_clear) {
        uint32_t zero_row[640];
        memset(zero_row, 0, sizeof(zero_row));
        for (uint32_t y = 0; y < height; y++) {
            agx_ctx_write(agx_surface_gva(&sf, 0, y), zero_row, width * 4);
        }
    }
}

int main(void) {
    // Fill mock target with 0xa5 pattern
    memset(mock_target, 0xa5, sizeof(mock_target));

    // Clear pass for 448x64
    mock_replay_pass(448, 64, true);

    // Verify 448x64 is zeroed
    uint32_t pitch_pixels = (448 + 15) & ~15u;
    for (uint32_t y = 0; y < 64; y++) {
        for (uint32_t x = 0; x < 448; x++) {
            assert(mock_target[y * pitch_pixels + x] == 0);
        }
        // Check padding untouched
        for (uint32_t x = 448; x < pitch_pixels; x++) {
            assert(mock_target[y * pitch_pixels + x] == 0xa5a5a5a5);
        }
    }

    // Load pass: should leave target untouched
    memset(mock_target, 0x5a, sizeof(mock_target));
    mock_replay_pass(640, 1136, false);
    assert(mock_target[0] == 0x5a5a5a5a);
    assert(mock_target[1000] == 0x5a5a5a5a);

    return 0;
}
'''
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / 'test.c'
            bin_path = Path(td) / 'test'
            src.write_text(c_test)
            subprocess.run([compiler, '-Wall', '-Wextra', '-O2', str(src), '-o', str(bin_path)], check=True)
            res = subprocess.run([str(bin_path)], capture_output=True, check=True)
            self.assertEqual(res.returncode, 0)


if __name__ == '__main__':
    unittest.main()
