#!/usr/bin/env python3
"""Run a batch of identical display probes and count backboardd respawns.

Each run is a parallel_probe variant; the environment for the whole batch is
given with --env NAME=VALUE (repeatable). A run counts as a crash when hooks
attribute backboardd work to more than one pid.
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def pids(run, process):
    try:
        events = json.loads((run / 'breakpoints.json').read_text())
    except (OSError, ValueError):
        return None
    found = set()
    for event in events:
        identity = event.get('identity') or event.get('caller_identity') or {}
        if isinstance(identity, dict) and identity.get('process') == process:
            found.add(identity.get('pid'))
    return sorted(found)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out-root', type=Path, required=True)
    p.add_argument('--common', required=True, help='display_probe arguments shared by every run')
    p.add_argument('--runs', type=int, default=6)
    p.add_argument('--env', action='append', default=[], help='NAME=VALUE for every run')
    p.add_argument('--max-parallel', type=int, default=2)
    args = p.parse_args()
    env = dict(os.environ)
    for item in args.env:
        name, value = item.split('=', 1)
        env[name] = value
    cmd = [sys.executable, str(HERE / 'parallel_probe.py'), '--max-load', '8',
           '--max-parallel', str(args.max_parallel), '--out-root', str(args.out_root),
           '--common', args.common]
    for i in range(args.runs):
        cmd += ['--variant', f'r{i + 1}=']
    subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.STDOUT)
    rows, crashes = [], 0
    for i in range(args.runs):
        run = args.out_root / f'r{i + 1}'
        bb, sb = pids(run, 'backboardd'), pids(run, 'SpringBoard')
        crashed = bb is not None and len(bb) > 1
        crashes += crashed
        rows.append(dict(run=run.name, backboardd=bb, springboard=sb, crashed=crashed))
    summary = dict(env=args.env, runs=args.runs, crashes=crashes, rows=rows)
    (args.out_root / 'crash-batch.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
