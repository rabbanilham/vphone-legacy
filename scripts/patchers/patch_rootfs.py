#!/usr/bin/env python3
"""Validate/decrypt/convert firmware storage. Never disable system daemons."""
import os
import shutil
import struct
import subprocess
from pathlib import Path


def validate_udif(path):
    path = Path(path)
    if path.stat().st_size < 512:
        raise ValueError("RootFS is truncated")
    with path.open("rb") as stream:
        stream.seek(-512, os.SEEK_END)
        header = stream.read(512)
    if header[:4] != b"koly" or struct.unpack_from(">II", header, 4) != (4, 512):
        raise ValueError("RootFS is not a valid decrypted UDIF v4 container")
    sectors = struct.unpack_from(">Q", header, 492)[0]
    xml_offset, xml_size = struct.unpack_from(">QQ", header, 216)
    if not sectors or not xml_size or xml_offset + xml_size > path.stat().st_size - 512:
        raise ValueError("Invalid UDIF sector count or resource bounds")
    return {"sectors": sectors, "sector_size": 512}


def vfdecrypt_dmg(input_dmg: str, output_dmg: str, key_hex: str, binary=None):
    key = bytes.fromhex(key_hex)
    if len(key) != 36:
        raise ValueError("VFDecrypt key must contain 16-byte AES and 20-byte HMAC keys")
    project = Path(__file__).resolve().parents[2]
    binary = binary or str(project / "dist/vfdecrypt")
    if not Path(binary).is_file() or not os.access(binary, os.X_OK):
        raise RuntimeError("vfdecrypt helper is missing; run firmware helper build first")
    with open(input_dmg, "rb") as stream:
        if stream.read(8) != b"encrcdsa":
            raise ValueError("Expected encrypted v2 Apple RootFS")
    try:
        subprocess.run([binary, "-i", input_dmg, "-o", output_dmg, "-k", key_hex],
                       capture_output=True, check=True)
        validate_udif(output_dmg)
    except Exception:
        Path(output_dmg).unlink(missing_ok=True)
        raise
    return True


def inspect_hfs_partition(path):
    path = Path(path)
    size = path.stat().st_size
    with path.open("rb") as stream:
        stream.seek(1024)
        header = stream.read(512)
    if len(header) != 512 or header[:2] not in (b"H+", b"HX"):
        raise ValueError("Expected raw HFS+/HFSX partition, not a DMG or disk container")
    signature, version = struct.unpack_from(">HH", header)
    if version != (5 if header[:2] == b"HX" else 4):
        raise ValueError("HFS volume version mismatch")
    block_size, blocks = struct.unpack_from(">II", header, 40)
    if block_size < 512 or block_size & (block_size - 1) or not blocks or blocks * block_size > size:
        raise ValueError("Invalid HFS allocation bounds")
    return {"filesystem": "hfsx" if header[:2] == b"HX" else "hfs+",
            "size": size, "block_size": block_size, "total_blocks": blocks}


def extract_hfs_partition(disk_path, partition_path):
    """Decode APM using its advertised logical block size, never guessed offsets."""
    disk = Path(disk_path)
    with disk.open("rb") as stream:
        driver = stream.read(512)
        if driver[:2] != b"ER":
            raise ValueError("Expected Apple partition map in decoded RootFS disk")
        block_size = struct.unpack_from(">H", driver, 2)[0]
        if block_size < 512 or block_size > 65536 or block_size & (block_size - 1):
            raise ValueError("Invalid Apple partition block size")
        stream.seek(block_size)
        first = stream.read(512)
        if first[:2] != b"PM":
            raise ValueError("Missing Apple partition map entry")
        entries = struct.unpack_from(">I", first, 4)[0]
        if not 1 <= entries <= 128:
            raise ValueError("Invalid Apple partition map count")
        candidates = []
        for index in range(1, entries + 1):
            stream.seek(index * block_size)
            entry = stream.read(512)
            if entry[:2] != b"PM" or struct.unpack_from(">I", entry, 4)[0] != entries:
                raise ValueError("Malformed Apple partition map")
            if entry[48:80].split(b"\0", 1)[0] not in (b"Apple_HFS", b"Apple_HFSX"):
                continue
            start, count = struct.unpack_from(">II", entry, 8)
            offset, length = start * block_size, count * block_size
            if offset < (entries + 1) * block_size or not length or offset + length > disk.stat().st_size:
                raise ValueError("HFS partition exceeds decoded disk bounds")
            candidates.append((offset, length))
        if len(candidates) != 1:
            raise ValueError("Expected exactly one HFS system partition")
        offset, length = candidates[0]
        stream.seek(offset)
        with Path(partition_path).open("xb") as output:
            remaining = length
            while remaining:
                chunk = stream.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise ValueError("Truncated HFS partition")
                output.write(chunk)
                remaining -= len(chunk)
    info = inspect_hfs_partition(partition_path)
    info["source_partition"] = {"offset": offset, "size": length, "apm_block_size": block_size}
    return info


def convert_udif_to_raw(input_dmg, output_path):
    geometry = validate_udif(input_dmg)
    tool = shutil.which("qemu-img")
    if not tool:
        raise RuntimeError("qemu-img required to decode UDIF into disk sectors")
    container = Path(str(output_path) + ".disk-container")
    try:
        subprocess.run([tool, "convert", "-f", "dmg", "-O", "raw", str(input_dmg), str(container)],
                       capture_output=True, check=True)
        if container.stat().st_size != geometry["sectors"] * 512:
            raise ValueError("Converted raw image sector count differs from UDIF")
        return extract_hfs_partition(container, output_path)
    finally:
        container.unlink(missing_ok=True)


def create_data_volume(output_path, size=1024 * 1024 * 1024):
    """hdiutil delegates formatting to newfs_hfs on a transient backing device.

    No host volume is mounted. macOS disk-image services may require the caller
    to execute outside its restricted filesystem sandbox.
    """
    hdiutil, converter = shutil.which("hdiutil"), shutil.which("qemu-img")
    if not hdiutil or not converter:
        raise RuntimeError("hdiutil and qemu-img are required to initialize a persistent HFSX volume")
    output = Path(output_path)
    if output.exists() or size < 64 * 1024 * 1024 or size % 512:
        raise ValueError("Data image must be new and contain whole sectors (minimum 64 MiB)")
    image = Path(str(output) + ".dmg")
    try:
        subprocess.run([hdiutil, "create", "-sectors", str(size // 512),
                        "-fs", "Case-sensitive HFS+", "-volname", "vphone-data",
                        "-layout", "NONE", "-type", "UDIF", "-nospotlight", str(image)],
                       capture_output=True, check=True)
        # Current macOS creates raw sectors for a blank layout NONE image even
        # with the .dmg suffix. Older macOS may wrap them in UDIF.
        with image.open("rb") as stream:
            stream.seek(1024)
            raw_signature = stream.read(2)
        if raw_signature in (b"H+", b"HX"):
            inspect_hfs_partition(image)
            image.rename(output)
        else:
            validate_udif(image)
            subprocess.run([converter, "convert", "-f", "dmg", "-O", "raw", str(image), str(output)],
                           capture_output=True, check=True)
        if output.stat().st_size != size:
            raise ValueError("Data image size differs from requested sector count")
        return inspect_hfs_partition(output)
    finally:
        image.unlink(missing_ok=True)


validate_raw_hfs = inspect_hfs_partition


def patch_mounted_rootfs(*args, **kwargs):
    raise ValueError("Unverified RootFS patches are disabled; preserve firmware system services")
