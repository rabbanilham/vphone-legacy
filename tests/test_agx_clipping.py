"""Pixel oracles for viewport crops of a textured quad (C/offline parity)."""
import ctypes
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("clipping_replay", ROOT / "backend/agx_replay_offline.py")
replay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(replay)


class QuadClippingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("cc")
        if not compiler:
            raise unittest.SkipTest("C compiler unavailable")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        source = (ROOT / "backend/s5l8960x.c").read_text()
        start = source.index("static float agx_quad_coordinate(")
        end = source.index("\n}", start) + 2
        harness = Path(cls.tmp.name) / "clipping.c"
        affine_start = source.index("static bool agx_quad_affine(")
        affine_end = source.index("\n}", affine_start) + 2
        harness.write_text("#include <stdbool.h>\n#include <math.h>\n" + source[start:end] + "\n" +
                           source[affine_start:affine_end] +
                           "\nfloat coordinate(float p,float l,float h,float a,float b) { return agx_quad_coordinate(p,l,h,a,b); }\n" +
                           "int affine(const float *xy,const float *uv,float *out) { return agx_quad_affine((const float (*)[2])xy,(const float (*)[2])uv,out); }\n")
        binary = Path(cls.tmp.name) / "clipping.dylib"
        subprocess.run([compiler, "-shared", "-fPIC", str(harness), "-o", str(binary)], check=True)
        cls.library = ctypes.CDLL(str(binary))
        cls.coordinate = cls.library.coordinate
        cls.coordinate.argtypes = [ctypes.c_float] * 5
        cls.coordinate.restype = ctypes.c_float

    def test_visible_pixels_are_a_crop_of_full_texture(self):
        # Eight distinct texels span [-2,6); viewport [0,4) must see 2..5,
        # rather than rescaling all eight texels into the four visible pixels.
        for scale in (1, 8):
            expected = [2, 3, 4, 5]
            for fn in (replay.quad_coordinate, self.coordinate):
                sampled = [int(fn(x, -2, 6, 0, scale) * 8 / scale) for x in range(4)]
                self.assertEqual(sampled, expected)

    def test_fractional_and_right_edge_crops_keep_original_domain(self):
        for fn in (replay.quad_coordinate, self.coordinate):
            self.assertAlmostEqual(fn(0, -0.25, 3.75, 0.25, 0.75), 0.34375)
            self.assertAlmostEqual(fn(15, 14, 18, 0, 1), 0.375)
            self.assertAlmostEqual(fn(7, 0, 8, 0, 1), 0.9375)

    def test_wallpaper_crop_preserves_texel_centres(self):
        # Observed native Home wallpaper quad: x=-52..692, width=744.
        for fn in (replay.quad_coordinate, self.coordinate):
            self.assertAlmostEqual(fn(0, -52, 692, 0, 1) * 744, 52.5, places=4)
            self.assertAlmostEqual(fn(639, -52, 692, 0, 1) * 744, 691.5, places=4)

    def test_rotated_and_constant_v_match_vertex_associations(self):
        xy = [(2, 0), (4, 0), (4, 4)]
        for uv in ([(0, 1), (0, 0), (1, 0)], [(0, 0.5), (0, 0.5), (1, 0.5)]):
            expected = replay.quad_affine(xy, uv)
            raw_xy = (ctypes.c_float * 6)(*(v for p in xy for v in p))
            raw_uv = (ctypes.c_float * 6)(*(v for p in uv for v in p))
            out = (ctypes.c_float * 6)()
            self.assertTrue(self.library.affine(raw_xy, raw_uv, out))
            for c, e in zip(out, expected):
                self.assertAlmostEqual(c, e)
            for (x, y), (u, v) in zip(xy, uv):
                self.assertAlmostEqual(out[0] * x + out[1] * y + out[2], u)
                self.assertAlmostEqual(out[3] * x + out[4] * y + out[5], v)

    def test_degenerate_vertices_are_rejected(self):
        xy = [(0, 0), (1, 0), (2, 0)]
        uv = [(0, 0), (1, 0), (1, 1)]
        self.assertIsNone(replay.quad_affine(xy, uv))
        raw_xy = (ctypes.c_float * 6)(*(v for p in xy for v in p))
        raw_uv = (ctypes.c_float * 6)(*(v for p in uv for v in p))
        self.assertFalse(self.library.affine(raw_xy, raw_uv, (ctypes.c_float * 6)()))
