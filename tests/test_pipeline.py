#!/usr/bin/env python3
"""
test_pipeline.py — Unit tests for vphone-legacy components.
"""

import os
import sys
import unittest
import struct

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(PROJECT_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from fw_manifest import get_firmware_info, list_supported_versions
from patchers.patch_kernel import LegacyKernelPatcher
from patchers.patch_devicetree import DeviceTreeNode, DeviceTreeProperty, parse_node, serialize_node

class TestFirmwareManifest(unittest.TestCase):
    def test_supported_versions(self):
        versions = list_supported_versions()
        self.assertEqual(versions, ["7.1.2"])
        self.assertFalse(get_firmware_info("8.4.1")["supported"])
        self.assertFalse(get_firmware_info("9.3.5")["supported"])

    def test_firmware_metadata(self):
        meta = get_firmware_info("7.1.2")
        self.assertIsNotNone(meta)
        self.assertEqual(meta["device"], "iPhone6,2")
        self.assertEqual(meta["chip"], "s5l8960x")
        self.assertEqual(meta["arch"], "arm64")
        self.assertTrue(len(meta["rootfs_key"]) > 32)

class TestKernelPatcher(unittest.TestCase):
    def test_heuristic_patches_fail_explicitly(self):
        original = b"watchdog timeout" + bytes(256)
        patcher = LegacyKernelPatcher(original)
        with self.assertRaisesRegex(ValueError, "Heuristic kernel patches are disabled"):
            patcher.apply_all_patches()
        self.assertEqual(patcher.data, original)
        self.assertEqual(patcher.patches_applied, [])

class TestDeviceTreePatcher(unittest.TestCase):
    def test_devicetree_serialization(self):
        root = DeviceTreeNode()
        root.set_prop("name", b"device-tree\x00")
        root.set_prop("model", b"iPhone6,2\x00")

        chosen = DeviceTreeNode()
        chosen.set_prop("name", b"chosen\x00")
        chosen.set_prop("boot-args", b"-v serial=3\x00")
        root.children.append(chosen)

        serialized = serialize_node(root)
        self.assertTrue(len(serialized) > 0)

        # Parse back
        parsed_root, _ = parse_node(serialized, 0)
        self.assertEqual(parsed_root.get_prop("model"), b"iPhone6,2\x00")
        found_chosen = parsed_root.find_node("chosen")
        self.assertIsNotNone(found_chosen)
        self.assertEqual(found_chosen.get_prop("boot-args"), b"-v serial=3\x00")

if __name__ == "__main__":
    unittest.main()
