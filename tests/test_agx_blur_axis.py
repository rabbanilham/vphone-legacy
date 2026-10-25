"""Verify axis-only blur sampling against the general production sampler."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'backend/s5l8960x.c'


class BlurAxisTests(unittest.TestCase):
    def test_bit_exact_axis_samples_and_complete_passes(self):
        cc = shutil.which('cc')
        if not cc:
            self.skipTest('host C compiler unavailable')
        source = SOURCE.read_text()
        helpers = source[source.index('static inline uint32_t agx_bilinear_interpolate('):
                         source.index('static void agx_blur_texture(')]
        harness = r'''
#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <math.h>
#include <assert.h>
'''
        checks = r'''
static uint32_t rng = 0x3241abcdu;
static uint32_t random_pixel(void) {
    rng ^= rng << 13; rng ^= rng >> 17; rng ^= rng << 5; return rng;
}
/* Retain the general 2-D sampling oracle, including per-sample quantization
 * before weighted accumulation. This intentionally does not use the fast path. */
static void reference_blur(uint32_t *in, uint32_t *tmp, unsigned w, unsigned h,
                           const float *off, const float *weights, unsigned taps, bool vertical) {
    for (unsigned y = 0; y < h; y++) for (unsigned x = 0; x < w; x++) {
        float acc[4] = {0};
        for (unsigned t = 0; t < taps; t++) {
            float u = x + 0.5f, v = y + 0.5f;
            uint32_t a = agx_sample_bilinear(in, w, h,
                vertical ? u : u + off[t], vertical ? v + off[t] : v);
            uint32_t b = agx_sample_bilinear(in, w, h,
                vertical ? u : u - off[t], vertical ? v - off[t] : v);
            for (unsigned c = 0; c < 4; c++)
                acc[c] += ((float)((a >> (c*8)) & 255) +
                           (float)((b >> (c*8)) & 255)) * weights[t];
        }
        uint32_t out = 0;
        for (unsigned c = 0; c < 4; c++) {
            int n = (int)lroundf(acc[c]); n = n < 0 ? 0 : (n > 255 ? 255 : n);
            out |= (uint32_t)n << (c*8);
        }
        tmp[y*w+x] = out;
    }
    memcpy(in, tmp, (size_t)w*h*4);
}
int main(void) {
    /* All 257 quantized weights, random independent BGRA channels. */
    for (unsigned n = 0; n < 2048; n++) {
        uint32_t p[4] = {random_pixel(), random_pixel(), random_pixel(), random_pixel()};
        for (unsigned k = 0; k <= 256; k++) {
            float coordinate = 0.5f + k / 256.0f;
            assert(agx_sample_linear_axis(p, 2, 1, coordinate) ==
                   agx_sample_bilinear(p, 2, 2, coordinate, 0.5f));
            assert(agx_sample_linear_axis(p, 2, 2, coordinate) ==
                   agx_sample_bilinear(p, 2, 2, 0.5f, coordinate));
        }
    }
    const unsigned sizes[][2] = {{1,1},{1,31},{37,1},{2,2},{17,13},{608,64}};
    const float offsets[7] = {0, 0.638336f, 2.475021f, 4.455121f, 6.435364f, 10.396517f, 1000.25f};
    const float weights[7] = {0.118916f,0.141265f,0.107065f,0.069256f,0.038236f,0.018016f,0.007245f};
    const unsigned taps[] = {1,4,6,7};
    for (unsigned size = 0; size < sizeof(sizes)/sizeof(sizes[0]); size++) {
        unsigned w=sizes[size][0], h=sizes[size][1]; size_t bytes=(size_t)w*h*4;
        uint32_t *a=malloc(bytes), *b=malloc(bytes), *tmp=malloc(bytes), *ref_tmp=malloc(bytes);
        assert(a && b && tmp && ref_tmp);
        for (unsigned t=0; t<sizeof(taps)/sizeof(taps[0]); t++) {
            for (unsigned i=0; i<w*h; i++) a[i]=random_pixel();
            memcpy(b,a,bytes);
            for (unsigned axis=0; axis<2; axis++) {
                agx_blur_1d(a,tmp,w,h,offsets,weights,taps[t],axis);
                reference_blur(b,ref_tmp,w,h,offsets,weights,taps[t],axis);
                if (memcmp(a,b,bytes)) { fprintf(stderr,"mismatch %ux%u taps%u axis%u\n",w,h,taps[t],axis);return 1; }
            }
        }
        free(a); free(b); free(tmp); free(ref_tmp);
    }
    return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix='agx-blur-axis-') as directory:
            root = Path(directory)
            cfile = root / 'test.c'
            cfile.write_text(harness + helpers + checks)
            build = subprocess.run([cc, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                                    '-fsanitize=address,undefined', str(cfile), '-lm',
                                    '-o', str(root / 'test')], capture_output=True, text=True)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(root / 'test')], capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stderr)
