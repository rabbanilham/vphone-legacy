"""Pure parsers and versioned performance comparison; no VM dependencies."""
import json
import math
import re
import statistics

SCHEMA_VERSION = 2
LIMITS = {'present_ms.p95': .15, 'input_ms.p50': .20,
          'cpu_pct.mean': .15, 'boot_host_s.p50': .15}


def distribution(values):
    values = sorted(float(v) for v in values if v is not None and math.isfinite(float(v)))
    if not values:
        return {'n': 0, 'mean': None, 'p50': None, 'p95': None, 'p99': None, 'max': None}
    return {'n': len(values), 'mean': statistics.mean(values),
            **{f'p{p}': values[max(0, math.ceil(len(values)*p/100)-1)] for p in (50, 95, 99)},
            'max': values[-1]}


def parse_line(line):
    for tag, kind in (('[pacing-json] ', 'pacing'), ('[player-stats-json] ', 'player')):
        if tag in line:
            try:
                value = json.loads(line.split(tag, 1)[1])
                return {**value, 'kind': kind} if isinstance(value, dict) else None
            except (ValueError, TypeError):
                return None
    if 'ANS perf:' in line:
        return {'kind': 'ans-lifetime', **{k: int(v) for k, v in
                re.findall(r'(\w+)=(\d+)', line.split('ANS perf:', 1)[1])}}
    match = re.search(r'\[pacing 5s\] (\S+) n=(\d+) interval_ms\(p50/p95/p99/max\)=(\d+(?:\.\d+)?/\d+(?:\.\d+)?/\d+(?:\.\d+)?/\d+(?:\.\d+)?)', line)
    if match:
        return {'kind': 'pacing-text', 'metric': match[1], 'n': int(match[2]),
                **dict(zip(('p50', 'p95', 'p99', 'max'), map(float, match[3].split('/'))))}
    if '[player-stats]' in line:
        match = re.search(r'present_ms\(p50/p95/p99/max\)[:=]\s*(\d+(?:\.\d+)?/\d+(?:\.\d+)?/\d+(?:\.\d+)?/\d+(?:\.\d+)?)', line)
        if match:
            return {'kind': 'present', **dict(zip(('p50', 'p95', 'p99', 'max'), map(float, match[1].split('/'))))}
    return None


def compare(candidate, baseline, noise=None):
    """Uncalibrated metrics are informational, never regression failures."""
    rows = []
    for key in sorted(set(candidate) | set(baseline) | set(LIMITS)):
        c, b = candidate.get(key), baseline.get(key)
        valid = (c is not None and b is not None and math.isfinite(c) and
                 math.isfinite(b) and c >= 0 and b >= 0)
        delta = (c / b - 1) if valid and b > 0 else (0 if valid and c == b else None)
        calibrated = noise is not None and key in noise
        limit = max(LIMITS.get(key, .15), 2 * noise[key]) if calibrated else None
        higher_better = key.endswith('fps.mean')
        regression = -delta if higher_better and delta is not None else delta
        status = ('unavailable' if delta is None else 'uncalibrated' if not calibrated else
                  'fail' if regression > limit + 1e-12 else 'pass')
        rows.append({'metric': key, 'base': b, 'candidate': c, 'delta': delta,
                     'limit': limit, 'status': status, 'failed': status == 'fail'})
    return rows


def aa_variation(runs):
    """Maximum relative deviation from run median; >=2 complete values required."""
    result = {}
    for key, row in aggregate(runs).items():
        if len(row['runs']) != len(runs) or len(runs) < 2:
            continue
        median = row['median']
        if median > 0:
            result[key] = max(abs(x / median - 1) for x in row['runs'])
        elif all(x == 0 for x in row['runs']):
            result[key] = 0
    return result


def aggregate(runs):
    keys = set().union(*(r['metrics'] for r in runs))
    result = {}
    for key in sorted(keys):
        values = [r['metrics'][key] for r in runs if r['metrics'].get(key) is not None
                  and math.isfinite(r['metrics'][key])]
        result[key] = {'median': statistics.median(values) if values else None,
                       'min': min(values) if values else None, 'max': max(values) if values else None,
                       'runs': values, 'available_runs': len(values), 'total_runs': len(runs)}
    return result


