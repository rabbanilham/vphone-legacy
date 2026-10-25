"""Fail-closed firmware gate tests; optional integration validates the real IPSW."""
import copy
import os
from pathlib import Path
import plistlib
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import zlib

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
from fw_manifest import list_supported_versions, resolve_firmware, select_identity
from fw_prepare import prepare_firmware, monitor_boot_contract
from patchers.patch_kernel import unpack_im4p, decompress_lzss, decode_kernel, decode_kernel_components, decode_kernel_payload
from patchers.extract_ans import decode_ans_components, extract_ans_payload, ANS_SHA256
from patchers.patch_devicetree import parse_node, DeviceTreeNode, serialize_node
from patchers.patch_rootfs import inspect_hfs_partition, vfdecrypt_dmg, validate_udif, extract_hfs_partition


def manifest_fixture():
    return {"ProductVersion": "7.1.2", "ProductBuildVersion": "11D257", "SupportedProductTypes": ["iPhone6,2"],
            "BuildIdentities": [{"Info": {"DeviceClass": "n53ap", "RestoreBehavior": "Erase", "BuildNumber": "11D257"},
                                  "Manifest": {k: {"Info": {"Path": k}} for k in ("OS", "KernelCache", "DeviceTree", "iBoot")}}]}


def executable_fixture(base):
    thread = bytearray(272)
    struct.pack_into("<Q", thread, 32 * 8, base + 512)
    segment = struct.pack("<II16s4Q4I", 0x19, 72, b"__TEXT", base, 1024, 0, 1024, 5, 5, 0, 0)
    commands = segment + struct.pack("<4I", 5, 288, 6, 68) + thread
    result = bytearray(1024)
    result[:32] = struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 2, 2, len(commands), 0, 0)
    result[32:32 + len(commands)] = commands
    return bytes(result)


def compressed_kernel_fixture(monitor):
    kernel = executable_fixture(0xffffff8000200000)
    compressed = bytearray()
    for offset in range(0, len(kernel), 8):
        literal = kernel[offset:offset + 8]
        compressed.append((1 << len(literal)) - 1)
        compressed.extend(literal)
    header = bytearray(384)
    header[:8] = b"complzss"
    struct.pack_into(">III", header, 8, zlib.adler32(kernel), len(kernel), len(compressed))
    return bytes(header + compressed) + monitor, kernel


