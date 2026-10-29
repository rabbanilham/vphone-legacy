"""Pure parsers and versioned performance comparison; no VM dependencies."""
import json
import math
import re
import statistics

SCHEMA_VERSION = 1
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
    if '[pacing-json] ' in line:
        try:
            return {'kind': 'pacing', **json.loads(line.split('[pacing-json] ', 1)[1])}
        except (ValueError, TypeError):
            return None
    match = re.search(r'\[pacing 5s\] (\S+) n=(\d+) interval_ms\(p50/p95/p99/max\)=(\d+(?:\.\d+)?/\d+(?:\.\d+)?/\d+(?:\.\d+)?/\d+(?:\.\d+)?)', line)
    if match:
        return {'kind': 'pacing-text', 'metric': match[1], 'n': int(match[2]),
                **dict(zip(('p50', 'p95', 'p99', 'max'), map(float, match[3].split('/'))))}
    if '[player-stats]' in line:
        match = re.search(r'present_ms\(p50/p95/p99/max\)[:=]\s*(\d+(?:\.\d+)?/\d+(?:\.\d+)?/\d+(?:\.\d+)?/\d+(?:\.\d+)?)', line)
        if match:
            return {'kind': 'present', **dict(zip(('p50', 'p95', 'p99', 'max'), map(float, match[1].split('/'))))}
    return None


def compare(candidate, baseline):
    rows = []
    for key, limit in LIMITS.items():
        c, b = candidate.get(key), baseline.get(key)
        valid = (c is not None and b is not None and math.isfinite(c) and
                 math.isfinite(b) and c >= 0 and b > 0)
        delta = (c / b - 1) if valid else None
        rows.append({'metric': key, 'base': b, 'candidate': c, 'delta': delta,
                     'limit': limit, 'failed': delta is None or delta > limit + 1e-12})
    return rows


def aggregate(runs):
    keys = set().union(*(r['metrics'] for r in runs))
    return {key: {'median': statistics.median(v), 'min': min(v), 'max': max(v), 'runs': v}
            for key in sorted(keys)
            if (v := [r['metrics'][key] for r in runs if r['metrics'].get(key) is not None])}


def pacing_distribution(windows, metric):
    """Merge histogram counts, never average percentiles (100 us upper bounds)."""
    bins = [0]*1024
    maximum = 0
    for row in windows:
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
