#!/usr/bin/env python3
"""Benchmark production axis blur against a Git baseline; no firmware required."""
import argparse
import json
from pathlib import Path
import shutil
import statistics
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
HEADER = r"""#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <math.h>
#include <time.h>
"""
DRIVER = r"""static double now(void) {struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec+t.tv_nsec/1e9;}
int main(void) {
 const float off[]={.6575f,2.4492f,4.4092f,6.3704f,8.3332f,10.2979f};
 const float wt[]={.1666f,.1759f,.1005f,.0417f,.0126f,.0027f};
 unsigned sizes[][2]={{608,64},{640,1136}};
 for(unsigned z=0;z<2;z++) {
 unsigned w=sizes[z][0],h=sizes[z][1];size_t n=(size_t)w*h;
 uint32_t *a=malloc(n*4),*b=malloc(n*4),*tmp=malloc(n*4);unsigned repeats=z?4:40;
 for(unsigned run=0;run<5;run++) {
 for(size_t i=0;i<n;i++)a[i]=(uint32_t)(i*2654435761u)^0x81a63942u;
 memcpy(b,a,n*4);double t=now();
 for(unsigned j=0;j<repeats;j++)ref_agx_blur_1d(a,tmp,w,h,off,wt,6,j&1);
 double oldtime=now()-t;t=now();
 for(unsigned j=0;j<repeats;j++)agx_blur_1d(b,tmp,w,h,off,wt,6,j&1);
 double newtime=now()-t;
 if(memcmp(a,b,n*4))return 2;
 printf("%u %u %u %.9f %.9f %u\n",w,h,run,oldtime/repeats,newtime/repeats,b[n/2]);
 }
 free(a);free(b);free(tmp);
 }
 return 0;
}
"""


def helpers(source):
    return source[source.index('static inline uint32_t agx_bilinear_interpolate('):
                  source.index('static void agx_blur_texture(')]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', default='1966769', help='Git ref containing the general blur sampler')
    args = parser.parse_args()
    cc = shutil.which('cc')
    if not cc:
        parser.error('host C compiler required')
    baseline = subprocess.check_output(['git', 'show', args.baseline + ':backend/s5l8960x.c'], cwd=ROOT, text=True)
    current = (ROOT / 'backend/s5l8960x.c').read_text()
    with tempfile.TemporaryDirectory(prefix='agx-blur-bench-') as directory:
        folder = Path(directory)
        source = folder / 'bench.c'
        source.write_text(HEADER + helpers(baseline).replace('agx_', 'ref_agx_') + helpers(current) + DRIVER)
        subprocess.run([cc, '-O2', '-std=gnu11', str(source), '-lm', '-o', str(folder / 'bench')], check=True)
        result = subprocess.run([str(folder / 'bench')], capture_output=True, text=True, check=True)
    samples = []
    for line in result.stdout.splitlines():
        w, h, run, old, new, checksum = line.split()
        samples.append(dict(width=int(w), height=int(h), run=int(run), baseline_s=float(old), optimized_s=float(new)))
    summary = []
    for w, h in sorted({(s['width'], s['height']) for s in samples}):
        subset = [s for s in samples if (s['width'], s['height']) == (w, h)]
        old = statistics.median(s['baseline_s'] for s in subset)
        new = statistics.median(s['optimized_s'] for s in subset)
        summary.append(dict(width=w, height=h, baseline_ms=old*1000, optimized_ms=new*1000, speedup=old/new))
    print(json.dumps(dict(baseline=args.baseline, bit_exact=True, samples=samples, summary=summary), indent=2))


if __name__ == '__main__':
    main()
