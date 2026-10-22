#!/usr/bin/env python3
"""Run identical display probes and count respawns of the selected process.

Each run is a parallel_probe variant; the environment for the whole batch is
given with --env NAME=VALUE (repeatable). A run counts as a crash when hooks
attribute selected-process work to more than one pid. PID evidence alone
cannot establish that a process stayed alive after its last recorded hook.
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
            pid = identity.get('pid')
            if type(pid) is int and pid > 0:
                found.add(pid)
    return sorted(found)


def summarize_run(run, process):
    bb, sb = pids(run, 'backboardd'), pids(run, 'SpringBoard')
    selected = bb if process == 'backboardd' else sb
    return dict(run=run.name, backboardd=bb, springboard=sb,
                crashed=bool(selected and len(selected) > 1),
                observed=bool(selected))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out-root', type=Path, required=True)
    p.add_argument('--common', required=True, help='display_probe arguments shared by every run')
    p.add_argument('--runs', type=int, default=6)
    p.add_argument('--process', choices=('backboardd', 'SpringBoard'), default='backboardd')
    p.add_argument('--env', action='append', default=[], help='NAME=VALUE for every run')
    p.add_argument('--max-parallel', type=int, default=2)
    args = p.parse_args()
    if args.runs < 1:
        p.error('runs must be positive')
    env = dict(os.environ)
    for item in args.env:
        name, value = item.split('=', 1)
        env[name] = value
    cmd = [sys.executable, str(HERE / 'parallel_probe.py'), '--max-load', '8',
           '--max-parallel', str(args.max_parallel), '--out-root', str(args.out_root),
           '--common', args.common]
    for i in range(args.runs):
        cmd += ['--variant', f'r{i + 1}=']
    completed = subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.STDOUT)
    rows, crashes = [], 0
    for i in range(args.runs):
        run = args.out_root / f'r{i + 1}'
        row = summarize_run(run, args.process)
        crashes += row['crashed']
        rows.append(row)
    summary = dict(env=args.env, process=args.process, runs=args.runs, crashes=crashes,
                   unobserved_runs=sum(not row['observed'] for row in rows),
                   probe_exit_code=completed.returncode, rows=rows)
    args.out_root.mkdir(parents=True, exist_ok=True)
    (args.out_root / 'crash-batch.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))
    return int(bool(completed.returncode or crashes or summary['unobserved_runs']))


if __name__ == '__main__':
    sys.exit(main())
