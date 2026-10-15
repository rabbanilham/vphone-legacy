"""Writable system journaling must preserve firmware and existing guest writes."""
import hashlib
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from qemu.storage import prepare_system_runtime, snapshot_storage


def fixture(root):
    data = bytearray(8192)
    struct.pack_into(">HH", data, 1024, 0x4858, 5)
    struct.pack_into(">II", data, 1064, 4096, 2)
    (root / "system.raw").write_bytes(data)
    return {"artifacts": {"system_disk": {
        "path": "system.raw", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}}}


class StorageRuntimeTests(unittest.TestCase):
    def test_snapshot_survives_later_guest_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture(root)
            source = root / "system.raw"
            before = source.read_bytes()
            records = snapshot_storage({"system": str(source)}, root / "before")
            source.write_bytes(b"later guest changes")
            snapshot = Path(records["system"]["path"])
            self.assertEqual(snapshot.read_bytes(), before)
            self.assertEqual(records["system"]["sha256"], hashlib.sha256(before).hexdigest())
            self.assertFalse(snapshot.samefile(source))

    def test_guest_writes_survive_reuse_and_original_stays_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = fixture(root)
            record = prepare_system_runtime(root, manifest)
            disk = root / record["path"]
            original = (root / "system.raw").read_bytes()
            self.assertEqual(disk.read_bytes(), original)
            self.assertFalse(disk.samefile(root / "system.raw"))
            with disk.open("r+b") as stream:
                stream.seek(4096)
                stream.write(b"guest journal update")
            manifest["artifacts"]["system_runtime_disk"] = record
            self.assertEqual(prepare_system_runtime(root, manifest), record)
            self.assertEqual(disk.read_bytes()[4096:4116], b"guest journal update")
            self.assertEqual((root / "system.raw").read_bytes(), original)

    def test_unregistered_existing_disk_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = fixture(root)
            disk = root / "system-runtime.raw"
            disk.write_bytes(b"existing unrelated contents")
            with self.assertRaisesRegex(ValueError, "Unregistered"):
                prepare_system_runtime(root, manifest)
            self.assertEqual(disk.read_bytes(), b"existing unrelated contents")

    def test_changed_origin_or_size_is_rejected_without_recreating(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = fixture(root)
            record = prepare_system_runtime(root, manifest)
            manifest["artifacts"]["system_runtime_disk"] = record | {"source_sha256": "wrong"}
            with self.assertRaisesRegex(ValueError, "provenance"):
                prepare_system_runtime(root, manifest)
            manifest["artifacts"]["system_runtime_disk"] = record
            (root / record["path"]).write_bytes(b"short existing disk")
            with self.assertRaisesRegex(ValueError, "size"):
                prepare_system_runtime(root, manifest)
            self.assertEqual((root / record["path"]).read_bytes(), b"short existing disk")

    def test_hard_link_to_original_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = fixture(root)
            record = prepare_system_runtime(root, manifest)
            disk = root / record["path"]
            disk.unlink()
            os.link(root / "system.raw", disk)
            manifest["artifacts"]["system_runtime_disk"] = record
            with self.assertRaisesRegex(ValueError, "provenance"):
                prepare_system_runtime(root, manifest)


if __name__ == "__main__":
    unittest.main()
