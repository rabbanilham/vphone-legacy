#!/usr/bin/env python3
"""AGX renderer benchmark: replay the corpus with a base and a candidate core.

  agx_bench.py [--base REV] [--scene NAME ...] [--jobs N]

Builds the offline harness (backend/agx_replay_host.c) twice: once around the
renderer core of backend/s5l8960x.c at git revision REV (default main) and
once around the working tree. Each scene is replayed as a chain (as
agx_corpus.py does) with both binaries; the harness reports the time spent in
agx_replay_frame. Outputs are compared pixel by pixel, so a speedup that
changes the image shows up as differing pixels and a maximum channel delta.
Exit status is 1 when any frame differs, unless --allow-diff is given.
"""

import argparse
import concurrent.futures as futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agx_corpus  # noqa: E402

ROOT = agx_corpus.ROOT


def build(core_src, out_dir):
    saved = agx_corpus.CORE_SRC
    agx_corpus.CORE_SRC = core_src
    try:
        return agx_corpus.build_harness(out_dir)
    finally:
        agx_corpus.CORE_SRC = saved


def run_chain(binary, dumps, prefix):
    result = subprocess.run([str(binary), '--chain', str(prefix), *map(str, dumps)],
                            capture_output=True, text=True)
    infos = [json.loads(line) for line in result.stdout.splitlines()]
    if len(infos) != len(dumps):
        raise RuntimeError(f'{binary.name}: {len(infos)}/{len(dumps)} frames; '
                           f'{result.stderr.strip()[-400:]}')
    return infos


def compare(a_path, b_path):
    a, b = Path(a_path).read_bytes(), Path(b_path).read_bytes()
    if a == b:
        return 0, 0
    if a[:12] != b[:12]:
        return -1, 255
    differing = 0
    worst = 0
    # Compare 4-byte pixels; memoryview slices keep this tolerable in Python.
    for i in range(12, len(a), 4):
        if a[i:i + 4] != b[i:i + 4]:
            differing += 1
            worst = max(worst, *(abs(a[i + k] - b[i + k]) for k in range(4)))
    return differing, worst


def scene(args, name, dumps, base_bin, cand_bin, work):
    rows = []
    best_base, best_cand = None, None
    for _ in range(args.repeat):
        b = run_chain(base_bin, dumps, work / f'base-{name}')
        c = run_chain(cand_bin, dumps, work / f'cand-{name}')
        best_base = b if best_base is None else [x if x['replay_ms'] <= y['replay_ms'] else y
                                                 for x, y in zip(best_base, b)]
        best_cand = c if best_cand is None else [x if x['replay_ms'] <= y['replay_ms'] else y
                                                 for x, y in zip(best_cand, c)]
    for i, (dump, b, c) in enumerate(zip(dumps, best_base, best_cand)):
        differing, worst = compare(work / f'base-{name}-{i}.rgba', work / f'cand-{name}-{i}.rgba')
        rows.append({'scene': name, 'frame': dump.stem, 'draws': c['draws'],
                     'base_ms': b['replay_ms'], 'cand_ms': c['replay_ms'],
                     'differing_px': differing, 'max_delta': worst})
    for i in range(len(dumps)):
        for side in ('base', 'cand'):
            (work / f'{side}-{name}-{i}.rgba').unlink(missing_ok=True)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--base', default='main', help='git revision of the base core')
    parser.add_argument('--corpus', type=Path, default=agx_corpus.CORPUS)
    parser.add_argument('--scene', action='append')
    parser.add_argument('--jobs', type=int, default=1)
    parser.add_argument('--repeat', type=int, default=1, help='keep the fastest of N runs')
    parser.add_argument('--json', type=Path, help='write per-frame rows here')
    parser.add_argument('--allow-diff', action='store_true')
    parser.add_argument('--max-slowdown', type=float, help='maximum total candidate/base timing ratio')
    args = parser.parse_args()
    if args.repeat < 1 or args.jobs < 1:
        parser.error('--repeat and --jobs must be positive')
    if args.max_slowdown is not None and (not __import__('math').isfinite(args.max_slowdown) or args.max_slowdown <= 0):
        parser.error('--max-slowdown must be finite and positive')

    with tempfile.TemporaryDirectory(prefix='agx-bench-') as tmp:
        work = Path(tmp)
        base_src = work / 'base-s5l8960x.c'
        base_src.write_text(subprocess.check_output(['git', 'show', f'{args.base}:backend/s5l8960x.c'],
                                                    cwd=ROOT, text=True))
        base_bin = build(base_src, args.corpus / '.harness-base')
        cand_bin = build(agx_corpus.CORE_SRC, args.corpus / '.harness')
        scenes = {}
        for d in sorted(p for p in args.corpus.iterdir() if (p / 'dumps').is_dir()):
            if args.scene and d.name not in args.scene:
                continue
            scenes[d.name] = sorted((d / 'dumps').glob('frame-*.bin'), key=lambda p: int(p.stem.split('-')[1]))
        with futures.ThreadPoolExecutor(args.jobs) as pool:
            jobs = [pool.submit(scene, args, n, ds, base_bin, cand_bin, work) for n, ds in scenes.items()]
            rows = [r for j in jobs for r in j.result()]

    total_b = sum(r['base_ms'] for r in rows)
    total_c = sum(r['cand_ms'] for r in rows)
    diff = [r for r in rows if r['differing_px']]
    print(f'{"scene":14} {"frames":>6} {"base ms":>10} {"cand ms":>10} {"speedup":>8} {"diff":>5}')
    for name in scenes:
        rs = [r for r in rows if r['scene'] == name]
        b, c = sum(r['base_ms'] for r in rs), sum(r['cand_ms'] for r in rs)
        print(f'{name:14} {len(rs):6} {b:10.1f} {c:10.1f} {b / max(c, 1e-6):7.2f}x '
              f'{sum(1 for r in rs if r["differing_px"]):5}')
    print(f'{"total":14} {len(rows):6} {total_b:10.1f} {total_c:10.1f} {total_b / max(total_c, 1e-6):7.2f}x {len(diff):5}')
    slow = sorted(rows, key=lambda r: -r['cand_ms'])[:5]
    print('slowest candidate frames: ' + ', '.join(f'{r["scene"]}/{r["frame"]} {r["cand_ms"]:.1f} ms' for r in slow))
    for r in diff[:20]:
        print(f'  DIFF {r["scene"]}/{r["frame"]}: {r["differing_px"]} px, max delta {r["max_delta"]}')
    if args.json:
        args.json.write_text(json.dumps(rows, indent=1) + '\n')
    slowdown = total_c / total_b if total_b > 0 else float('inf')
    timing_failed = args.max_slowdown is not None and (not rows or slowdown > args.max_slowdown)
    if args.max_slowdown is not None:
        print(f'total candidate/base: {slowdown:.3f}; limit: {args.max_slowdown:.3f}')
    return int(bool(diff and not args.allow_diff) or timing_failed)


if __name__ == '__main__':
    sys.exit(main())
