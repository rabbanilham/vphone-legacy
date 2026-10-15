"""Prepare native ANS RAM using the audited iBoot handoff, without executing it."""
import hashlib
from pathlib import Path
import struct

from patchers.extract_ans import ANS_SHA256

ANS_BASE = 0x83f800000
ANS_SIZE = 0x800000
CONFIG_OFFSET = 0x365a0


def prepare_ans_region(firmware_path, output_dir, clocks):
    source = Path(firmware_path).read_bytes()
    if len(source) != 0x39000 or hashlib.sha256(source).hexdigest() != ANS_SHA256:
        raise ValueError("ANS firmware must be the verified native n53ap 11D257 payload")
    magic, version, memory_size, alignment = struct.unpack_from("<4sIII", source, CONFIG_OFFSET)
    requested = struct.unpack_from("<I", source, CONFIG_OFFSET + 0x14)[0]
    count = struct.unpack_from("<Q", source, CONFIG_OFFSET + 0x18)[0]
    if (magic, version, memory_size, alignment, requested, count) != (b"CisC", 2, 0x982f0, 0x1000, 0x400000, 2):
        raise ValueError("Unexpected native ANS configuration")
    if len(clocks) <= 41 or clocks[0] != 24000000 or not 0 < clocks[41] <= 0xffffffff:
        raise ValueError("ANS clocks must match the experimental A7 clock profile")
    shared_offset = (memory_size + alignment - 1) & ~(alignment - 1)
    if shared_offset + requested > ANS_SIZE:
        raise ValueError("ANS shared memory exceeds the native reservation")
    region = bytearray(ANS_SIZE)  # Includes the native BSS/shared-memory extent.
    region[:len(source)] = source
    fixups = []
    # Widths and stores recovered from iBoot 0x6f9c..0x70a0. A0/zero debug
    # policy and PLL frequencies are an explicit emulator profile, not fuses.
    for offset, width, value, meaning in (
            (0x40, 4, 0x8960, "chip-id"),
            (0x44, 4, 0, "chip-revision A0 emulator profile"),
            (0x48, 4, clocks[0], "clock table entry 0"),
            (0x4c, 4, clocks[41], "clock table entry 41"),
            (0x20, 8, shared_offset, "first shared region offset"),
            (0x28, 4, requested, "first shared region size"),
            (0x30, 8, 0, "second shared region offset"),
            (0x38, 4, 0, "second shared region size"),
            (0x50, 4, 0, "production debug policy emulator profile")):
        at = CONFIG_OFFSET + offset
        before = bytes(region[at:at + width])
        region[at:at + width] = value.to_bytes(width, "little")
        fixups.append(dict(offset=at, width=width, meaning=meaning,
                           before=before.hex(), after=region[at:at + width].hex()))
    output = Path(output_dir) / "ANSRegion.bin"
    output.write_bytes(region)
    return dict(path=str(output.resolve()), address=ANS_BASE, size=ANS_SIZE,
                sha256=hashlib.sha256(region).hexdigest(), source_sha256=ANS_SHA256,
                firmware_memory_size=memory_size, shared_offset=shared_offset,
                shared_size=requested, fixups=fixups, runtime_fixups_applied=True,
                execution_verified=False,
                source_contract="research/native-ans-firmware.md; iBoot-1940.10.58")
