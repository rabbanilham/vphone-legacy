"""Run queued display_probe variants on independent disk copies and summarize them."""
import argparse
from collections import deque
import json
import math
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

from probe_cleanup import cleanup
from probe_summary import write_summary

HERE = Path(__file__).resolve().parent


def wait_for_load(max_load, load_wait):
    deadline = time.monotonic() + load_wait
    while True:
        load = os.getloadavg()[0]
        if load <= max_load:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(f'Aborting: host load {load:.2f} exceeds --max-load {max_load:g} '
                  f'after waiting {load_wait:g} seconds; no variants started.', file=sys.stderr)
            return False
        print(f'Host load {load:.2f} exceeds {max_load:g}; waiting '
              f'{min(10, remaining):g} seconds.', file=sys.stderr, flush=True)
        time.sleep(min(10, remaining))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out-root', type=Path, required=True)
    p.add_argument('--common', default='', help='Arguments shared by every variant')
    p.add_argument('--variant', action='append', required=True, help='name=extra display_probe arguments')
    p.add_argument('--max-load', type=float, default=6.0)
    p.add_argument('--load-wait', type=float, default=300)
    p.add_argument('--max-parallel', type=int, default=2)
    args = p.parse_args()
    if args.max_parallel < 1 or not math.isfinite(args.max_load) or args.max_load < 0 or not math.isfinite(args.load_wait) or args.load_wait < 0:
        p.error('max-parallel must be positive; max-load and load-wait must be finite and nonnegative')
    pending = deque()
    names = set()
    for item in args.variant:
        name, sep, extra = item.partition('=')
        if not sep or not name or name in ('.', '..') or Path(name).name != name or name in names:
            p.error('variants require unique simple directory names: name=arguments')
        names.add(name)
        pending.append((name, shlex.split(extra)))
    common = shlex.split(args.common)
    if not wait_for_load(args.max_load, args.load_wait):
        return 1
    args.out_root.mkdir(parents=True, exist_ok=True)
    running, summary = {}, {}
    while pending or running:
        while pending and len(running) < args.max_parallel:
            name, extra = pending.popleft()
            out = args.out_root / name
            cmd = [sys.executable, str(HERE / 'display_probe.py'), '--output', str(out), *common, *extra]
            with (args.out_root / f'{name}.log').open('wb') as log:
                proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
            running[name] = (proc, out, time.monotonic())
        finished = [(name, entry) for name, entry in running.items() if entry[0].poll() is not None]
        for name, (proc, out, started) in finished:
            code = proc.wait()
            record = dict(exit_code=code, wall_seconds=round(time.monotonic() - started, 1))
            observed = out / 'observations.json'
            if observed.exists():
                record.update(json.loads(observed.read_text()))
            # Preserve the subprocess result even if observations contain timing fields.
            record['exit_code'] = code
            out.mkdir(parents=True, exist_ok=True)
            try:
                result, table = write_summary(out, probe_exit_code=code)
                record['probe_summary'] = result
                print(f'\n{name}\n{table}', flush=True)
            except Exception as exc:
                record['summary_error'] = repr(exc)
                print(f'{name}: summary failed: {exc}', file=sys.stderr)
            if (out / 'process-exit.json').exists():
                try:
                    cleanup(out)
                    record['images_removed'] = True
                except Exception as exc:
                    record['cleanup_error'] = repr(exc)
            summary[name] = record
            del running[name]
            (args.out_root / 'parallel-summary.json').write_text(json.dumps(summary, indent=2) + '\n')
        if running and not finished:
            time.sleep(.1)
    return 0 if all(v['exit_code'] == 0 for v in summary.values()) else 1


if __name__ == '__main__':
    sys.exit(main())
