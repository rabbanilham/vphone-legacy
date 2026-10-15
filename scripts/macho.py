"""Strict, dependency-free parser for the ARM64 kernel image used by the loader."""

import struct

MH_MAGIC_64 = 0xFEEDFACF
CPU_TYPE_ARM64 = 0x0100000C
LC_SEGMENT_64 = 0x19
LC_UNIXTHREAD = 5


def inspect_macho(data: bytes) -> dict:
    if len(data) < 32:
        raise ValueError("Truncated Mach-O header")
    magic, cpu, subtype, kind, ncmds, sizeofcmds, flags, reserved = struct.unpack_from("<8I", data)
    if magic != MH_MAGIC_64 or cpu != CPU_TYPE_ARM64 or kind != 2:
        raise ValueError("Expected an ARM64 Mach-O executable")
    commands_end = 32 + sizeofcmds
    if commands_end > len(data) or ncmds > sizeofcmds // 8:
        raise ValueError("Invalid Mach-O load command table")
    offset, segments, entry = 32, [], None
    for _ in range(ncmds):
        if offset + 8 > commands_end:
            raise ValueError("Truncated load command")
        cmd, size = struct.unpack_from("<II", data, offset)
        if size < 8 or size % 8 or offset + size > commands_end:
            raise ValueError("Invalid load command size")
        if cmd == LC_SEGMENT_64:
            if size < 72:
                raise ValueError("Truncated segment command")
            name = data[offset + 8:offset + 24].split(b"\0", 1)[0].decode("ascii")
            vmaddr, vmsize, fileoff, filesize, maxprot, initprot, nsects, _ = struct.unpack_from("<4Q4I", data, offset + 24)
            if size != 72 + nsects * 80 or filesize > vmsize or fileoff + filesize > len(data):
                raise ValueError(f"Invalid segment {name}")
            if vmaddr + vmsize > 1 << 64:
                raise ValueError("Segment address overflow")
            segments.append(dict(name=name, vmaddr=vmaddr, vmsize=vmsize,
                                 fileoff=fileoff, filesize=filesize,
                                 initprot=initprot, maxprot=maxprot))
        elif cmd == LC_UNIXTHREAD:
            cursor = offset + 8
            while cursor < offset + size:
                if cursor + 8 > offset + size:
                    raise ValueError("Truncated thread-state header")
                flavor, count = struct.unpack_from("<II", data, cursor)
                cursor += 8
                if cursor + count * 4 > offset + size:
                    raise ValueError("Truncated thread state")
                if flavor == 6:  # ARM_THREAD_STATE64: x[29], fp, lr, sp, pc, cpsr, pad
                    if count != 68 or entry is not None:
                        raise ValueError("Ambiguous or malformed ARM64 entrypoint")
                    entry = struct.unpack_from("<Q", data, cursor + 32 * 8)[0]
                cursor += count * 4
        offset += size
    if offset != commands_end:
        raise ValueError("Load command count/size mismatch")
    text = [s for s in segments if s["name"] == "__TEXT"]
    if len(text) != 1 or entry is None:
        raise ValueError("Missing __TEXT or ARM_THREAD_STATE64 entrypoint")
    allocated = sorted((s for s in segments if s["vmsize"]), key=lambda s: s["vmaddr"])
    for left, right in zip(allocated, allocated[1:]):
        if left["vmaddr"] + left["vmsize"] > right["vmaddr"]:
            raise ValueError("Overlapping Mach-O segments")
    if not any(s["initprot"] & 4 and s["vmaddr"] <= entry < s["vmaddr"] + s["filesize"] for s in segments):
        raise ValueError("Entrypoint is outside file-backed executable memory")
    return dict(architecture="arm64", entry_va=entry, text_vmaddr=text[0]["vmaddr"], segments=segments)


def file_offset(image: dict, address: int, size: int = 1) -> int:
    for seg in image["segments"]:
        delta = address - seg["vmaddr"]
        if 0 <= delta and delta + size <= seg["filesize"]:
            return seg["fileoff"] + delta
    raise ValueError("Address is outside file-backed Mach-O memory")
