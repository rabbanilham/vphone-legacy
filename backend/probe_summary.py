#!/usr/bin/env python3
"""Summarize a display probe directory without loading large trace logs into RAM.

Exit code comes from process-exit.json (QEMU); parallel_probe additionally records
its display_probe subprocess exit code as probe_exit_code. Missing data is null.
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
                if 'DISPLAY_MMIO' in line:
                    fields = dict(re.findall(r'(\w+)=([^\s]+)', line))
                    if fields.get('op') == 'write' and fields.get('pa') in mmio:
                        mmio[fields['pa']] = hex(int(fields['value'], 0))
    exited = root / 'process-exit.json'
    result = dict(breakpoint_hits=dict(sorted(hits.items())), panic_lines=panics,
                  gpu_hang_count=hangs, display_mmio_last_writes=mmio,
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
    rows += list(result['display_mmio_last_writes'].items())
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
