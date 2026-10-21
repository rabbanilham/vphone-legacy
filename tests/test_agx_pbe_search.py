"""Exercise the actual diagnostic C helpers without building/running QEMU."""
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'backend/s5l8960x.c'


class PBESearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which('cc')
        if not compiler:
            raise unittest.SkipTest('host C compiler unavailable')
        cls.temp = tempfile.TemporaryDirectory(prefix='agx-pbe-test-')
        cls.addClassCleanup(cls.temp.cleanup)
        root = Path(cls.temp.name)
        source = SOURCE.read_text()
        helpers = source[source.index('static void agx_pbe_scan('):
                         source.index('static bool agx_ctx_read(A7State *s, uint64_t cat, uint64_t gva, void *buf, size_t n);')]
        # One context buffer and one firmware META buffer, each a single page.
        # The page following each is unmapped: reads must stop there.
        harness = r'''
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define MIN(a, b) ((a) < (b) ? (a) : (b))
#define LOG_UNIMP 0
#define MEMTXATTRS_UNSPECIFIED 0
#define MEMTX_OK 0
#define qemu_log_mask(mask, ...) printf(__VA_ARGS__)
typedef uint64_t hwaddr;
typedef struct { uint64_t agx_ctx_cat[16], agx_cat_base; } A7State;
static int address_space_memory;
static uint8_t memory[0x3000];
static uint32_t ldl_le_p(const void *p) {
    const uint8_t *b = p;
    return (uint32_t)b[0] | (uint32_t)b[1] << 8 |
           (uint32_t)b[2] << 16 | (uint32_t)b[3] << 24;
}
static uint64_t ldq_le_p(const void *p) {
    return ldl_le_p(p) | (uint64_t)ldl_le_p((const uint8_t *)p + 4) << 32;
}
static uint64_t agx_meta_gva(uint32_t a) {
    return UINT64_C(0xe1c0000000) + (a & 0x3fffffff);
}
static bool agx_gpu_translate_ctx(A7State *s, uint64_t cat,
                                  uint64_t gva, hwaddr *pa) {
    (void)s;
    if (cat == 10 && gva >= 0x1000 && gva < 0x2000) {
        *pa = gva;
        return true;
    }
    if (cat == 20 && gva >= agx_meta_gva(0xc0460000) &&
        gva < agx_meta_gva(0xc0461000)) {
        *pa = 0x2000 + gva - agx_meta_gva(0xc0460000);
        return true;
    }
    return false;
}
static int address_space_read(void *as, hwaddr pa, int attrs,
                              void *buf, size_t n) {
    (void)as; (void)attrs;
    if (pa + n > sizeof(memory)) abort();
    memcpy(buf, memory + pa, n);
    return MEMTX_OK;
}
'''
        main = r'''
int main(int argc, char **argv) {
    uint8_t raw[0x10000] = {0};
    size_t n = fread(raw, 1, sizeof(raw), stdin);
    if (argc == 1) {
        agx_pbe_scan(raw, n, 0x40, 0x1000);
    } else {
        A7State s = { .agx_ctx_cat = {[4] = 10}, .agx_cat_base = 20 };
        /* 0x400470000 >> 4, explicitly little endian. */
        const uint8_t hit[] = {0x00, 0x70, 0x04, 0x40, 0, 0, 0, 0};
        memcpy(memory + 0x1100, hit, sizeof(hit));
        memcpy(memory + 0x2100, hit, sizeof(hit));
        for (unsigned i = 0; i < (unsigned)atoi(argv[1]); i++) {
            agx_pbe_search(&s, raw, 0x40404000);
        }
    }
    return 0;
}
'''
        (root / 'test.c').write_text(harness + helpers + main)
        cls.binary = root / 'test'
        subprocess.run([compiler, '-std=c11', '-Wall', '-Wextra', '-Werror',
                        str(root / 'test.c'), '-o', str(cls.binary)],
                       check=True, capture_output=True, text=True)

    def run_helper(self, data, count=None, enabled=True):
        env = os.environ.copy()
        env.pop('VPHONE_AGX_PBE_SEARCH', None)
        if enabled:
            env['VPHONE_AGX_PBE_SEARCH'] = '1'
        args = [str(self.binary)] + ([] if count is None else [str(count)])
        return subprocess.run(args, input=data, env=env, capture_output=True,
                              check=True).stdout.decode().splitlines()

    def test_shifted_surface_exactly_one_hit(self):
        data = bytearray(64)
        struct.pack_into('<Q', data, 12, 0x400470000 >> 4)
        self.assertEqual(self.run_helper(data), [
            'AGX PBE hit cmd-field +0x40 buf gva 0x1000 off 0xc width 64 '
            'value 0x40047000 surface 0x400470000 shift 4'])

    def test_all_surfaces_shifts_and_u32_tail(self):
        for surface in (0x400000000, 0x400470000, 0x4008e0000):
            for shift in (0, 2, 4, 6, 8, 12):
                with self.subTest(surface=surface, shift=shift):
                    hits = self.run_helper(struct.pack('<Q', surface >> shift))
                    self.assertEqual(len(hits), 1)
                    self.assertIn(f'surface 0x{surface:x} shift {shift}', hits[0])
        self.assertIn('width 32', self.run_helper(struct.pack('<I', 0x40047000))[0])

    def test_tagged_u64_and_reject_near_matches(self):
        hits = self.run_helper(struct.pack('<Q', 0xab000004008e0000))
        self.assertEqual(len(hits), 1)
        self.assertIn('surface 0x4008e0000 shift 0', hits[0])
        self.assertEqual(self.run_helper(bytes(32)), [])
        self.assertEqual(self.run_helper(struct.pack('<Q', 0x40047001)), [])
        self.assertEqual(self.run_helper(b'\x00' + struct.pack('<Q', 0x40047000)), [])

    def test_context_meta_dedup_and_opt_in(self):
        raw = bytearray(0x280)
        struct.pack_into('<II', raw, 0, 1, 1)
        struct.pack_into('<I', raw, 0x18, 0x1000)
        struct.pack_into('<I', raw, 0x40, 0x1000)  # duplicate buffer
        struct.pack_into('<I', raw, 0x0c, 0xc0460000)
        hits = self.run_helper(raw, 1)
        self.assertEqual(len(hits), 2)
        self.assertTrue(any('cmd-field +0xc buf gva 0xe1c0460000' in h for h in hits))
        self.assertTrue(any('cmd-field +0x18 buf gva 0x1000' in h for h in hits))
        self.assertEqual(self.run_helper(raw, 1, enabled=False), [])
        struct.pack_into('<I', raw, 4, 0xffffffff)  # cannot wrap ctx + 3
        self.assertEqual(len(self.run_helper(raw, 1)), 1)  # META still valid

    def test_command_and_200_command_cap(self):
        raw = bytearray(0x280)
        struct.pack_into('<Q', raw, 0x200, 0x40047000)
        lines = self.run_helper(raw, 201)
        self.assertEqual(len(lines), 201)
        self.assertIn('cmd-field +0x0 buf gva 0xe1c0404000 off 0x200', lines[0])
        self.assertEqual(lines[-1], 'AGX PBE search disabled after 200 commands')


if __name__ == '__main__':
    unittest.main()
