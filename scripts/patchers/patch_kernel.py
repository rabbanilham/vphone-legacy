#!/usr/bin/env python3
"""Strict IM4P decryption and Apple complzss decoding. No speculative patches."""
import struct
import subprocess
import zlib
from pathlib import Path
from typing import Optional


def _der_item(data, offset):
    if offset + 2 > len(data):
        raise ValueError("Truncated DER header")
    tag, length = data[offset:offset + 2]
    offset += 2
    if length & 0x80:
        count = length & 0x7f
        if not count or count > 4 or offset + count > len(data):
            raise ValueError("Invalid DER length")
        length = int.from_bytes(data[offset:offset + count], "big")
        offset += count
    end = offset + length
    if end > len(data):
        raise ValueError("Truncated DER value")
    return tag, data[offset:end], end


def unpack_im4p(data: bytes, expected_type: bytes) -> bytes:
    tag, body, end = _der_item(data, 0)
    if tag != 0x30 or end != len(data):
        raise ValueError("Expected complete IM4P DER sequence")
    items = []
    offset = 0
    while offset < len(body):
        tag, value, offset = _der_item(body, offset)
        items.append((tag, value))
    if len(items) < 4 or items[0] != (0x16, b"IM4P") or items[1] != (0x16, expected_type):
        raise ValueError("IM4P signature or payload type mismatch")
    if items[2][0] != 0x16 or items[3][0] != 4 or not items[3][1]:
        raise ValueError("Invalid IM4P payload")
    return items[3][1]


def decrypt_im4p(data: bytes, expected_type: bytes, iv_hex: str, key_hex: str) -> bytes:
    payload = unpack_im4p(data, expected_type)
    iv, key = bytes.fromhex(iv_hex or ""), bytes.fromhex(key_hex or "")
    if len(iv) != 16 or len(key) != 32 or len(payload) % 16:
        raise ValueError("AES-256-CBC requires exact IV/key and block aligned payload")
    result = subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-nopad", "-K", key.hex(),
                             "-iv", iv.hex()], input=payload, capture_output=True, check=True)
    return result.stdout


def decompress_lzss(src: bytes, uncomp_len: int) -> bytes:
    if uncomp_len <= 0 or uncomp_len > 512 * 1024 * 1024:
        raise ValueError("Invalid LZSS output size")
    output = bytearray()
    ring = bytearray(b" " * 4078 + b"\0" * 18)
    cursor, offset, flags = 4078, 0, 0
    while len(output) < uncomp_len:
        flags >>= 1
        if not flags & 0x100:
            if offset >= len(src):
                raise ValueError("Truncated LZSS flags")
            flags = src[offset] | 0xff00
            offset += 1
        if flags & 1:
            if offset >= len(src):
                raise ValueError("Truncated LZSS literal")
            chunk = bytes([src[offset]])
            offset += 1
        else:
            if offset + 2 > len(src):
                raise ValueError("Truncated LZSS reference")
            first, second = src[offset:offset + 2]
            offset += 2
            index, count = first | ((second & 0xf0) << 4), (second & 15) + 3
            # References may overlap their output in the ring.
            for position in range(count):
                value = ring[(index + position) & 4095]
                output.append(value)
                ring[cursor] = value
                cursor = (cursor + 1) & 4095
                if len(output) > uncomp_len:
                    raise ValueError("LZSS reference exceeds declared size")
            continue
        output.extend(chunk)
        ring[cursor] = chunk[0]
        cursor = (cursor + 1) & 4095
    if offset != len(src):
        raise ValueError("LZSS compressed length mismatch")
    return bytes(output)


def decode_kernel_payload(payload: bytes) -> tuple[bytes, bytes]:
    """Decode compressed XNU and the appended native monitor executable.

    The monitor starts exactly after the compressed stream length declared by
    complzss. Its file-backed segments define its extent; only AES alignment
    padding may follow. No byte-pattern searching or guessed offsets are used.
    """
    if not payload.startswith(b"complzss") or len(payload) < 0x180:
        raise ValueError("Expected Apple complzss kernel payload after decryption")
    checksum, uncomp_len, comp_len = struct.unpack_from(">III", payload, 8)
    compressed_end = 0x180 + comp_len
    if comp_len == 0 or compressed_end > len(payload):
        raise ValueError("Truncated complzss stream")
    kernel = decompress_lzss(payload[0x180:compressed_end], uncomp_len)
    if zlib.adler32(kernel) & 0xffffffff != checksum:
        raise ValueError("complzss Adler32 mismatch")
    from macho import inspect_macho
    inspect_macho(kernel)
    trailing = payload[compressed_end:]
    if not trailing:
        raise ValueError("Missing appended native secure monitor")
    try:
        monitor_info = inspect_macho(trailing)
    except ValueError as error:
        raise ValueError(f"Invalid appended native secure monitor: {error}") from error
    extent = max(segment["fileoff"] + segment["filesize"] for segment in monitor_info["segments"])
    padding = trailing[extent:]
    if extent <= 0 or len(padding) > 15 or any(padding):
        raise ValueError("Unexpected data after native monitor executable")
    monitor = trailing[:extent]
    inspect_macho(monitor)
    return kernel, monitor


def decode_kernel_components(data: bytes, iv_hex: str, key_hex: str) -> tuple[bytes, bytes]:
    return decode_kernel_payload(decrypt_im4p(data, b"krnl", iv_hex, key_hex))


def decode_kernel(data: bytes, iv_hex: str, key_hex: str) -> bytes:
    """Compatibility API validates both native payloads, returns XNU only."""
    return decode_kernel_components(data, iv_hex, key_hex)[0]


def extract_and_patch_kernel(input_path: str, output_path: str,
                             iv_hex: Optional[str] = None, key_hex: Optional[str] = None):
    """Compatibility name: validate and write the unmodified decoded kernel."""
    kernel = decode_kernel(Path(input_path).read_bytes(), iv_hex, key_hex)
    Path(output_path).write_bytes(kernel)
    return {"patches": [], "reason": "No runtime-verified compatibility patches exist"}


class LegacyKernelPatcher:
    """Retained to fail explicitly for callers of the removed heuristic patcher."""
    def __init__(self, kernel_bytes: bytes):
        self.data = bytes(kernel_bytes)
        self.patches_applied = []

    def apply_all_patches(self):
        raise ValueError("Heuristic kernel patches are disabled; supply a verified patch specification")
