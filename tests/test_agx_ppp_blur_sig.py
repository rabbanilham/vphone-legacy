"""Unit tests for AGX PPP incremental state retention, signature-based blur detection,
and UISwitch shadow edge smoothing (Task GC).

Verifies:
1. PPP bit-to-slot mapping and incremental state retention in C and Python.
2. Resolution of unlock and unlock-flyin Draw 2 (formerly 12 skipped draws).
3. USC preamble signature matching across all 22 blur shaders and rejection of non-blur shaders.
4. Removal of hard-coded blur hash lists in favor of 41-byte preamble matching.
5. UISwitch knob shadow transition smoothing at scissor boundary x=508.
"""
from pathlib import Path
import re
import unittest
from PIL import Image

from backend.agx_frame import Frame
import backend.agx_replay_offline as offline

ROOT = Path(__file__).resolve().parents[1]
C_SOURCE = ROOT / 'backend' / 's5l8960x.c'

BLUR_PREAMBLE_BYTES = bytes([
    0x2a, 0x0a, 0x94, 0x99, 0x99, 0xaf, 0x1b, 0x80,
    0x00, 0x33, 0x00, 0x00, 0x80, 0x40, 0x80, 0x01,
    0x88, 0x00, 0x20, 0xff, 0x3a, 0x8a, 0x80, 0x94,
    0x99, 0x99, 0xaf, 0x1b, 0x80, 0x00, 0x33, 0x00,
    0x00, 0x82, 0x40, 0x80, 0x03, 0x88, 0x00, 0x21,
    0x00
])

UNLOCK_FRAMES = [
    'corpus/unlock/dumps/frame-5.bin',
    'corpus/unlock/dumps/frame-19.bin',
    'corpus/unlock/dumps/frame-33.bin',
    'corpus/unlock/dumps/frame-47.bin',
    'corpus/unlock/dumps/frame-61.bin',
    'corpus/unlock/dumps/frame-75.bin',
    'corpus/unlock/dumps/frame-89.bin',
    'corpus/unlock-flyin/dumps/frame-5.bin',
    'corpus/unlock-flyin/dumps/frame-11.bin',
    'corpus/unlock-flyin/dumps/frame-17.bin',
    'corpus/unlock-flyin/dumps/frame-23.bin',
    'corpus/unlock-flyin/dumps/frame-29.bin',
]


