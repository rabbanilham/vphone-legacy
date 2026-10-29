#!/usr/bin/env python3
"""Repeatable real-player run. Never run concurrently with another live guest."""
import argparse
import hashlib
import json
import os
import signal
from pathlib import Path
import subprocess
import threading
import time
from datetime import datetime, timezone

from perf_common import ROOT, PROFILE, LiveLock, LiveSession, environment
from perf_metrics import SCHEMA_VERSION, distribution, parse_line, compare, aggregate, pacing_distribution
from input_latency import RFB, QMP, UNLOCK

# Relative virtual seconds, driven by backend pacing-json (5 second resolution).
# Coordinates from agx_corpus; QMP uses framebuffer y = script y + 10.
STEPS = [('unlock', 75, None), ('home-swipe-left', 110, ('swipe', 580, 60, 600)),
         ('home-swipe-right', 120, ('swipe', 60, 580, 600)),
         ('settings', 130, ('tap', 245, 815)), ('airplane', 145, ('tap', 560, 233)),
         ('home', 160, ('home',)), ('page-two', 170, ('swipe', 580, 60, 600)),
         ('extras', 180, ('tap', 92, 110)), ('done', 195, None)]


def touch(qmp, kind, x, y):
    events = [{'type': 'mtt', 'data': {'type': 'data', 'slot': 0, 'tracking-id': 0,
               'axis': axis, 'value': int(max(0, min(size-1, value))) * 32767 // size}}
              for axis, value, size in [('x', x, 640), ('y', y+10, 1136)]]
    events.append({'type': 'mtt', 'data': {'type': kind, 'slot': 0, 'tracking-id': 0,
                                        'axis': 'x', 'value': 0}})
    reply = qmp.execute('input-send-event', {'device': 'a7-touch-display', 'events': events})
    if isinstance(reply, dict) and 'class' in reply:
        raise RuntimeError(f'QMP touch failed: {reply}')


def action(qmp, spec):
    if spec[0] == 'home':
        for down in (True, False):
            qmp.execute('input-send-event', {'device': 'a7-touch-display', 'events': [
                {'type': 'key', 'data': {'down': down, 'key': {'type': 'qcode', 'data': 'home'}}}]})
            time.sleep(.08)
        return
    _, x, *rest = spec
    end, y = (x, rest[0]) if spec[0] == 'tap' else rest
    touch(qmp, 'begin', x, y)
    if spec[0] == 'swipe':
        for i in range(1, 25):
            time.sleep(.4/24)
            touch(qmp, 'update', x+(end-x)*i/24, y)
    else:
        time.sleep(.08)
    touch(qmp, 'end', end, y)


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def metadata(binary):
    digest = hashlib.sha256()
    with binary.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return {'git': command('git', 'rev-parse', 'HEAD'), 'dirty': bool(command('git', 'status', '--porcelain')),
            'qemu_binary': str(binary), 'qemu_sha256': digest.hexdigest(),
            'profile': str(PROFILE), 'plan_sha256': hashlib.sha256((PROFILE/'boot/boot_plan.json').read_bytes()).hexdigest(),
            'sw_vers': command('sw_vers'), 'hw.model': command('sysctl', '-n', 'hw.model'),
            'VPHONE_TCG_THREAD': os.environ.get('VPHONE_TCG_THREAD', 'multi'),
            'environment': {k: v for k, v in environment(UNLOCK).items() if k.startswith(('VPHONE_', 'QEMU_'))}}


def run_once(out, binary):
    records, samples, inputs, markers = [], [], [], []
    boot = None
    with LiveSession(out, binary, UNLOCK) as live:
        rfb = RFB(str(live.work / 'vnc'), rgb_only=True)
        rfb.handshake()
        reader = threading.Thread(target=rfb.loop, daemon=True)
        reader.start()
        qmp = QMP(str(live.work / 'qmp'))
        qmp.sock.settimeout(10)
        streams = [(out/name).open() for name in ('qemu-stderr.log', 'player-stderr.log')]
        buffers = ['', '']
        virtual = 0
        epoch = None
        next_step = 0
        next_sample = live.start
        current = 'boot'
        try:
            while next_step < len(STEPS):
                now = time.monotonic()
                if live.qemu.poll() is not None or live.player.poll() is not None:
                    raise RuntimeError('QEMU/player exited during measurement')
                if now-live.start > 420:
                    raise TimeoutError('virtual-time progress timeout')
                for i, stream in enumerate(streams):
                    buffers[i] += stream.read()
                    lines = buffers[i].split('\n')
                    buffers[i] = lines.pop()
                    for line in lines:
                        parsed = parse_line(line)
                        if parsed:
                            records.append({'host_s': now-live.start, 'step': current, **parsed})
                            if parsed['kind'] == 'pacing' and 'virtual_ns' in parsed:
                                if epoch is None:
                                    epoch = parsed.get('epoch_virtual_ns', 0)
                                virtual = (parsed['virtual_ns']-epoch)/1e9
                with rfb.lock:
                    changes = [t for t, c in rfb.updates if c >= 30]
                if boot is None and changes:
                    boot = {'host_s': changes[0]-live.start, 'guest_s': virtual,
                            'guest_s_bracket': [virtual, virtual+5],
                            'method': 'first update changing >=30 icon-area rows; lock-screen proxy',
                            'guest_resolution_s': 5}
                if now >= next_sample:
                    for name, proc in [('qemu', live.qemu), ('player', live.player)]:
                        raw = command('ps', '-p', str(proc.pid), '-o', '%cpu=,rss=')
                        if raw:
                            cpu, rss = map(float, raw.split())
                            samples.append({'host_s': now-live.start, 'guest_s': virtual, 'step': current,
                                            'process': name, 'cpu_pct': cpu, 'rss_bytes': rss*1024})
                    next_sample = now+1
                name, at, spec = STEPS[next_step]
                if virtual >= at:
                    current = name
                    markers.append({'name': name, 'target_guest_s': at, 'guest_s': virtual, 'host_s': now-live.start})
                    if spec:
                        sent = time.monotonic()
                        action(qmp, spec)
                        deadline = sent+3
                        first = None
                        while time.monotonic() < deadline:
                            with rfb.lock:
                                first = next((t for t, c in rfb.updates if c and t >= sent), None)
                            if first is not None:
                                break
                            time.sleep(.005)
                        inputs.append({'step': name, 'kind': spec[0], 'host_s': sent-live.start,
                                       'latency_ms': (first-sent)*1000 if first else None})
                    next_step += 1
                time.sleep(.02)
        finally:
            rfb.sock.shutdown(2)
            rfb.sock.close()
            reader.join(timeout=2)
            qmp.file.close()
            qmp.sock.close()
            for stream in streams:
                stream.close()
    per_step = {}
    for name in ['boot']+[s[0] for s in STEPS]:
        windows = [r for r in records if r['step'] == name]
        per_step[name] = {'pacing_ms': {m: pacing_distribution(windows, m)
                                       for m in ('vblank', 'swap-complete', 'vblank-late')},
                          'present_window_p95_ms': distribution(r['p95'] for r in windows if r['kind'] == 'present'),
                          'input_ms': distribution(r['latency_ms'] for r in inputs if r['step'] == name)}
        for proc in ('qemu', 'player'):
            rows = [r for r in samples if r['step'] == name and r['process'] == proc]
            per_step[name][proc] = {k: distribution(r[k] for r in rows) for k in ('cpu_pct', 'rss_bytes')}
    # Quantiles of reported window quantiles are labelled as such; no invented pooled percentile.
    presents = [r['p95'] for r in records if r['kind'] == 'present']
    latency = distribution(i['latency_ms'] for i in inputs if i['kind'] in ('tap', 'swipe'))
    metrics = {'present_ms.p95': max(presents) if presents else None, 'input_ms.p50': latency['p50'],
               'cpu_pct.mean': sum(distribution(r['cpu_pct'] for r in samples if r['process'] == p)['mean'] or 0
                                   for p in ('qemu', 'player')) if samples else None,
               'boot_host_s.p50': boot['host_s'] if boot else None}
    with rfb.lock:
        frame_times = [t for t, c in rfb.updates if c]
    frame_intervals = distribution((b-a)*1000 for a,b in zip(frame_times, frame_times[1:]))
    return {'metrics': metrics, 'boot': boot, 'markers': markers, 'windows': records,
            'input': inputs, 'input_distribution_ms': latency, 'changed_frame_interval_ms': frame_intervals, 'process_samples': samples,
            'per_step': per_step, 'peak_rss_bytes': {p: max((r['rss_bytes'] for r in samples if r['process']==p), default=None)
                                                 for p in ('qemu', 'player')}}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repeat', type=int, default=3)
    p.add_argument('--qemu-binary', type=Path, default=ROOT/'backend/bin/qemu-system-aarch64')
    p.add_argument('--out', type=Path)
    p.add_argument('--baseline')
    p.add_argument('--list-steps', action='store_true')
    args = p.parse_args()
    if args.list_steps:
        print(json.dumps(STEPS, indent=2))
        return 0
    if args.repeat < 1:
        p.error('--repeat must be positive')
    base = args.baseline
    if base == 'latest':
        candidates = []
        for path in (ROOT/'build/perf').glob('*/perf.json'):
            try:
                if json.loads(path.read_text()).get('status') == 'complete':
                    candidates.append(path)
            except (ValueError, OSError):
                continue
        candidates.sort(key=lambda x: x.stat().st_mtime)
        if not candidates:
            p.error('no baseline perf.json found')
        base = candidates[-1].parent
    baseline = json.loads((Path(base)/'perf.json').read_text()) if base else None
    if baseline and baseline.get('schema_version') != SCHEMA_VERSION:
        p.error('incompatible baseline schema')
    binary = args.qemu_binary.resolve()
    meta = metadata(binary)
    out = args.out or ROOT/'build/perf'/f"{meta['git'][:12]}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}"
    out.mkdir(parents=True, exist_ok=False)
    result = {'schema_version': SCHEMA_VERSION, 'metadata': meta, 'steps': STEPS, 'metric_notes': {
        'present_ms.p95': 'maximum reported player window p95, not pooled frame percentile',
        'cpu_pct.mean': 'sum of per-process mean ps %cpu samples',
        'peak_rss_bytes': 'sampled peak at 1 Hz, may miss short peaks',
        'guest_time': 'pacing-json virtual clock, sampled every 5 host seconds',
        'input': 'QMP send to first changed RFB icon-area row, may include unrelated animation',
        'boot': 'first meaningful framebuffer change proxy, not semantic lock-screen recognition',
        'swap_request_ms': 'unavailable: request sites outside authorized instrumentation region'}, 'runs': []}
    result['status'] = 'running'
    try:
        with LiveLock(out):
            for i in range(args.repeat):
                print(f'run {i+1}/{args.repeat}: {out}', flush=True)
                result['runs'].append(run_once(out/f'run-{i+1}', binary))
                result['aggregate'] = aggregate(result['runs'])
                (out/'perf.json').write_text(json.dumps(result, indent=2)+'\n')
    except BaseException as exc:
        result['status'] = 'failed'
        result['error'] = f'{type(exc).__name__}: {exc}'
        (out/'perf.json').write_text(json.dumps(result, indent=2)+'\n')
        (out/'summary.txt').write_text(result['error']+'\n')
        raise
    result['status'] = 'complete'
    summary = ['Performance suite schema 1', 'Metric                         median [min, max]']
    for key, value in result['aggregate'].items():
        summary.append(f"{key:30} {value['median']:.3f} [{value['min']:.3f}, {value['max']:.3f}]")
    failed = any(r['input_distribution_ms']['n'] != 6 or any(v is None for v in r['metrics'].values())
                 for r in result['runs'])
    summary.append('Data completeness: ' + ('FAIL' if failed else 'PASS'))
    if baseline:
        rows = compare({k:v['median'] for k,v in result['aggregate'].items()},
                       {k:v['median'] for k,v in baseline['aggregate'].items()})
        summary.append('Metric                         baseline candidate delta limit status')
        for r in rows:
            summary.append(f"{r['metric']:30} {r['base']} {r['candidate']} {r['delta']} {r['limit']} {'FAIL' if r['failed'] else 'PASS'}")
        failed = failed or any(r['failed'] for r in rows)
        result['comparison'] = rows
    (out/'perf.json').write_text(json.dumps(result, indent=2)+'\n')
    (out/'summary.txt').write_text('\n'.join(summary)+'\n')
    print('\n'.join(summary))
    return int(failed)


if __name__ == '__main__':
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f'signal {signum}')
    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(main())
