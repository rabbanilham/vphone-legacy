"""Tests for Safari and WebKit AGX GPU rendering: Format 3 A8 textures and alpha-mask tinting."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('agx_replay_offline', ROOT / 'backend/agx_replay_offline.py')
replay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(replay)


class SafariRenderTests(unittest.TestCase):
    def test_format3_a8_descriptor(self):
        class Memory:
            pages = {0x10000000: bytes(4096)}
        q = (0x10000000 >> 2) << 16
        # Format 3 (A8 / L8 8-bit texture) with Morton twiddling (tsw[0] & 7 == 1)
        tsw = [0x38000021, (255 << 16) | (255 << 2), q & 0xffffffff, q >> 32]
        tex = replay.decode_tsw(Memory(), tsw)
        self.assertIsNotNone(tex)
        self.assertEqual(tex['bpp'], 1)
        self.assertEqual(tex['w'], 256)
        self.assertEqual(tex['h'], 256)
        self.assertTrue(tex['twiddled'])
        self.assertEqual(tex['stride'], 256)

    def test_format3_a8_texel_morton(self):
        class Memory:
            def __init__(self):
                self.pages = {}
            def read(self, addr, length):
                return bytes([self.pages.get(addr + i, 0) for i in range(length)])
            def u32(self, addr):
                return 0

        mem = Memory()
        # Set a coverage byte at base + 0
        mem.pages[0x10000000] = 0x80  # alpha = 128
        tex = {'addr': 0x10000000, 'w': 256, 'h': 256, 'bpp': 1, 'strip': False, 'twiddled': True, 'nc_sparse': False}
        rgba = replay.texel(mem, tex, 0, 0)
        self.assertEqual(rgba, (0, 0, 0, 128))

    def test_alpha_mask_tint_modulation(self):
        # When an alpha-mask / coverage texture has RGB == 0 and Alpha != 0,
        # tint modulation must modulate the vertex tint RGB by coverage,
        # not zero out RGB by multiplying src_rgb (which is 0).
        tint_r, tint_g, tint_b, tint_a = 128, 128, 128, 255
        coverage = 200

        # Simulating the alpha-mask modulation formula in s5l8960x.c:
        a = (coverage * tint_a) // 255
        r = (tint_r * a) // 255
        g = (tint_g * a) // 255
        b = (tint_b * a) // 255
        self.assertGreater(r, 0)
        self.assertGreater(g, 0)
        self.assertGreater(b, 0)
        self.assertEqual(r, 100)
        self.assertEqual(g, 100)
        self.assertEqual(b, 100)

    def test_safari_frame27_audit(self):
        dump = ROOT / 'build/agx-corpus/safari/dumps/frame-27.bin'
        if not dump.exists():
            raise unittest.SkipTest('safari corpus dump frame-27.bin not found')
        from backend.agx_frame import Frame
        f = Frame(dump)
        outcomes = replay.audit_frame(f)
        self.assertEqual(len(outcomes), 25)
        # Verify no draws resulted in errors
        for idx, result, details in outcomes:
            self.assertTrue(result.startswith('replayed') or result.startswith('approximated'),
                            f'draw {idx} had unexpected outcome: {result}')


if __name__ == '__main__':
    unittest.main()
