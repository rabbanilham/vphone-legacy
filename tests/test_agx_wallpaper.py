"""Uploaded Morton textures and exact PBE pitches used by CoreImage wallpaper."""
import ctypes
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('agx_wallpaper_offline', ROOT / 'backend/agx_replay_offline.py')
replay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(replay)


class WallpaperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which('cc')
        if not compiler:
            raise unittest.SkipTest('C compiler required')
        cls.tmp = tempfile.TemporaryDirectory()
        src = (ROOT / 'backend/s5l8960x.c').read_text()
        fn = src[src.index('static uint64_t agx_texel_gva('):src.index('/* A Morton row')]
        harness = Path(cls.tmp.name) / 'layout.c'
        harness.write_text('''#include <stdint.h>
#include <stdbool.h>
typedef struct { uint64_t addr; uint32_t w, h, stride; bool strip, twiddled; } AGXTexture;
''' + fn + '''
uint64_t address(unsigned w, unsigned h, unsigned x, unsigned y) {
    AGXTexture t = { .addr = 0x10000000, .w = w, .h = h, .twiddled = true };
    return agx_texel_gva(&t, x, y);
}
''')
        binary = Path(cls.tmp.name) / 'layout.so'
        subprocess.run([compiler, '-shared', '-fPIC', str(harness), '-o', str(binary)], check=True)
        cls.lib = ctypes.CDLL(str(binary))
        cls.lib.address.restype = ctypes.c_uint64

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_y_first_morton_rectangles(self):
        class Memory:
            def read(self, address, length):
                self.address = address
                return bytes(length)
        # Independent offsets: Y is the low bit, X the next; after the
        # smaller power-of-two dimension, append remaining coordinate bits.
        cases = [(4, 2, 0, 1, 4), (4, 2, 1, 0, 8), (4, 2, 3, 1, 28),
                 (2, 8, 1, 7, 60), (744, 1392, 0, 1024, 0x400000),
                 (744, 1392, 512, 0, 0x200000)]
        for w, h, x, y, offset in cases:
            with self.subTest(w=w, h=h, x=x, y=y):
                expected = 0x10000000 + offset
                self.assertEqual(self.lib.address(w, h, x, y), expected)
                memory = Memory()
                replay.texel(memory, dict(addr=0x10000000, w=w, h=h, twiddled=True), x, y)
                self.assertEqual(memory.address, expected)

    def test_uploaded_and_rendered_descriptors_stay_distinct(self):
        class Memory:
            pages = {0x10000000: bytes(4096)}
        q = (0x10000000 >> 2) << 16
        for word, twiddled in [(0x60008861, True), (0x60000a61, False), (0x60008864, False)]:
            tex = replay.decode_tsw(Memory(), [word, 31 << 16 | 31 << 2, q & 0xffffffff, q >> 32])
            self.assertEqual(tex['twiddled'], twiddled)
