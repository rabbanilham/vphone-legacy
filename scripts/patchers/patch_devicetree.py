#!/usr/bin/env python3
"""
patch_devicetree.py — Apple DeviceTree (dtre) parser and patcher for vphone-legacy.
Handles parsing the native Apple DeviceTree binary format, editing boot-args,
memory sizes, and console routing for QEMU s5l8960x emulation.
"""

import sys
import struct
from typing import Dict, Any, List, Optional
from .patch_kernel import decrypt_im4p
from pathlib import Path

class DeviceTreeProperty:
    def __init__(self, name: str, value: bytes):
        self.name = name
        self.value = value

    def to_bytes(self) -> bytes:
        name_bytes = self.name.encode("utf-8")
        if len(name_bytes) > 32:
            name_bytes = name_bytes[:32]
        else:
            name_bytes = name_bytes.ljust(32, b"\x00")
        length = len(self.value)
        # Apple DeviceTree aligns property values to 4 bytes
        aligned_len = (length + 3) & ~3
        val_padded = self.value.ljust(aligned_len, b"\x00")
        return name_bytes + struct.pack("<I", length) + val_padded

class DeviceTreeNode:
    def __init__(self):
        self.properties: Dict[str, DeviceTreeProperty] = {}
        self.children: List['DeviceTreeNode'] = []

    def get_prop(self, name: str) -> Optional[bytes]:
        p = self.properties.get(name)
        return p.value if p else None

    def set_prop(self, name: str, value: bytes):
        self.properties[name] = DeviceTreeProperty(name, value)

    def find_node(self, name: str) -> Optional['DeviceTreeNode']:
        node_name_prop = self.get_prop("name")
        if node_name_prop and node_name_prop.rstrip(b"\x00").decode("utf-8", errors="ignore") == name:
            return self
        for child in self.children:
            found = child.find_node(name)
            if found:
                return found
        return None

def parse_node(data: bytes, offset: int = 0, depth: int = 0):
    if depth > 64 or offset + 8 > len(data):
        raise ValueError("Truncated or excessively nested DeviceTree")
    node = DeviceTreeNode()
    n_props, n_children = struct.unpack_from("<II", data, offset)
    offset += 8
    if n_props > 65536 or n_children > 65536:
        raise ValueError("Invalid DeviceTree node counts")
    for _ in range(n_props):
        if offset + 36 > len(data):
            raise ValueError("Truncated DeviceTree property")
        name_raw = data[offset:offset + 32]
        try:
            prop_name = name_raw.split(b"\0", 1)[0].decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError("Invalid DeviceTree property name") from exc
        if not prop_name or prop_name in node.properties:
            raise ValueError("Invalid or duplicate DeviceTree property")
        length = struct.unpack_from("<I", data, offset + 32)[0] & 0x7fffffff
        offset += 36
        aligned = (length + 3) & ~3
        if offset + aligned > len(data):
            raise ValueError("Truncated DeviceTree property value")
        node.set_prop(prop_name, data[offset:offset + length])
        offset += aligned
    for _ in range(n_children):
        child, offset = parse_node(data, offset, depth + 1)
        node.children.append(child)
    return node, offset


def decode_devicetree(data: bytes, iv_hex: str, key_hex: str):
    decrypted = decrypt_im4p(data, b"dtre", iv_hex, key_hex)
    root, end = parse_node(decrypted)
    if any(decrypted[end:]):
        raise ValueError("Nonzero trailing data after DeviceTree")
    if root.get_prop("model") != b"iPhone6,2\0" or b"N53AP" not in (root.get_prop("compatible") or b"").split(b"\0"):
        raise ValueError("DeviceTree does not identify N53AP")
    return decrypted[:end], root


def serialize_node(node: DeviceTreeNode) -> bytes:
    out = struct.pack("<II", len(node.properties), len(node.children))
    for p in node.properties.values():
        out += p.to_bytes()
    for child in node.children:
        out += serialize_node(child)
    return out

def patch_devicetree(input_path: str, output_path: str, boot_args: str = "-v serial=3",
                     iv_hex: Optional[str] = None, key_hex: Optional[str] = None):
    """Compatibility name: preserve guest DT until device contracts are verified."""
    data, root = decode_devicetree(Path(input_path).read_bytes(), iv_hex, key_hex)
    Path(output_path).write_bytes(data)
    return {"patches": [], "reason": "Boot arguments belong in verified boot_args structure"}
