#!/usr/bin/env python3
"""Measure player and QEMU live performance at Lock screen (t=90s) and Home (t=115s)."""
import os
import sys
import time
import shutil
import tempfile
import plistlib
import platform
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from display_probe import FB  # noqa: E402
import live_run  # noqa: E402

from perf_common import LiveLock, prepare, environment, stop

_measure_lock = None

def acquire_lock(out_dir):
    global _measure_lock
    _measure_lock = LiveLock(out_dir).acquire()
    print(f'[lock] Acquired {_measure_lock.path}')

def release_lock():
    _measure_lock.release()
    print(f'[lock] Released {_measure_lock.path}')

def main():
    out_dir = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / 'build/perf/perf-gpu-present/baseline-player'
    out_dir.mkdir(parents=True, exist_ok=True)

    acquire_lock(out_dir)
    try:
        run_measurement(out_dir)
    finally:
        release_lock()

def run_measurement(out_dir):
    player = live_run.build_player()
    work = Path(tempfile.mkdtemp(prefix='vphone-live-measure-', dir='/tmp'))
    storage = ROOT / 'build/ios712-validated'
    cmd = prepare(work, storage=storage if storage.exists() else None)
    print(f'[measure] Work dir: {work}')
    touch_script_path = ROOT / 'research/evidence/H-home-live/awake-touch-script.txt'
    env = environment(touch_script_path.read_text())

    qemu_log = (work / 'qemu.log').open('wb')
    player_stderr = (out_dir / 'player-stderr.log').open('wb')

    qemu = subprocess.Popen(cmd, env=env, stdout=qemu_log, stderr=subprocess.STDOUT)
    viewer = None

    try:
        deadline = time.monotonic() + 25
        while not (work / 'vnc').exists():
            if qemu.poll() is not None:
                raise RuntimeError(f"QEMU exited prematurely: {(work / 'qemu.log').read_text()}")
            if time.monotonic() > deadline:
                raise RuntimeError("QEMU did not create VNC socket")
            time.sleep(0.05)

        viewer = subprocess.Popen([str(player), str(work / 'vnc'), str(work / 'qmp')],
                                  env=env, stderr=player_stderr)
        print(f"[measure] QEMU PID={qemu.pid}, Viewer PID={viewer.pid}")

        start_time = time.monotonic()

        # Phase 1: wait until t=90s (Lock Screen)
        target_lock = 90
        now = time.monotonic() - start_time
        if target_lock > now:
            time.sleep(target_lock - now)

        print("[measure] Starting sample at Lock Screen (t=90s)...")
        # Sample both QEMU and viewer for 10s
        p_sample_qemu = subprocess.Popen(['sample', str(qemu.pid), '10', '-file', str(out_dir / 'sample-lock-qemu.txt')])
        p_sample_view = subprocess.Popen(['sample', str(viewer.pid), '10', '-file', str(out_dir / 'sample-lock-player.txt')])

        time.sleep(5)
        # Record thread state midway through sample
        with (out_dir / 'threads-lock-qemu.txt').open('w') as f:
            subprocess.run(['ps', '-M', '-p', str(qemu.pid), '-o', 'user,pid,%cpu,%mem,time'], stdout=f)
        with (out_dir / 'threads-lock-player.txt').open('w') as f:
            subprocess.run(['ps', '-M', '-p', str(viewer.pid), '-o', 'user,pid,%cpu,%mem,time'], stdout=f)

        p_sample_qemu.wait()
        p_sample_view.wait()
        print("[measure] Completed Lock Screen sample.")

        # Phase 2: wait until t=115s (Home Screen, unlock happens at ~95s)
        target_home = 115
        now = time.monotonic() - start_time
        if target_home > now:
            time.sleep(target_home - now)

        print("[measure] Starting sample at Home Screen (t=115s)...")
        p_sample_qemu = subprocess.Popen(['sample', str(qemu.pid), '10', '-file', str(out_dir / 'sample-home-qemu.txt')])
        p_sample_view = subprocess.Popen(['sample', str(viewer.pid), '10', '-file', str(out_dir / 'sample-home-player.txt')])

        time.sleep(5)
        with (out_dir / 'threads-home-qemu.txt').open('w') as f:
            subprocess.run(['ps', '-M', '-p', str(qemu.pid), '-o', 'user,pid,%cpu,%mem,time'], stdout=f)
        with (out_dir / 'threads-home-player.txt').open('w') as f:
            subprocess.run(['ps', '-M', '-p', str(viewer.pid), '-o', 'user,pid,%cpu,%mem,time'], stdout=f)

        p_sample_qemu.wait()
        p_sample_view.wait()
        print("[measure] Completed Home Screen sample.")

        time.sleep(5)

    finally:
        stop(viewer)
        stop(qemu)
        qemu_log.close()
        player_stderr.close()

        # Copy logs to out_dir
        if (work / 'player-input.log').exists():
            shutil.copyfile(work / 'player-input.log', out_dir / 'player-input.log')
        if (work / 'serial.log').exists():
            shutil.copyfile(work / 'serial.log', out_dir / 'serial.log')
        if (work / 'qemu.log').exists():
            shutil.copyfile(work / 'qemu.log', out_dir / 'qemu.log')

        shutil.rmtree(work, ignore_errors=True)

    # Summarize stats
    print(f"[measure] Done. Generating summary at {out_dir / 'summary.txt'}...")
    stats_lines = []
    if (out_dir / 'player-stderr.log').exists():
        for line in (out_dir / 'player-stderr.log').read_text().splitlines():
            if '[player-stats]' in line:
                stats_lines.append(line)

    with (out_dir / 'summary.txt').open('w') as f:
        f.write(f"Measurement summary for {out_dir.name}\n")
        f.write("=" * 60 + "\n\n")
        f.write("--- Player FPS / Stats ---\n")
        for line in stats_lines:
            f.write(line + "\n")
        f.write("\n--- Lock Screen QEMU Threads ---\n")
        if (out_dir / 'threads-lock-qemu.txt').exists():
            f.write((out_dir / 'threads-lock-qemu.txt').read_text() + "\n")
        f.write("--- Lock Screen Player Threads ---\n")
        if (out_dir / 'threads-lock-player.txt').exists():
            f.write((out_dir / 'threads-lock-player.txt').read_text() + "\n")
        f.write("--- Home Screen QEMU Threads ---\n")
        if (out_dir / 'threads-home-qemu.txt').exists():
            f.write((out_dir / 'threads-home-qemu.txt').read_text() + "\n")
        f.write("--- Home Screen Player Threads ---\n")
        if (out_dir / 'threads-home-player.txt').exists():
            f.write((out_dir / 'threads-home-player.txt').read_text() + "\n")

    print((out_dir / 'summary.txt').read_text())

if __name__ == '__main__':
    main()
