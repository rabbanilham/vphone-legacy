"""Committed logical ASP utility namespaces; not an Apple physical NAND image."""
import os
from pathlib import Path
import struct
import tempfile
import time
import zlib

from bundle import digest, resolve_artifact
from patchers.extract_ans import ANS_SHA256

PAGE_SIZE = 4096
MAGIC = b"VPHONE-UTILITY1\0"
PROFILE = b"n53ap-11D257-ASP10-utility-v1".ljust(32, b"\0")
# Native guest logical API capacities/opcodes, not physical NAND metadata.
REGIONS = ((0x30, 0x40, 253), (0x31, 0x41, 2048), (0x34, 0x44, 2),
           (0x35, 0x45, 1), (0x36, 0x46, 2), (0x37, 0x47, 32))
IMAGE_SIZE = (1 + sum(pages for _, _, pages in REGIONS)) * PAGE_SIZE


def utility_header(system_lbas, data_lbas, epoch):
    if not 0 < system_lbas <= 0xffffffff or not 0 < data_lbas <= 0xffffffff or not 0 <= epoch <= 0xffffffff:
        raise ValueError("Invalid logical utility geometry or format epoch")
    header = bytearray(PAGE_SIZE)
    header[:16] = MAGIC
    struct.pack_into("<4I3Q", header, 16, 1, PAGE_SIZE, len(REGIONS), 0,
                     epoch, system_lbas, data_lbas)
    header[56:88] = bytes.fromhex(ANS_SHA256)
    header[88:120] = PROFILE
    page = 1
    for index, (read, write, count) in enumerate(REGIONS):
        struct.pack_into("<IIQQQ", header, 128 + index * 32, read, write, page, count, 0)
        page += count
    struct.pack_into("<I", header, PAGE_SIZE - 4, zlib.crc32(header[:-4]))
    return bytes(header)


def validate_utility_image(path, system_lbas, data_lbas):
    path = Path(path)
    if not path.is_file() or path.stat().st_size != IMAGE_SIZE:
        raise ValueError("Logical utility image missing or size changed")
    with path.open("rb") as stream:
        header = stream.read(PAGE_SIZE)
    epoch = struct.unpack_from("<Q", header, 32)[0]
    # Comparing the complete reconstructed header checks reserved bytes,
    # source binding, every region extent, profile/version and header CRC.
    if header != utility_header(system_lbas, data_lbas, epoch):
        raise ValueError("Logical utility header/profile/geometry/CRC mismatch")
    return dict(profile=PROFILE.rstrip(b"\0").decode(), size=IMAGE_SIZE,
                format_epoch=epoch, namespace_initialized=True,
                physical_nand_format=False, factory_identity=False)


def prepare_utility_image(bundle, manifest):
    """Initialize new erased logical regions; never format existing VM media."""
    bundle = Path(bundle).resolve()
    artifacts = manifest["artifacts"]
    source_system_sha = artifacts["system_disk"]["sha256"]
    system_lbas, data_lbas = [artifacts[name]["size"] // PAGE_SIZE
                              for name in ("system_disk", "data_disk")]
    if any(artifacts[name]["size"] % PAGE_SIZE for name in ("system_disk", "data_disk")):
        raise ValueError("Utility model requires sector-aligned backing geometry")
    existing = artifacts.get("utility_disk")
    if existing is not None:
        disk = resolve_artifact(bundle, existing)
        if (existing.get("mutable") is not True or existing.get("source_ans_sha256") != ANS_SHA256 or
                existing.get("source_system_sha256") != source_system_sha or
                existing.get("size") != IMAGE_SIZE):
            raise ValueError("Logical utility artifact provenance mismatch; existing contents preserved")
        state = validate_utility_image(disk, system_lbas, data_lbas)
        if state["format_epoch"] != existing.get("format_epoch") or state["profile"] != existing.get("profile"):
            raise ValueError("Logical utility provisioning epoch/profile differs from its record")
        return existing
    disk = bundle / "utility.raw"
    if disk.exists() or disk.is_symlink():
        raise ValueError("Unregistered utility.raw exists; preserve it and resolve its provenance")
    with tempfile.NamedTemporaryFile(dir=bundle, prefix="utility-init-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(bytes(PAGE_SIZE))  # No valid header before all regions exist.
            remaining = IMAGE_SIZE - PAGE_SIZE
            erased = b"\xff" * (256 * PAGE_SIZE)
            while remaining:
                chunk = erased[:min(remaining, len(erased))]
                stream.write(chunk)
                remaining -= len(chunk)
            stream.flush()
            os.fsync(stream.fileno())
            epoch = int(time.time())  # Actual virtual-namespace provisioning event.
            stream.seek(0)
            stream.write(utility_header(system_lbas, data_lbas, epoch))
            stream.flush()
            os.fsync(stream.fileno())
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    try:
        validate_utility_image(temporary, system_lbas, data_lbas)
        checksum = digest(temporary)
        temporary.replace(disk)
        directory = os.open(bundle, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return dict(path=disk.name, size=IMAGE_SIZE, mutable=True, sha256=checksum,
                source_ans_sha256=ANS_SHA256, source_system_sha256=source_system_sha,
                profile=PROFILE.rstrip(b"\0").decode(),
                provisioning="Initialized erased logical namespaces; not native NAND formatter",
                format_epoch=epoch)
