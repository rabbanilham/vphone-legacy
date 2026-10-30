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

from perf_common import ROOT, PROFILE, LiveLock, LiveSession, environment, host_preflight
from perf_metrics import SCHEMA_VERSION, distribution, parse_line, compare, aggregate, pacing_distribution, aa_variation, summarize, window_phase, clock_discontinuity
from input_latency import RFB, QMP, UNLOCK

# Relative virtual seconds, driven by backend pacing-json (5 second resolution).
# Coordinates from agx_corpus; QMP uses framebuffer y = script y + 10.
STEPS = [('unlock', 75, None), ('home-swipe-left', 110, ('swipe', 580, 60, 600)),
         ('home-swipe-right', 120, ('swipe', 60, 580, 600)),
         ('settings', 130, ('tap', 245, 815)), ('airplane', 145, ('tap', 560, 233)),
         ('home', 160, ('home',)), ('page-two', 170, ('swipe', 580, 60, 600)),
         ('extras', 180, ('tap', 92, 110)), ('done', 195, None)]


# Definition only: no semantic precondition/causality validator is enabled yet.
# ROI coordinates are framebuffer pixels; all exclude status clock/dock.
INPUT_CONTRACTS = {
    'settings': {'event': 'touch begin', 'roi': [40, 130, 600, 900], 'precondition': 'Home; Settings icon identified'},
    'airplane': {'event': 'touch begin', 'roi': [480, 205, 620, 285], 'precondition': 'Settings; switch identified'},
    'extras': {'event': 'touch begin', 'roi': [30, 170, 610, 900], 'precondition': 'Home page two; Extras folder identified'},
    **{name: {'event': 'first touch move', 'roi': [0, 120, 640, 900],
              'precondition': 'Home page identified; ROI quiet before event'}
       for name in ('home-swipe-left', 'home-swipe-right', 'page-two')},
}


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
    load_start = host_preflight()
    boot = None
    with LiveSession(out, binary, UNLOCK) as live:
        rfb = RFB(str(live.work / 'vnc'), rgb_only=True)
        rfb.handshake()
        reader = threading.Thread(target=rfb.loop, daemon=True)
        reader.start()
        qmp = QMP(str(live.work / 'qmp'))
        qmp.sock.settimeout(10)
        # Trace duplicates stderr; use it as a fallback without double counting.
        paths = [out/'qemu-stderr.log', out/'player-stderr.log', live.work/'player-input.log']
        streams, buffers, seen = {}, {}, set()
        virtual, next_step, next_sample = 0, 0, live.start
        last_poll = time.monotonic()
        previous_pacing = None
        previous_pacing_receipt = None
        clock_offsets = {}
        last_player_time = live.start
        try:
            while next_step < len(STEPS):
                now = time.monotonic()
                if now-last_poll > 15:
                    raise RuntimeError('host/suite stalled >15s; run invalid (sleep or suspension)')
                last_poll = now
                if live.qemu.poll() is not None or live.player.poll() is not None:
                    raise RuntimeError('QEMU/player exited during measurement')
                if now-live.start > 420:
                    raise TimeoutError('virtual-time progress timeout')
                for path in paths:
                    if path not in streams and path.exists():
                        streams[path], buffers[path] = path.open(), ''
                    if path not in streams:
                        continue
                    buffers[path] += streams[path].read()
                    lines = buffers[path].split('\n')
                    buffers[path] = lines.pop()
                    for line in lines:
                        parsed = parse_line(line)
                        if not parsed or parsed['kind'] not in ('pacing', 'player', 'ans-lifetime'):
                            continue
                        signature = json.dumps(parsed, sort_keys=True)
                        if parsed['kind'] == 'player':
                            if signature in seen:
                                continue
                            seen.add(signature)
                            last_player_time = now
                        stamp = parsed.get('host_ns', 0)/1e9 if parsed['kind'] == 'pacing' else parsed.get('host_uptime_s')
                        if stamp is not None:
                            clock_offsets.setdefault(parsed['kind'], stamp-(now-live.start))
                        offset = clock_offsets.get(parsed['kind'], 0)
                        end = stamp-offset if stamp is not None else now-live.start
                        start = (parsed['window_start_host_ns']/1e9-offset
                                 if parsed['kind'] == 'pacing' and 'window_start_host_ns' in parsed
                                 else previous_pacing['host_ns']/1e9-offset
                                 if parsed['kind'] == 'pacing' and previous_pacing is not None
                                 else 0 if parsed['kind'] == 'pacing'
                                 else end-parsed.get('window_s', 1))
                        records.append({'host_s': end, 'window_start_s': start,
                                        'clock_offset_s': offset, 'receipt_host_s': now-live.start,
                                        'source': path.name, 'phase': 'boot' if virtual < 105 else 'steady', **parsed})
                        if parsed['kind'] == 'pacing':
                            virtual = parsed['virtual_ns']/1e9
                            if previous_pacing is not None:
                                gap = (parsed['host_ns']-previous_pacing['host_ns'])/1e9
                                receipt_gap = now-previous_pacing_receipt
                                if clock_discontinuity(gap, receipt_gap):
                                    raise RuntimeError(f'pacing clock/report gap {gap:.1f}s vs suite {receipt_gap:.1f}s; run invalid')
                            previous_pacing = parsed
                            previous_pacing_receipt = now
                with rfb.lock:
                    changes = [t for t, c in rfb.updates if c >= 30]
                if boot is None and changes:
                    boot = {'host_s': changes[0]-live.start, 'guest_s': virtual,
                            'method': 'first >=30 changed icon-area rows; framebuffer proxy'}
                if now >= next_sample:
                    for name, proc in [('qemu', live.qemu), ('player', live.player)]:
                        raw = command('ps', '-p', str(proc.pid), '-o', '%cpu=,rss=')
                        if raw:
                            cpu, rss = map(float, raw.split())
                            samples.append({'host_s': now-live.start, 'guest_s': virtual,
                                            'process': name, 'cpu_pct': cpu, 'rss_bytes': rss*1024})
                    next_sample = now+1
                # A still screen sends no frames, so stats lines are sparse once the
                # guest is idle; only a long silence indicates a dead player.
                if virtual >= 110 and now-last_player_time > 75:
                    raise RuntimeError('no player receive stats for >75s; check player-input.log/window/display')
                name, at, spec = STEPS[next_step]
                if virtual >= at:
                    if virtual-at > 10:
                        raise RuntimeError('missed gesture schedule; refusing to dispatch catch-up actions')
                    live.foreground()
                    markers.append({'name': name, 'target_guest_s': at, 'guest_s': virtual,
                                    'host_s': time.monotonic()-live.start})
                    if spec:
                        ready = qmp.touch_ready()
                        sent = time.monotonic()
                        if ready or spec[0] == 'home':
                            action(qmp, spec)
                        inputs.append({'step': name, 'kind': spec[0], 'host_s': sent-live.start,
                                       'event_host_s': None, 'latency_ms': None, 'valid': False, 'touch_ready': ready,
                                       'contract': INPUT_CONTRACTS.get(name),
                                       'reason': 'screen/ROI causality unvalidated' if ready else 'digitizer gated'})
                    next_step += 1
                time.sleep(.02)
        finally:
            rfb.sock.shutdown(2)
            rfb.sock.close()
            reader.join(timeout=2)
            qmp.file.close()
            qmp.sock.close()
            for stream in streams.values():
                stream.close()
            # Save diagnostics even when clock/player validation aborts the run.
            (out/'measurement-raw.json').write_text(json.dumps({'windows': records, 'input': inputs,
                'markers': markers, 'process_samples': samples, 'load_start': load_start,
                'load_end': list(os.getloadavg())}, indent=2)+'\n')
    # Unlock script ends at virtual 103s. Require a whole post-unlock window.
    steady_start = next((r['host_s'] for r in records if r['kind'] == 'pacing'
                         and r['virtual_ns']/1e9 >= 105), float('inf'))
    for row in records:
        row['step'] = window_phase(row['window_start_s'], row['host_s'], markers, steady_start)
        row['phase'] = 'boot' if row['host_s'] <= steady_start else 'steady' if row['step'] != 'boundary' else 'boundary'
        if row['kind'] == 'pacing' and row['phase'] == 'steady':
            row['excluded_metrics'] = []
            for metric, data in row['metrics'].items():
                start = data.get('first_start_host_ns')
                # Legacy binaries: the first interval begins no earlier than
                # window start minus its observed maximum. Conservative bound.
                interval_start = (start/1e9-row['clock_offset_s'] if start is not None else
                                  row['window_start_s']-data.get('max', 0)/1000)
                if window_phase(interval_start, row['host_s'], markers, steady_start) != row['step']:
                    row['excluded_metrics'].append(metric)
        if row['kind'] == 'ans-lifetime':
            row['phase'] = 'lifetime'
    for row in samples:
        row['step'] = window_phase(row['host_s'], row['host_s'], markers, steady_start)
    per_step = {}
    for name in ['boot', 'boundary']+[s[0] for s in STEPS]:
        per_step[name] = {'metrics': summarize([r for r in records if r['step'] == name],
            [r for r in samples if r['step'] == name], [r for r in inputs if r['step'] == name]),
            'input_valid_n': sum(r['valid'] for r in inputs if r['step'] == name),
            'input_attempted_n': sum(r['step'] == name for r in inputs)}
    steady = [r for r in records if r['phase'] == 'steady']
    metrics = summarize(steady, [r for r in samples if r['host_s'] >= steady_start], inputs)
    for name, row in per_step.items():
        if name not in ('boot', 'boundary', 'unlock'):
            metrics.update({f'step.{name}.{key}': value for key, value in row['metrics'].items()})
    metrics['boot_host_s.p50'] = boot['host_s'] if boot else None
    issues = []
    if not any(r.get('sample_counts', {}).get('present_ms', 0) > 0 for r in steady):
        issues.append('no measured player presentation intervals in steady state')
    if not any(r.get('fps', 0) > 1 for r in steady):
        issues.append('no active player receive window (fps>1) in steady state')
    return {'metrics': metrics, 'boot': boot, 'markers': markers, 'windows': records,
            'peak_rss_bytes': {p: metrics[f'{p}.rss_bytes.max'] for p in ('qemu', 'player')},
            'input': inputs, 'input_distribution_ms': distribution([]), 'process_samples': samples,
            'per_step': per_step, 'quality_issues': issues, 'steady_start_host_s': steady_start,
            'load_start': load_start, 'load_end': list(os.getloadavg())}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repeat', type=int, default=3)
    p.add_argument('--qemu-binary', type=Path, default=ROOT/'backend/bin/qemu-system-aarch64')
    p.add_argument('--out', type=Path)
    p.add_argument('--baseline')
    p.add_argument('--self-test', action='store_true', help='A/A: same binary, >=2 sequential runs')
    p.add_argument('--list-steps', action='store_true')
    args = p.parse_args()
    if args.list_steps:
        print(json.dumps(STEPS, indent=2))
        return 0
    if args.repeat < 1:
        p.error('--repeat must be positive')
    if args.self_test:
        args.repeat = max(2, args.repeat)
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
    if baseline and baseline.get('status') != 'complete':
        p.error('baseline is incomplete or invalid')
    if baseline and baseline.get('schema_version') != SCHEMA_VERSION:
        p.error('incompatible baseline schema')
    binary = args.qemu_binary.resolve()
    host_preflight()
    meta = metadata(binary)
    out = args.out or ROOT/'build/perf'/f"{meta['git'][:12]}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}"
    out.mkdir(parents=True, exist_ok=False)
    result = {'schema_version': SCHEMA_VERSION, 'metadata': meta, 'steps': STEPS, 'metric_notes': {
        'present_ms.p95': 'maximum reported player window p95, not pooled frame percentile',
        'cpu_pct.mean': 'sum of per-process mean ps %cpu samples',
        'peak_rss_bytes': 'sampled peak at 1 Hz, may miss short peaks',
        'player_fps': 'receive throughput, not display presentation FPS',
        'unavailable': 'null metrics retain zero available_runs; optional missing data is not a regression pass',
        'guest_time': 'pacing-json virtual clock, sampled every 5 host seconds',
        'input': 'unavailable until semantic precondition and action-specific ROI causality are validated; valid n=0',
        'boot': 'first meaningful framebuffer change proxy, not semantic lock-screen recognition',
        'swap_request_ms': 'oldest pending DISP0 request to modeled completion, host clock; histogram upper bounds'}, 'runs': []}
    result['mode'] = 'A/A' if args.self_test else 'candidate'
    result['load_start'] = list(os.getloadavg())
    result['status'] = 'running'
    try:
        with LiveLock(out):
            for i in range(args.repeat):
                print(f'run {i+1}/{args.repeat}: {out}', flush=True)
                result['runs'].append(run_once(out/f'run-{i+1}', binary))
                result['aggregate'] = aggregate(result['runs'])
                (out/'perf.json').write_text(json.dumps(result, indent=2)+'\n')
    except BaseException as exc:
        result['load_end'] = list(os.getloadavg())
        result['status'] = 'failed'
        result['error'] = f'{type(exc).__name__}: {exc}'
        (out/'perf.json').write_text(json.dumps(result, indent=2)+'\n')
        (out/'summary.txt').write_text(result['error']+'\n')
        raise
    result['status'] = 'complete'
    result['load_end'] = list(os.getloadavg())
    if args.self_test:
        result['aa_noise'] = aa_variation(result['runs'])
    summary = ['Performance suite schema 2', 'Metric                         median [min, max]']
    for key, value in result['aggregate'].items():
        if value['median'] is None:
            summary.append(f'{key:30} unavailable (0/{value["total_runs"]} runs)')
            continue
        summary.append(f"{key:30} {value['median']:.3f} [{value['min']:.3f}, {value['max']:.3f}]")
    failed = any(r['quality_issues'] for r in result['runs'])
    result['quality_issues'] = [issue for r in result['runs'] for issue in r['quality_issues']]
    if failed:
        result['status'] = 'invalid'
        result.pop('aa_noise', None)
    summary.extend(result['quality_issues'])
    summary.append('Player data validity: ' + ('FAIL' if failed else 'PASS'))
    summary.append('Input latency: unavailable; semantic/ROI validation pending, valid samples=0')
    if args.self_test:
        summary.append('A/A relative median deviations: '+json.dumps(result.get('aa_noise', {}), sort_keys=True))
    if baseline:
        noise = baseline.get('aa_noise')
        if noise is None and baseline.get('metadata', {}).get('qemu_sha256') == meta['qemu_sha256']:
            noise = aa_variation(baseline['runs'])
        rows = compare({k:v['median'] if v['available_runs'] == v['total_runs'] else None
                        for k,v in result['aggregate'].items()},
                       {k:v['median'] if v['available_runs'] == v['total_runs'] else None
                        for k,v in baseline['aggregate'].items()},
                       noise)
        summary.append('Metric                         baseline candidate delta limit status')
        for r in rows:
            summary.append(f"{r['metric']:30} {r['base']} {r['candidate']} {r['delta']} {r['limit']} {r['status']}")
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
