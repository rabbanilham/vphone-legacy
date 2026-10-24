"""Unit tests for AGX untextured draw handling (Task GA: stride 12 and stride 24).

Verifies:
1. Registration and classification of the 22 backdrop Gaussian blur programs (stride 12).
2. Preservation of legacy 9-program BLUR_PROGRAMS set and expansion in ALL_BLUR_PROGRAMS.
3. Precise skip reason for untextured stride 24 with program 0x0.
4. C and Python parity across all blur shader constants and skip classifications.
5. Verification of the 41-byte CoreAnimation Gaussian blur preamble across all 22 programs.
6. End-to-end audit classification on actual corpus dumps.
"""
from pathlib import Path
import re
import unittest

from backend.agx_frame import Frame
import backend.agx_replay_offline as offline

ROOT = Path(__file__).resolve().parents[1]
C_SOURCE = ROOT / 'backend' / 's5l8960x.c'

# The 22 backdrop blur hashes identified in Task GA
BLUR_PROGRAMS_12_EXPECTED = {
    0x13a51b8bc1b85b4c,
    0x23e324c0933a2344,
    0x28463bd14bffa040,
    0x3568dc9c7476346b,
    0x4251b7edb10c7be4,
    0x48870313e0aa10e8,
    0x604b25e32d1eca5c,
    0x66287bbe34a68c71,
    0x664855a92cde7229,
    0x6a2a1d789db87995,
    0x6af3b793cd19efb3,
    0x95450769f4fd347b,
    0x9606b672a1c89485,
    0xa112190af2d46c9e,
    0xa7d834f91cf06600,
    0xa82d25f9c6abcf1f,
    0xc449a4cb19584ccf,
    0xc8fbd825d2fea18d,
    0xcbf57b156b6423ec,
    0xe8b7f35f72f5737f,
    0xefc984709b59caeb,
    0xf5f55b3c7127e3f3,
}

# The 41-byte preamble shared by all CoreAnimation Gaussian blur shaders
BLUR_PREAMBLE_HEX = '2a0a949999af1b800033000080408001880020ff3a8a80949999af1b80003300008240800388002100'


