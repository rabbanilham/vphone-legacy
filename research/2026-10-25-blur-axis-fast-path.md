# Axis-only blur sampling

The native renderer was sampled for five seconds during an unlock attempt. Of
2,602 render-thread samples, 1,481 were executing replay; 835 of those were under
`agx_blur_texture`. This identifies a useful CPU hotspot in that interval. It
neither measures guest FPS nor establishes the cause of intermittent failed
unlock attempts.

## Change and exactness

One-dimensional blur always samples at texel centres along its stationary axis.
Its general four-pixel bilinear interpolation therefore reduces to two pixels.
The fast path retains coordinate clamping and the existing 8-bit interpolation
weight, including half-up sample rounding before floating-point tap accumulation.
It interpolates BGRA in two pairs of packed 16-bit lanes. Each lane's weighted
sum is at most 65,280; adding the rounding bias of 128 cannot carry into another
lane. No kernel, tap extraction, blend, pass order, worker count or guest timing
is changed. General two-dimensional texture sampling keeps the existing path.

## Measurements

Run the reproducible host benchmark with:

```sh
.venv/bin/python scripts/benchmark_agx_blur.py --baseline 1966769
```

Both versions are extracted from production C and compiled together using `cc
-O2`. Five repetitions alternate horizontal and vertical six-tap passes and
compare the complete output buffers. Representative local medians:

| Image | Baseline per pass | Fast path per pass | Kernel speedup |
|---|---:|---:|---:|
| 608 × 64 | 2.333 ms | 0.835 ms | 2.79× |
| 640 × 1136 | 45.483 ms | 16.132 ms | 2.82× |

These measurements apply to the blur kernel. Three interleaved full-corpus
replays had median C-phase times of 1.007 s before and 0.990 s after; that small
difference is not strong evidence of an end-to-end speedup. Corpus replay also
loads dumps, manages the process and writes output. No native FPS claim is made.

## Validation

- The axis test compiles actual production functions under address/undefined
  behaviour sanitizers. It checks all 257 quantized interpolation weights with
  random independent channels against the unchanged general bilinear sampler.
- Complete horizontal and vertical passes match the general-sampler reference
  for one-pixel dimensions, edges, oversized offsets, and 1/4/6/7 tap kernels.
- All 265 chained corpus images match the frozen candidate byte for byte:
  1,949 draws, zero harness errors or skips.
- Full suite: 319 tests run, 22 skipped, remaining passed. Documentation links
  passed. All 28 synthetic AGX device contracts passed on the isolated backend.

The 104 approximate backdrop draws remain approximate. Native scene and
long-session acceptance must be evaluated separately; exact corpus agreement
is not proof of complete hardware emulation.
