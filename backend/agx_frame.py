#!/usr/bin/env python3
"""Read opt-in AGX frame snapshots (VPHONE_AGX_FRAME_DUMP).

AGXF v1 is little endian, with no native padding. The 64-byte header is
<4sIIIQQIIIIQQ: magic, version, context_id, flags (bit 0 TA valid, bit 1
capped), catalogue_pa, pbe_target, type1_size, ta_size, queue, ta_queue,
record_count, display_mmu_table. It is followed by the type-1 and most recent
same-context type-0 commands (each 0x280 bytes; TA zeroed if absent), then
record_count records <QQ4096s (page GVA, page PA, contents). The entire file
is capped at 96 MiB. Aliases are separate records; holes are never filled.
"""

import argparse
from collections import defaultdict
import json
from pathlib import Path
import struct

HEADER = struct.Struct('<4sIIIQQIIIIQQ')
RECORD = struct.Struct('<QQ4096s')
PAGE_SIZE = 4096
COMMAND_SIZE = 0x280
MAX_SIZE = 96 * 1024 * 1024
FIELDS = ('magic', 'version', 'context_id', 'flags', 'catalogue_pa',
          'pbe_target', 'type1_size', 'ta_size', 'queue', 'ta_queue',
          'record_count', 'display_mmu_table')


class Frame:
    """Load a snapshot from a path; expose header, commands, pages and PAs.

    pages maps page-aligned GVA to bytes; physical maps the same GVA to PA.
    read/u32/u64 support reads crossing page boundaries and raise KeyError
    for an unmapped page (including pages omitted by the capture cap).
    """

    def __init__(self, path):
        self.path = Path(path)
        with self.path.open('rb') as source:
            raw = source.read(HEADER.size)
            if len(raw) != HEADER.size:
                raise ValueError('truncated AGXF header')
            self.header = dict(zip(FIELDS, HEADER.unpack(raw)))
            h = self.header
            if h['magic'] != b'AGXF' or h['version'] != 1:
                raise ValueError('unsupported AGXF magic or version')
            if h['flags'] & ~3:
                raise ValueError('unknown AGXF flags')
            if h['type1_size'] != COMMAND_SIZE or h['ta_size'] != COMMAND_SIZE:
                raise ValueError('invalid command sizes')
            expected = HEADER.size + 2 * COMMAND_SIZE + h['record_count'] * RECORD.size
            if expected > MAX_SIZE or self.path.stat().st_size != expected:
                raise ValueError('invalid AGXF file size or record count')
            self.type1_command = source.read(COMMAND_SIZE)
            self.ta_command = source.read(COMMAND_SIZE)
            self.pages = {}
            self.physical = {}
            for _ in range(h['record_count']):
                record = source.read(RECORD.size)
                if len(record) != RECORD.size:
                    raise ValueError('truncated page record')
                gva, pa, data = RECORD.unpack(record)
                if gva % PAGE_SIZE or pa % PAGE_SIZE or gva >= 1 << 40:
                    raise ValueError('invalid page address')
                if gva in self.pages:
                    raise ValueError(f'duplicate GVA 0x{gva:x}')
                self.pages[gva] = data
                self.physical[gva] = pa

    @property
    def ta_valid(self):
        return bool(self.header['flags'] & 1)

    @property
    def capped(self):
        return bool(self.header['flags'] & 2)

    def read(self, gva, n):
        if gva < 0 or n < 0 or gva + n > 1 << 40:
            raise ValueError('read outside 40-bit GPU address space')
        chunks = []
        while n:
            page, offset = divmod(gva, PAGE_SIZE)
            base = page * PAGE_SIZE
            try:
                data = self.pages[base]
            except KeyError:
                raise KeyError(f'unmapped GVA 0x{gva:x} (page 0x{base:x})') from None
            size = min(n, PAGE_SIZE - offset)
            chunks.append(data[offset:offset + size])
            gva += size
            n -= size
        return b''.join(chunks)

    def u32(self, gva):
        return struct.unpack('<I', self.read(gva, 4))[0]

    def u64(self, gva):
        return struct.unpack('<Q', self.read(gva, 8))[0]

    def ranges_by_pc(self):
        """Return PC index -> contiguous [start, end) GVA ranges."""
        groups = defaultdict(list)
        for gva in sorted(self.pages):
            ranges = groups[gva >> 30]
            if ranges and ranges[-1][1] == gva:
                ranges[-1] = (ranges[-1][0], gva + PAGE_SIZE)
            else:
                ranges.append((gva, gva + PAGE_SIZE))
        return dict(groups)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('frame', type=Path)
    args = parser.parse_args(argv)
    try:
        frame = Frame(args.frame)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    header = {key: value.decode('ascii') if isinstance(value, bytes) else hex(value)
              for key, value in frame.header.items()}
    print(json.dumps(header, indent=2))
    print(f'TA valid: {frame.ta_valid}; capped: {frame.capped}')
    print('Mapped GVA histogram by PC index (ranges are end-exclusive):')
    groups = frame.ranges_by_pc()
    counts = {pc: sum((end - start) // PAGE_SIZE for start, end in ranges)
              for pc, ranges in groups.items()}
    largest = max(counts.values(), default=1)
    for pc, ranges in groups.items():
        count = counts[pc]
        bar = '#' * max(1, round(40 * count / largest))
        print(f'PC[{pc:3d}] {count:6d} pages {bar}')
        for start, end in ranges:
            print(f'  0x{start:010x}..0x{end:010x}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