class TestAGXPPPStateBlurSignature(unittest.TestCase):
    def test_ppp_bit_slot_mapping_and_retention(self):
        """Verify PPP state updates correctly map header bits to fixed 32 slots."""
        retained = [0] * 32

        # Draw 1: Full packet (hdr sets bits 0..6, 9..11, 23)
        # Bit 6 has 4 words starting at idx 7 (slot 6 = prog ptr)
        # Bit 9 has 1 word (slot 12 = texture PDS ptr)
        full_packet = [
            (1 << 0) | (1 << 1) | (1 << 2) | (1 << 3) | (1 << 4) | (1 << 5) |
            (1 << 6) | (1 << 9) | (1 << 10) | (1 << 11) | (1 << 23),
            0x10001, 0x20002, 0x30003, 0x40004, 0x50005, 0x60006,  # slots 0..5
            0x12340000, 0x12340001, 0x12340002, 0x12340003,        # slots 6..9 (bit 6)
            0x8000beef,                                             # slot 12 (bit 9)
            0xa0001, 0xa0002,                                       # slots 13..14 (bit 10)
            0x42000000, 0x42000000, 0x42000000, 0x42000000, 0x3f000000, 0x3f000000,  # slots 15..20 (bit 11)
            0xcc001, 0xcc002                                        # slots 28..29 (bit 23)
        ]
        offline.update_ppp_state(retained, full_packet)
        self.assertEqual(retained[6], 0x12340000)
        self.assertEqual(retained[12], 0x8000beef)
        self.assertEqual(retained[28], 0xcc001)

        # Draw 2: Incremental packet (hdr 0xc3f: bits 0..5, 10, 11 only)
        # Omits bit 6 (prog ptr), bit 9 (texture), bit 23 (blend)
        inc_packet = [
            (1 << 0) | (1 << 1) | (1 << 2) | (1 << 3) | (1 << 4) | (1 << 5) |
            (1 << 10) | (1 << 11),
            0x90001, 0x90002, 0x90003, 0x90004, 0x90005, 0x90006,  # slots 0..5
            0xb0001, 0xb0002,                                       # slots 13..14 (bit 10)
            0x43000000, 0x43000000, 0x43000000, 0x43000000, 0x3f000000, 0x3f000000   # slots 15..20 (bit 11)
        ]
        offline.update_ppp_state(retained, inc_packet)

        # Updated slots
        self.assertEqual(retained[0], 0x90001)
        self.assertEqual(retained[13], 0xb0001)
        self.assertEqual(retained[15], 0x43000000)
        # RETAINED slots: program, texture, blend MUST persist untouched
        self.assertEqual(retained[6], 0x12340000)
        self.assertEqual(retained[12], 0x8000beef)
        self.assertEqual(retained[28], 0xcc001)

    def test_unlock_draw2_ppp_state_retention(self):
        """Verify Draw 2 in all 12 unlock/flyin dumps resolves prog and texture from Draw 1."""
        for path_str in UNLOCK_FRAMES:
            dump_path = ROOT / path_str
            if not dump_path.exists():
                self.skipTest(f'dump {path_str} not present')
            f = Frame(str(dump_path))
            f.require_matching_ta()
            outcomes = offline.audit_frame(f)
            self.assertGreater(len(outcomes), 2)
            did, res, details = outcomes[2]
            self.assertEqual(did, 2)
            self.assertIn('textured quad', res)
            self.assertEqual(details.get('prog'), '0x8eb52cf6fe248b1d')

    def test_blur_preamble_signature_matches_corpus(self):
        """Verify all 22 blur programs match the 41-byte USC machine code preamble."""
        dump_dir = ROOT / 'corpus'
        if not dump_dir.exists():
            self.skipTest('corpus not present')
        matched_blur_progs = set()
        non_blur_progs = set()
        for p in dump_dir.glob('*/*/*.bin'):
            if p.name.endswith('frame.bin'):
                continue
            try:
                f = Frame(str(p))
                f.require_matching_ta()
                for vs_data, ppp, draw, vp in offline.walk(f):
                    pr = offline.program(f, ppp)
                    if not pr:
                        continue
                    pds_ptr = ppp[6] if len(ppp) == 32 else (ppp[7] if len(ppp) > 7 else ppp[6])
                    if not pds_ptr and len(ppp) > 7:
                        pds_ptr = ppp[7]
                    off = f.u32(0x600000000 | pds_ptr)
                    code = f.read(0x700000000 | off, len(BLUR_PREAMBLE_BYTES))
                    if code == BLUR_PREAMBLE_BYTES:
                        matched_blur_progs.add(pr)
                    else:
                        non_blur_progs.add(pr)
            except Exception:
                continue

        # All 22 blur programs match the preamble
        self.assertEqual(len(matched_blur_progs), 22)
        self.assertEqual(matched_blur_progs, offline.BLUR_PROGRAMS_12)
        # Zero non-blur programs match the preamble
        self.assertTrue(matched_blur_progs.isdisjoint(non_blur_progs))

    def test_c_source_no_blur_hash_list(self):
        """Verify C source does not contain hardcoded blur hash lists."""
        text = C_SOURCE.read_text(encoding='utf-8')
        for i in range(10, 32):
            self.assertNotIn(f'AGX_USC_BLUR_{i}', text)
        self.assertIn('agx_blur_preamble', text)
        self.assertIn('agx_is_blur_code', text)

    def test_uiswitch_shadow_edge_smooth_transition(self):
        """Verify knob shadow edge in settings-all frame-1 has a smooth gradient without dipping to 194."""
        report_f1 = ROOT / 'corpus' / 'report' / 'settings-all-frame-1.png'
        if not report_f1.exists():
            self.skipTest('report image settings-all-frame-1.png not generated')
        im = Image.open(str(report_f1)).convert('RGB')

        # At scissor boundary x=508, y=208, previously dipped to 194.
        # With coverage modulation in ROUNDED_CAPS, track fill is preserved and shadow gradient is smooth.
        p507 = im.getpixel((507, 208))[0]
        p508 = im.getpixel((508, 208))[0]
        p509 = im.getpixel((509, 208))[0]

        self.assertGreaterEqual(p508, 225, f"Pixel at x=508 dipped to {p508}; cliff not resolved")
        self.assertLessEqual(abs(p507 - p508), 20, f"Abrupt step from {p507} to {p508} at x=508")


if __name__ == '__main__':
    unittest.main()
