"""Key-press balloon: Morton-sparse 224x256 passes and the masked backdrop composite.

The iOS 7 key popup renders a backdrop pass and a balloon-alpha mask pass into
224x256 offscreen targets, then composites the backdrop through the mask with a
stride-36 two-sampler program (0x4126.../0x66c0.../0xb618...). Those targets are
Y-first Morton surfaces whose pages for 32x32 tiles past the 224-pixel width are
unmapped (pages 42,43,46,47,...). Read as linear rows they had holes (white bars
on neighbouring keys) and the composite was skipped or drawn unmasked (an opaque
grey rectangle of the balloon's bounding box).

Fixtures are trimmed live AGXF dumps of one animation tick, zlib-compressed:
  frame-24: backdrop pass (224x256), frame-25: balloon mask pass,
  frame-26: display pass; its rectangle (x40..280, y560..840) is prefilled with
  0xf2f2f2 so the result shows only what this tick's composite draws.
"""
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
sys.path.insert(0, str(ROOT / 'scripts'))
import agx_replay_offline as replay

FIX = ROOT / 'tests/fixtures/agx_key_balloon'


def write_frames(tmp):
    paths = []
    for n in (24, 25, 26):
        p = Path(tmp) / f'f{n}.bin'
        p.write_bytes(zlib.decompress((FIX / f'frame-{n}.agxf.z').read_bytes()))
        paths.append(p)
    return paths


class KeyBalloonTests(unittest.TestCase):
    def test_c_core_chain(self):
        import agx_corpus
        with tempfile.TemporaryDirectory() as tmp:
            try:
                binary = agx_corpus.build_harness(Path(tmp) / 'harness')
            except (subprocess.CalledProcessError, OSError, ValueError) as error:
                raise unittest.SkipTest(f'host harness unavailable: {error}')
            frames = write_frames(tmp)
            result = subprocess.run([str(binary), '--chain', str(Path(tmp) / 'c'), *map(str, frames)],
                                    check=True, capture_output=True, text=True)
            lines = result.stdout.strip().splitlines()
            self.assertIn('"missing_rows": 0', lines[0])    # Morton-sparse target, no holes
            self.assertIn('"missing_rows": 0', lines[1])

            def render(i):
                d = (Path(tmp) / f'c-{i}.rgba').read_bytes()
                w, h = struct.unpack_from('<II', d, 4)
                return w, d[12:]

            w, px = render(2)

            def pix(x, y):
                o = (y * w + x) * 4
                return tuple(px[o:o + 3])
            # Inside the 204x228 bounding box but outside the balloon silhouette the
            # backdrop is masked away: the prefilled page grey stays.
            self.assertEqual(pix(70, 600), (0xf2, 0xf2, 0xf2))
            self.assertEqual(pix(250, 640), (0xf2, 0xf2, 0xf2))
            # Inside the silhouette the white key cap is drawn.
            self.assertTrue(all(c >= 0xf8 for c in pix(160, 700)))

    def test_python_mirror_sparse_and_programs(self):
        frame = replay.Frame(write_frames(tempfile.mkdtemp())[0])
        self.assertTrue(replay.nc_sparse_layout(frame, 0x10a4c000, 224, 256))
        for prog in (0x41266dfcceb2ee28, 0x066c0e6f88bb95a9, 0xb61824c33264c501):
            self.assertIn(prog, replay.DUAL_TEX_PROGRAMS)


if __name__ == '__main__':
    unittest.main()
