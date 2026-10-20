#!/usr/bin/env python3
"""Summarize a display probe directory without loading large trace logs into RAM.

Exit code comes from process-exit.json (QEMU); parallel_probe additionally records
its display_probe subprocess exit code as probe_exit_code. Missing data is null.
MMIO counts and distinct values describe recorded writes (in first-seen order),
not all guest writes when old traces lack FIFO instrumentation. Monitor snapshots
are reported separately and never treated as writes.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import re


def ppm_nonzero(path):
    """Count nonzero raster bytes in a binary P6 PPM, excluding its header."""
    with path.open('rb') as stream:
        def token():
            value = bytearray()
            while True:
                ch = stream.read(1)
                if not ch:
                    raise ValueError('Truncated PPM header')
                if ch == b'#':
                    stream.readline()
                elif not ch.isspace():
                    break
            while ch and not ch.isspace():
                value.extend(ch)
                ch = stream.read(1)
            if ch == b'\r' and stream.peek(1)[:1] == b'\n':
                stream.read(1)
            return bytes(value)
        if token() != b'P6':
            raise ValueError('Expected binary P6 framebuffer')
        width, height, maximum = (int(token()) for _ in range(3))
        if width <= 0 or height <= 0 or not 0 < maximum <= 65535:
            raise ValueError('Invalid PPM dimensions or maximum')
        remaining = width * height * 3 * (2 if maximum > 255 else 1)
        count = 0
        while remaining:
            chunk = stream.read(min(remaining, 1024 * 1024))
            if not chunk:
                raise ValueError('Truncated PPM raster')
            count += len(chunk) - chunk.count(0)
            remaining -= len(chunk)
        return count


def summarize(root, probe_exit_code=None):
    root = Path(root)
    hits, kinds = Counter(), Counter()
    panics = []
    hangs = 0
    mmio = dict.fromkeys(('0x206204044', '0x206204070'))
    writes = {pa: [] for pa in mmio}
    legacy = {pa: [] for pa in mmio}
    snapshots = dict(mmio)
    bp = root / 'breakpoints.json'
    if bp.exists():
        hits.update(event['name'] for event in json.loads(bp.read_text()))
    serial = root / 'serial.log'
    if serial.exists():
        with serial.open(errors='replace') as stream:
            for line in stream:
                if re.search(r'\bpanic\b', line, re.I):
                    panics.append(line.rstrip())
                hangs += line.count('GPU hang')
    trace = root / 'qemu-trace.log'
    if trace.exists():
        with trace.open(errors='replace') as stream:
            for line in stream:
                meta = re.search(r'AGX META:\s*(.*)', line)
                if meta:
                    # Normalize dynamic values while retaining meaningful message kinds.
                    message = meta[1]
                    kind = ('queue stamp' if re.match(r'queue .* stamp ', message) else
                            'queue cmd' if re.match(r'queue .* cmd ', message) else
                            'host ring' if message.startswith('host ring ') else
                            'consumed' if message.startswith('consumed ') else
                            re.split(r'\s+(?:0x[\da-fA-F]+|\d+|\()', message, maxsplit=1)[0].strip())
                    kinds[kind] += 1
                if 'DISPLAY_MMIO' in line or 'DISPLAY_FIFO' in line:
                    fields = dict(re.findall(r'(\w+)=([^\s]+)', line))
                    if fields.get('op') == 'write' and fields.get('pa') in mmio:
                        writes[fields['pa']].append(hex(int(fields['value'], 0)))
    # Older FIFO writes only appeared on stderr; prefer structured tracing per
    # register so a write present in both logs is not counted twice.
    for name in ('qemu-trace.log', 'stderr.log'):
        path = root / name
        if path.exists():
            with path.open(errors='replace') as stream:
                for line in stream:
                    match = re.search(r'\[DISP0\] layer0 base off=0x4044 val=(0x[\da-fA-F]+)', line)
                    if match:
                        legacy['0x206204044'].append(hex(int(match[1], 16)))
    counts, distinct = {}, {}
    for pa in mmio:
        values = writes[pa] or legacy[pa]
        mmio[pa] = values[-1] if values else None
        counts[pa] = len(values)
        distinct[pa] = list(dict.fromkeys(values))
    dump = root / 'mmio_registers.json'
    if dump.exists():
        # QMP xp /Nxw dumps: each line starts with a physical address,
        # followed by consecutive 32-bit words. These are reads, not writes.
        for view in json.loads(dump.read_text()).values():
            for line in view.splitlines():
                match = re.match(r'\s*(?:0x)?([\da-fA-F]+):\s*(.*)', line)
                if match:
                    base = int(match[1], 16)
                    for i, value in enumerate(re.findall(r'0x[\da-fA-F]+', match[2])):
                        pa = hex(base + 4 * i)
                        if pa in snapshots:
                            snapshots[pa] = hex(int(value, 16))
    exited = root / 'process-exit.json'
    result = dict(breakpoint_hits=dict(sorted(hits.items())), panic_lines=panics,
                  gpu_hang_count=hangs, display_mmio_last_writes=mmio,
                  display_mmio_write_counts=counts,
                  display_mmio_distinct_write_values=distinct,
                  display_mmio_snapshots=snapshots,
                  agx_meta_counts=dict(sorted(kinds.items())),
                  framebuffer_nonzero_bytes=ppm_nonzero(root / 'framebuffer.ppm')
                  if (root / 'framebuffer.ppm').exists() else None,
                  exit_code=json.loads(exited.read_text())['exit_code'] if exited.exists() else None)
    if probe_exit_code is not None:
        result['probe_exit_code'] = probe_exit_code
    return result


def format_table(result):
    rows = [('QEMU exit', result['exit_code']), ('Probe exit', result.get('probe_exit_code')),
            ('Panic lines', len(result['panic_lines'])), ('GPU hangs', result['gpu_hang_count']),
            ('Framebuffer nonzero bytes', result['framebuffer_nonzero_bytes'])]
    for pa, last in result['display_mmio_last_writes'].items():
        snapshot = result['display_mmio_snapshots'][pa]
        rows += [(pa, last if last is not None else
                  f'{snapshot} (snapshot)' if snapshot is not None else None),
                 (pa + ' writes', result['display_mmio_write_counts'][pa]),
                 (pa + ' distinct writes', result['display_mmio_distinct_write_values'][pa])]
    rows += [('Hook ' + k, v) for k, v in result['breakpoint_hits'].items()]
    rows += [('AGX META ' + k, v) for k, v in result['agx_meta_counts'].items()]
    return '\n'.join(f'{key:<32} {value if value is not None else "unknown"}' for key, value in rows) + '\n'


def write_summary(root, probe_exit_code=None):
    root = Path(root)
    result = summarize(root, probe_exit_code)
    (root / 'summary.json').write_text(json.dumps(result, indent=2) + '\n')
    table = format_table(result)
    (root / 'summary.txt').write_text(table)
    return result, table


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    print(write_summary(args.output)[1], end='')


if __name__ == '__main__':
    main()