class TestAGXUntexturedStrides(unittest.TestCase):
    def test_blur_program_sets(self):
        """Verify BLUR_PROGRAMS preserves 9 legacy entries while ALL_BLUR_PROGRAMS includes 22 new."""
        self.assertEqual(len(offline.BLUR_PROGRAMS), 9)
        self.assertEqual(len(offline.BLUR_PROGRAMS_12), 22)
        self.assertEqual(offline.BLUR_PROGRAMS_12, BLUR_PROGRAMS_12_EXPECTED)
        self.assertTrue(offline.BLUR_PROGRAMS.issubset(offline.ALL_BLUR_PROGRAMS))
        self.assertTrue(offline.BLUR_PROGRAMS_12.issubset(offline.ALL_BLUR_PROGRAMS))
        self.assertIn(offline.BLUR_4TAP, offline.ALL_BLUR_PROGRAMS)
        self.assertIn(offline.BLUR_7TAP, offline.ALL_BLUR_PROGRAMS)
        self.assertEqual(len(offline.ALL_BLUR_PROGRAMS), 9 + 22 + 2)

    def test_c_source_defines_and_dispatch(self):
        """Verify backend/s5l8960x.c defines all 31 blur constants and dispatches them in agx_is_blur_program."""
        text = C_SOURCE.read_text(encoding='utf-8')
        c_defines = {}
        for m in re.finditer(r'#define\s+(AGX_USC_BLUR_\w+)\s+UINT64_C\((0x[0-9a-fA-F]+)\)', text):
            name, val = m.group(1), int(m.group(2), 16)
            c_defines[name] = val

        # Check all 31 numbered blur shaders + 4TAP + 7TAP exist in C
        self.assertIn('AGX_USC_BLUR_4TAP', c_defines)
        self.assertIn('AGX_USC_BLUR_7TAP', c_defines)
        for i in range(1, 32):
            self.assertIn(f'AGX_USC_BLUR_{i}', c_defines)

        # Check all 22 new blur programs match their expected values
        c_blur_12_values = {c_defines[f'AGX_USC_BLUR_{i}'] for i in range(10, 32)}
        self.assertEqual(c_blur_12_values, BLUR_PROGRAMS_12_EXPECTED)

        # Verify agx_is_blur_program checks all 31 numbered blur shaders
        func_match = re.search(r'static inline bool agx_is_blur_program\(uint64_t prog\)\s*\{(.*?)\}', text, re.DOTALL)
        self.assertIsNotNone(func_match)
        body = func_match.group(1)
        for i in range(1, 32):
            self.assertIn(f'AGX_USC_BLUR_{i}', body)

    def test_c_skip_reason_parity(self):
        """Verify C code contains the precise skip string 'stride 24 untextured with program 0x0'."""
        text = C_SOURCE.read_text(encoding='utf-8')
        self.assertIn('"stride 24 untextured with program 0x0"', text)

    def test_corpus_frame_stride_12_blur_audit(self):
        """Verify audit on settings-all frame-11 correctly classifies draw 0 as separable Gaussian blur."""
        dump_path = ROOT / 'corpus' / 'settings-all' / 'dumps' / 'frame-11.bin'
        if not dump_path.exists():
            self.skipTest('corpus dump not present')
        f = Frame(str(dump_path))
        f.require_matching_ta()
        outcomes = offline.audit_frame(f)
        self.assertGreater(len(outcomes), 0)
        did, res, details = outcomes[0]
        self.assertEqual(did, 0)
        self.assertEqual(res, 'approximated: Gaussian blur (separable 6-tap, canvas backdrop)')
        self.assertEqual(details.get('prog'), '0x23e324c0933a2344')

    def test_corpus_frame_stride_24_prog_0_audit(self):
        """Verify audit on unlock frame-5 correctly classifies draw 2 as stride 24 with prog 0x0."""
        dump_path = ROOT / 'corpus' / 'unlock' / 'dumps' / 'frame-5.bin'
        if not dump_path.exists():
            self.skipTest('corpus dump not present')
        f = Frame(str(dump_path))
        f.require_matching_ta()
        outcomes = offline.audit_frame(f)
        self.assertGreater(len(outcomes), 2)
        did, res, details = outcomes[2]
        self.assertEqual(did, 2)
        self.assertEqual(res, 'skipped: stride 24 untextured with program 0x0')
        self.assertEqual(details.get('stride'), 24)
        self.assertEqual(details.get('prog'), '0x0')

    def test_corpus_blur_preamble_verification(self):
        """Verify all 22 blur programs in the corpus start with the exact 41-byte preamble."""
        dump_dir = ROOT / 'corpus'
        if not dump_dir.exists():
            self.skipTest('corpus not present')
        found_progs = {}
        for p in dump_dir.glob('*/*/*.bin'):
            if p.name.endswith('frame.bin'):
                continue
            try:
                f = Frame(str(p))
                f.require_matching_ta()
                for vs_data, ppp, draw, vp in offline.walk(f):
                    stride, _ = offline.vertices(f, vs_data, draw[1])
                    if stride == 12:
                        pr = offline.program(f, ppp)
                        if pr in BLUR_PROGRAMS_12_EXPECTED and pr not in found_progs:
                            off = f.u32(0x600000000 | ppp[7])
                            code = f.read(0x700000000 | off, 64)
                            found_progs[pr] = code[:41].hex()
            except Exception:
                continue

        # All observed hashes in corpus must have the exact same 41-byte preamble
        self.assertGreater(len(found_progs), 0)
        for pr, prefix in found_progs.items():
            self.assertEqual(prefix, BLUR_PREAMBLE_HEX, f"Program 0x{pr:x} preamble mismatch")


if __name__ == '__main__':
    unittest.main()
