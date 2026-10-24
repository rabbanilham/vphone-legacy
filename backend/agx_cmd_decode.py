#!/usr/bin/env python3
"""Decode VPHONE_AGX_DUMP and AGX RT lines; all command words are little endian.

Address labels are heuristics, not recovered field definitions. META candidates
map to GPU VA 0xe1c0000000 + (address & 0x3fffffff), as documented in docs/architecture.md.
Malformed dump lines are rejected with their source line number.
"""

import argparse
import json
from pathlib import Path
import re
import struct
import sys

GPU_BASE = 0xE1C0000000
DUMP_RE = re.compile(
    r"AGX DUMP queue (0x[0-9a-fA-F]+) cmd (0x[0-9a-fA-F]+) "
    r"len (0x[0-9a-fA-F]+) hex=([0-9a-fA-F]+)(?=\s|$)"
)

RT_RE = re.compile(
    r"AGX RT queue (0x[0-9a-fA-F]+) cmd (0x[0-9a-fA-F]+) "
    r"ch ([0-9]+) rt0 gva (0x[0-9a-fA-F]+) pa (0x[0-9a-fA-F]+) "
    r"fmt (0x[0-9a-fA-F]+) w ([0-9]+) h ([0-9]+) load (0x[0-9a-fA-F]+)(?=\s|$)"
)


def parse_rt_lines(lines):
    targets = []
    keys = ('queue', 'address', 'channel', 'gva', 'pa', 'fmt', 'w', 'h', 'load')
    for line_number, line in enumerate(lines, 1):
        # Auxiliary span/discontinuity/fill diagnostics are separate records.
        if 'AGX RT queue' not in line:
            continue
        match = RT_RE.search(line)
        if not match:
            raise ValueError(f"line {line_number}: malformed AGX RT")
        targets.append(dict(line=line_number, **{
            key: int(value, 16 if value.startswith('0x') else 10)
            for key, value in zip(keys, match.groups())}))
    return targets


def format_rt_report(targets):
    if not targets:
        return ''
    output = ['AGX RT render targets:',
              'queue       cmd         ch  rt0 gva     pa           fmt        w x h     load']
    for rt in targets:
        output.append(f"0x{rt['queue']:08x}  0x{rt['address']:08x}  {rt['channel']:2d}  "
                      f"0x{rt['gva']:08x}  0x{rt['pa']:09x}  0x{rt['fmt']:08x}  "
                      f"{rt['w']} x {rt['h']}  0x{rt['load']:x}")
    return '\n'.join(output)


def address_hint(value):
    if 0x40000000 <= value <= 0xFFFFFFFF:
        return f"META? -> GPU 0x{GPU_BASE + (value & 0x3fffffff):x}"
    if GPU_BASE <= value < GPU_BASE + 0x40000000:
        return "GPU VA?"
    return ""


def parse_lines(lines, command_type=None):
    commands = []
    for line_number, line in enumerate(lines, 1):
        if "AGX DUMP" not in line:
            continue
        match = DUMP_RE.search(line)
        if not match:
            raise ValueError(f"line {line_number}: malformed AGX dump")
        queue, address, length = (int(value, 16) for value in match.groups()[:3])
        try:
            raw = bytes.fromhex(match[4])
        except ValueError as exc:
            raise ValueError(f"line {line_number}: invalid hex bytes") from exc
        if len(raw) != length or length < 4 or length % 4:
            raise ValueError(f"line {line_number}: invalid length 0x{length:x} "
                             f"for {len(raw)} bytes")
        words32 = list(struct.unpack(f"<{length // 4}I", raw))
        if command_type is not None and words32[0] != command_type:
            continue
        words64 = [int.from_bytes(raw[i:i + 8], "little")
                   for i in range(0, length - 7, 8)]
        commands.append(dict(line=line_number, queue=queue, address=address,
                             length=length, type=words32[0], hex=raw.hex(),
                             u32=words32, u64=words64))
    return commands


def group_commands(commands):
    groups = {}
    for command in commands:
        groups.setdefault((command["queue"], command["type"]), []).append(command)
    return groups


def word_diff(previous, current):
    """Return changed u32 words, including words added or removed."""
    return [dict(offset=i * 4,
                 before=previous[i] if i < len(previous) else None,
                 after=current[i] if i < len(current) else None)
            for i in range(max(len(previous), len(current)))
            if (previous[i] if i < len(previous) else None)
            != (current[i] if i < len(current) else None)]


def format_report(commands):
    output = []
    for (queue, kind), group in group_commands(commands).items():
        output.append(f"Queue 0x{queue:x} type {kind}: {len(group)} commands")
        previous = None
        for command in group:
            output.append(f"  cmd 0x{command['address']:x} line {command['line']} "
                          f"len 0x{command['length']:x}")
            for width in (32, 64):
                output.append(f"    u{width}: offset  value  address candidate")
                for i, value in enumerate(command[f"u{width}"]):
                    output.append(f"      +0x{i * (width // 8):03x}  "
                                  f"0x{value:0{width // 4}x}  {address_hint(value)}".rstrip())
            if previous is not None:
                changes = word_diff(previous, command["u32"])
                output.append(f"    Diff u32: {len(changes)} changed words")
                for change in changes:
                    before, after = ("absent" if change[key] is None else
                                     f"0x{change[key]:08x}" for key in ("before", "after"))
                    output.append(f"      +0x{change['offset']:03x}: {before} -> {after}")
            previous = command["u32"]
    return "\n".join(output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--type", type=lambda value: int(value, 0), dest="command_type")
    parser.add_argument("--json", type=Path, dest="json_path")
    args = parser.parse_args(argv)
    try:
        with args.trace.open(encoding="utf-8", errors="replace") as trace:
            lines = trace.readlines()
        commands = parse_lines(lines, args.command_type)
        targets = parse_rt_lines(lines) if args.command_type in (None, 1) else []
        if args.json_path:
            groups = []
            for (queue, kind), group in group_commands(commands).items():
                records = []
                previous = None
                for command in group:
                    records.append(dict(command, changes=None if previous is None else
                                        word_diff(previous, command["u32"])))
                    previous = command["u32"]
                groups.append(dict(queue=queue, type=kind, commands=records))
            for target in targets:
                group = next((group for group in groups
                              if group['queue'] == target['queue'] and group['type'] == 1), None)
                if group is None:
                    group = dict(queue=target['queue'], type=1, commands=[])
                    groups.append(group)
                group.setdefault('render_targets', []).append(target)
            args.json_path.write_text(json.dumps(groups, indent=2) + "\n", encoding="utf-8")
        print('\n'.join(part for part in (format_report(commands),
                                          format_rt_report(targets)) if part))
    except (OSError, ValueError) as exc:
        print(f"{parser.prog}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