class FirmwareGateTests(unittest.TestCase):
    def test_only_supported_target(self):
        self.assertEqual(list_supported_versions(), ["7.1.2"])
        self.assertIsNone(resolve_firmware("8.4.1"))

    def test_identity_rejects_wrong_device_build_board_and_ambiguity(self):
        good = manifest_fixture()
        self.assertEqual(select_identity(good)["Info"]["DeviceClass"], "n53ap")
        for field, value in [("ProductVersion", "9.3.5"), ("ProductBuildVersion", "11D256"),
                             ("SupportedProductTypes", ["iPhone6,1"])]:
            invalid = copy.deepcopy(good)
            invalid[field] = value
            with self.assertRaises(ValueError):
                select_identity(invalid)
        for board in ("n51ap", "n53", ""):
            invalid = copy.deepcopy(good)
            invalid["BuildIdentities"][0]["Info"]["DeviceClass"] = board
            with self.assertRaises(ValueError):
                select_identity(invalid)
        invalid = copy.deepcopy(good)
        invalid["BuildIdentities"] *= 2
        with self.assertRaises(ValueError):
            select_identity(invalid)

    def test_member_selection_ignores_filename(self):
        with tempfile.TemporaryDirectory() as folder:
            fake = Path(folder) / "iPhone6,2_7.1.2_11D257_Restore.ipsw"
            wrong = manifest_fixture()
            wrong["SupportedProductTypes"] = ["iPhone6,1"]
            with zipfile.ZipFile(fake, "w") as archive:
                archive.writestr("BuildManifest.plist", plistlib.dumps(wrong))
            with self.assertRaises(ValueError):
                resolve_firmware("7.1.2", str(fake))

    def test_missing_ipsw_does_not_create_bundle(self):
        output = PROJECT / "build/missing-firmware-unit-test"
        with self.assertRaises(ValueError):
            prepare_firmware("7.1.2", None, output)
        self.assertFalse(output.exists())

    def test_refuses_to_overwrite_persistent_bundle(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "existing-vm"
            output.mkdir()
            persistent = output / "data.raw"
            persistent.write_bytes(b"existing guest data")
            with self.assertRaisesRegex(ValueError, "Output already exists"):
                prepare_firmware("7.1.2", None, output)
            self.assertEqual(persistent.read_bytes(), b"existing guest data")

    def test_encrypted_kernel_errors_do_not_fall_back_to_raw(self):
        for payload in [b"IM4P" + bytes(100), b"\xcf\xfa\xed\xfe" + bytes(100), b"\x30\x82\xff\xff"]:
            with self.assertRaises(ValueError):
                decode_kernel(payload, "00" * 16, "00" * 32)

    def test_ans_refuses_unverified_iboot_contract(self):
        for payload in (b"", bytes(0x6e000), bytes(0x6dfff)):
            with self.assertRaisesRegex(ValueError, "Unsupported iBoot content"):
                extract_ans_payload(payload)

    def test_trailing_monitor_is_bounded_by_compressed_length_and_macho(self):
        expected_monitor = executable_fixture(0x4100000000)
        payload, expected_kernel = compressed_kernel_fixture(expected_monitor + bytes(5))
        kernel, monitor = decode_kernel_payload(payload)
        self.assertEqual(kernel, expected_kernel)
        self.assertEqual(monitor, expected_monitor)

    def test_missing_corrupt_or_extra_monitor_fails(self):
        good_monitor = executable_fixture(0x4100000000)
        for tail in [b"", bytes(1024), good_monitor[:-1], good_monitor + bytes(16), good_monitor + b"X"]:
            payload, _ = compressed_kernel_fixture(tail)
            with self.assertRaisesRegex(ValueError, "monitor"):
                decode_kernel_payload(payload)

    def test_lzss_truncation_and_length_mismatch(self):
        self.assertEqual(decompress_lzss(b"\x07abc", 3), b"abc")
        for stream, count in [(b"\x07ab", 3), (b"\x00\x01", 3), (b"\x07abcd", 3), (b"\x00\x00\x00", 2)]:
            with self.assertRaises(ValueError):
                decompress_lzss(stream, count)

    def test_device_tree_bounds(self):
        for payload in [b"", struct.pack("<II", 1, 0), struct.pack("<II", 0xffffffff, 0),
                        struct.pack("<II", 1, 0) + b"name".ljust(32, b"\0") + struct.pack("<I", 256)]:
            with self.assertRaises(ValueError):
                parse_node(payload)
        root = DeviceTreeNode()
        root.set_prop("name", b"device-tree\0")
        encoded = serialize_node(root)
        parsed, end = parse_node(encoded)
        self.assertEqual(end, len(encoded))
        self.assertEqual(parsed.get_prop("name"), b"device-tree\0")

    def test_rootfs_copy_fallback_is_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "encrypted.dmg", Path(folder) / "output.dmg"
            source.write_bytes(b"encrcdsa" + bytes(1024))
            with self.assertRaises(RuntimeError):
                vfdecrypt_dmg(str(source), str(output), "00" * 36, binary=str(Path(folder) / "missing"))
            self.assertFalse(output.exists())

    def test_dmg_and_hfs_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "wrong.raw"
            file.write_bytes(bytes(4096))
            with self.assertRaises(ValueError):
                inspect_hfs_partition(file)
            with self.assertRaises(ValueError):
                validate_udif(file)
            raw = bytearray(4096)
            struct.pack_into(">HH", raw, 1024, 0x4858, 5)
            struct.pack_into(">II", raw, 1024 + 40, 512, 8)
            file.write_bytes(raw)
            self.assertEqual(inspect_hfs_partition(file)["filesystem"], "hfsx")
            struct.pack_into(">II", raw, 1024 + 40, 512, 100)
            file.write_bytes(raw)
            with self.assertRaises(ValueError):
                inspect_hfs_partition(file)

    def test_apm_uses_advertised_2048_byte_blocks(self):
        with tempfile.TemporaryDirectory() as folder:
            disk, volume = Path(folder) / "disk.raw", Path(folder) / "system.raw"
            raw = bytearray(16384)
            struct.pack_into(">HH", raw, 0, 0x4552, 2048)
            struct.pack_into(">HHIII", raw, 2048, 0x504d, 0, 1, 2, 6)
            raw[2048 + 48:2048 + 58] = b"Apple_HFSX"
            struct.pack_into(">HH", raw, 4096 + 1024, 0x4858, 5)
            struct.pack_into(">II", raw, 4096 + 1024 + 40, 512, 24)
            disk.write_bytes(raw)
            info = extract_hfs_partition(disk, volume)
            self.assertEqual(info["source_partition"]["offset"], 4096)
            self.assertEqual(info["size"], 12288)
            struct.pack_into(">I", raw, 2048 + 12, 100)
            disk.write_bytes(raw)
            with self.assertRaises(ValueError):
                extract_hfs_partition(disk, Path(folder) / "bad.raw")

    @unittest.skipUnless(os.environ.get("VPHONE_REAL_FIRMWARE_TEST"), "opt-in real firmware decryption")
    def test_real_kernel_and_devicetree(self):
        from patchers.patch_devicetree import decode_devicetree
        from macho import inspect_macho
        ipsw = Path(os.environ.get("VPHONE_TEST_IPSW", str(PROJECT / "iPhone6,2_7.1.2_11D257_Restore.ipsw"))).expanduser()
        metadata = resolve_firmware("7.1.2", str(ipsw))
        with zipfile.ZipFile(ipsw) as archive:
            import hashlib
            iboot, ans, ans_info = decode_ans_components(archive.read(metadata["iboot_path"]),
                                                       metadata["iboot_iv"], metadata["iboot_key"])
            self.assertEqual(hashlib.sha256(ans).hexdigest(), ANS_SHA256)
            self.assertEqual(len(ans), 0x39000)
            self.assertEqual(ans[ans_info["config_offset"]:ans_info["config_offset"] + 4], b"CisC")
            self.assertFalse(ans_info["runtime_fixups_applied"])
            with self.assertRaises(ValueError):
                extract_ans_payload(iboot[:-1] + bytes([iboot[-1] ^ 1]))
            with self.assertRaises(ValueError):
                decode_ans_components(archive.read(metadata["iboot_path"]), metadata["iboot_iv"], "00" * 32)
            kernel, monitor = decode_kernel_components(archive.read(metadata["kernelcache_path"]), metadata["kernel_iv"], metadata["kernel_key"])
            self.assertEqual(inspect_macho(kernel)["architecture"], "arm64")
            self.assertEqual(len(monitor), 81920)
            self.assertEqual(inspect_macho(monitor)["entry_va"], 0x410000c120)
            data, root = decode_devicetree(archive.read(metadata["devicetree_path"]), metadata["devicetree_iv"], metadata["devicetree_key"])
            self.assertEqual(root.get_prop("model"), b"iPhone6,2\0")
            self.assertEqual(monitor_boot_contract(root)["reserved_size"], 0x200000)
            self.assertEqual(monitor_boot_contract(root)["native_physical_base"], 0x800600000)
            self.assertEqual(len(data), 142016)
            with self.assertRaises(ValueError):
                decode_devicetree(archive.read(metadata["devicetree_path"]), metadata["devicetree_iv"], "00" * 32)
            with self.assertRaises(ValueError):
                decode_kernel(archive.read(metadata["kernelcache_path"]), metadata["kernel_iv"], "00" * 32)


if __name__ == "__main__":
    unittest.main()