def pacing_distribution(windows, metric):
    """Merge histogram counts, never average percentiles (100 us upper bounds)."""
    bins = [0]*1024
    maximum = 0
    for row in windows:
        if metric in row.get('excluded_metrics', []):
            continue
        value = row.get('metrics', {}).get(metric, {})
        counts = value.get('bins_100us', [])
        if len(counts) != 1024:
            continue
        bins = [a+b for a,b in zip(bins, counts)]
        maximum = max(maximum, value.get('max', 0))
    n = sum(bins)
    result = {'n': n, 'max': maximum if n else None}
    for p in (50, 95, 99):
        cumulative = 0
        result[f'p{p}'] = None
        for b, count in enumerate(bins):
            cumulative += count
            if n and cumulative >= math.ceil(n*p/100):
                result[f'p{p}'] = maximum if b == 1023 else (b+1)/10
                break
    return result


def window_phase(start, end, markers, steady_start):
    """Only whole windows inside a marker interval enter per-step statistics."""
    if end <= steady_start:
        return 'boot'
    if start < steady_start:
        return 'boundary'
    crossed = [m for m in markers if start < m['host_s'] <= end]
    if crossed:
        return 'boundary'
    return next((m['name'] for m in reversed(markers) if m['host_s'] <= start), 'boot')


def summarize(windows, samples, inputs):
    metrics = {}
    for name in ('vblank', 'vblank-late', 'swap-complete', 'swap-request'):
        d = pacing_distribution(windows, name)
        for q in ('p95', 'p99', 'max'):
            metrics[f'{name}_ms.{q}'] = d[q]
        rows = [r['metrics'][name] for r in windows if r.get('kind') == 'pacing'
                and name not in r.get('excluded_metrics', [])
                and name in r.get('metrics', {}) and r['metrics'][name].get('n', 0)]
        metrics[f'{name}.jank_windows'] = sum(r['max'] > 50 for r in rows) if rows else None
    metrics['swap_request_ms.p95'] = metrics['swap-request_ms.p95']
    players = [r for r in windows if r.get('kind') == 'player']
    for field in ('fps', 'dropped'):
        metrics[f'player.{field}.mean'] = distribution(r.get(field) for r in players)['mean']
    for field in ('present_ms', 'recv_ms', 'update_ms', 'arrive_to_present_ms'):
        # No pooled percentile is possible from per-window quantiles.
        for q in ('p50', 'p95', 'p99', 'max'):
            values = [r.get(field, {}).get(q) for r in players
                      if r.get('sample_counts', {}).get(field, 0) > 0]
            metrics[f'player.{field}.window_{q}_max'] = distribution(values)['max']
    metrics['present_ms.p95'] = metrics['player.present_ms.window_p95_max']
    for proc in ('qemu', 'player'):
        rows = [r for r in samples if r['process'] == proc]
        for q in ('mean', 'p95'):
            metrics[f'{proc}.cpu_pct.{q}'] = distribution(r['cpu_pct'] for r in rows)[q]
        metrics[f'{proc}.rss_bytes.max'] = distribution(r['rss_bytes'] for r in rows)['max']
    cpu = [metrics[f'{p}.cpu_pct.mean'] for p in ('qemu', 'player')]
    metrics['cpu_pct.mean'] = sum(cpu) if all(x is not None for x in cpu) else None
    # Semantic screen/ROI validation is not established for this profile.
    metrics['input_ms.p50'] = distribution(r['latency_ms'] for r in inputs if r.get('valid') is True)['p50']
    ans = [r['ans'] for r in windows if r.get('ans')]
    for field in ('requests', 'fsyncs', 'background_fsync_bql_ns'):
        metrics[f'ans.{field}'] = sum(r[field] for r in ans) if ans else None
    hist = [sum(r['handler_bins_log2_ns'][i] for r in ans) for i in range(64)]
    for p in (50, 95):
        total, seen, value = sum(hist), 0, None
        for i, count in enumerate(hist):
            seen += count
            if total and seen >= math.ceil(total*p/100):
                value = 2**64-1 if i == 63 else 2**i-1
                break
        metrics[f'ans.handler_ns.p{p}_le'] = value
    metrics['ans.handler_ns.max'] = max((r['handler_max_ns'] for r in ans if r['requests'] > 0), default=None)
    return metrics


def clock_discontinuity(producer_elapsed, suite_elapsed):
    return producer_elapsed > 15 or abs(producer_elapsed-suite_elapsed) > 10
