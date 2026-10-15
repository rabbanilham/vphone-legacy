"""Virtual utility provisioning integrity; no native NAND/key/boot claims."""
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from qemu.utility import (PAGE_SIZE, IMAGE_SIZE, REGIONS, prepare_utility_image,
                          utility_header, validate_utility_image)


def manifest():
    return {"artifacts": {name: {"size": 4 * PAGE_SIZE, "sha256": "f" * 64}
                           for name in ("system_disk", "data_disk")}}


class UtilityModelTests(unittest.TestCase):
    def test_committed_erased_namespaces_and_guest_changes_survive_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest()
            with patch("qemu.utility.time.time", return_value=1600000000):
                record = prepare_utility_image(root, value)
            disk = root / record["path"]
            contents = disk.read_bytes()
            self.assertEqual(len(contents), IMAGE_SIZE)
            self.assertEqual(contents[:PAGE_SIZE], utility_header(4, 4, 1600000000))
            self.assertEqual(contents[PAGE_SIZE:], b"\xff" * (IMAGE_SIZE - PAGE_SIZE))
            state = validate_utility_image(disk, 4, 4)
            self.assertTrue(state["namespace_initialized"])
            self.assertFalse(state["physical_nand_format"])
            self.assertFalse(state["factory_identity"])
            with disk.open("r+b") as stream:
                stream.seek(PAGE_SIZE)
                stream.write(b"guest-owned bytes")
            value["artifacts"]["utility_disk"] = record
            with patch("qemu.utility.time.time", return_value=1700000000):
                self.assertEqual(prepare_utility_image(root, value), record)
            self.assertEqual(disk.read_bytes()[PAGE_SIZE:PAGE_SIZE + 17], b"guest-owned bytes")
            self.assertEqual(validate_utility_image(disk, 4, 4)["format_epoch"], 1600000000)

    def test_wrong_geometry_corrupt_or_truncated_header_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = prepare_utility_image(root, manifest())
            disk = root / record["path"]
            with self.assertRaisesRegex(ValueError, "geometry"):
                validate_utility_image(disk, 5, 4)
            with disk.open("r+b") as stream:
                stream.seek(56)
                stream.write(b"\x00")
            with self.assertRaisesRegex(ValueError, "mismatch"):
                validate_utility_image(disk, 4, 4)
            disk.write_bytes(utility_header(4, 4, 0))
            with self.assertRaisesRegex(ValueError, "size"):
                validate_utility_image(disk, 4, 4)

    def test_valid_crc_cannot_hide_overlapping_region_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = prepare_utility_image(root, manifest())
            disk = root / record["path"]
            header = bytearray(utility_header(4, 4, 1))
            struct.pack_into("<Q", header, 128 + 32 + 8, 1)
            struct.pack_into("<I", header, PAGE_SIZE - 4, zlib.crc32(header[:-4]))
            with disk.open("r+b") as stream:
                stream.write(header)
            with self.assertRaisesRegex(ValueError, "mismatch"):
                validate_utility_image(disk, 4, 4)

    def test_existing_unregistered_image_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            disk = root / "utility.raw"
            disk.write_bytes(b"existing guest contents")
            with self.assertRaisesRegex(ValueError, "Unregistered"):
                prepare_utility_image(root, manifest())
            self.assertEqual(disk.read_bytes(), b"existing guest contents")

    def test_invalid_epoch_or_geometry_fails(self):
        for epoch in (-1, 0x100000000):
            with self.assertRaises(ValueError):
                utility_header(4, 4, epoch)
        with self.assertRaises(ValueError):
            utility_header(0, 4, 1)

    def test_changed_provisioning_epoch_or_system_origin_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = manifest()
            record = prepare_utility_image(root, value)
            value["artifacts"]["utility_disk"] = record
            with (root / record["path"]).open("r+b") as stream:
                stream.write(utility_header(4, 4, record["format_epoch"] - 1))
            with self.assertRaisesRegex(ValueError, "epoch/profile"):
                prepare_utility_image(root, value)
            with (root / record["path"]).open("r+b") as stream:
                stream.write(utility_header(4, 4, record["format_epoch"]))
            value["artifacts"]["system_disk"]["sha256"] = "other origin"
            with self.assertRaisesRegex(ValueError, "provenance"):
                prepare_utility_image(root, value)


if __name__ == "__main__":
    unittest.main()
