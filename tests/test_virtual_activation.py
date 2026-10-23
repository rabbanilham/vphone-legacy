"""Exact-firmware regression checks; proprietary fixtures are never committed."""
import hashlib
import json
import plistlib
from pathlib import Path
import struct
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from patchers.patch_virtual_activation import patch, OFFSET, ORIGINAL, REPLACEMENT
from prepare_virtual_activation import prepare_boot, patch_job_cache, LABEL, CACHE_OFFSET, CACHE_SIZE
from patchers.patch_devicetree import parse_node
from qemu.hfs_ownership import set_catalog_owner
from patchers.sign_diagnostic_probe import sign_probe, ENTITLEMENTS
sys.path.insert(0, str(ROOT / "backend"))
from collect_virtual_activation import summarize

INPUT = ROOT / "backend/evidence/virtual-activated-20261023/inputs/lockdownd.original"
PLAN = ROOT / "backend/evidence/keybag-contract-20261015/boot-plan-smp-nooscar/boot_plan.json"
CACHE = ROOT / "backend/evidence/ca-surface-20261014-214745/dyld_shared_cache_arm64"


class VirtualActivationTests(unittest.TestCase):
    def test_truncated_and_unknown_binary_are_rejected(self):
        for data in (b"", bytes(OFFSET + 4), bytes(OFFSET + 100)):
            with self.subTest(length=len(data)), self.assertRaises(ValueError):
                patch(data)

    @unittest.skipUnless(INPUT.exists(), "Local 11D257 lockdownd fixture required")
    def test_only_audited_instruction_is_changed(self):
        original = INPUT.read_bytes()
        result, manifest = patch(original)
        self.assertEqual(original[OFFSET:OFFSET + 4], ORIGINAL)
        self.assertEqual(result[OFFSET:OFFSET + 4], REPLACEMENT)
        self.assertEqual(result[:OFFSET], original[:OFFSET])
        self.assertEqual(result[OFFSET + 4:], original[OFFSET + 4:])
        self.assertEqual(manifest["patched_sha256"], hashlib.sha256(result).hexdigest())
        self.assertFalse(manifest["runtime_verified"])

    @unittest.skipUnless(INPUT.exists(), "Local 11D257 lockdownd fixture required")
    def test_already_patched_and_modified_input_are_rejected(self):
        original = INPUT.read_bytes()
        result, _ = patch(original)
        with self.assertRaisesRegex(ValueError, "already patched"):
            patch(result)
        for offset in (0, OFFSET, len(original) - 1):
            changed = bytearray(original)
            changed[offset] ^= 1
            with self.subTest(offset=offset), self.assertRaisesRegex(ValueError, "SHA-256"):
                patch(bytes(changed))

    @unittest.skipUnless(CACHE.exists(), "Local 11D257 shared-cache fixture required")
    def test_probe_cache_addition_preserves_all_existing_jobs(self):
        with CACHE.open("rb") as f:
            f.seek(CACHE_OFFSET)
            source = f.read(CACHE_SIZE)
        before = plistlib.loads(source)
        result = patch_job_cache(source)
        after = plistlib.loads(result)
        self.assertEqual(len(result), len(source))
        jobs = after["LaunchDaemons"]
        for name, job in before["LaunchDaemons"].items():
            self.assertEqual(jobs[name], job)
        self.assertEqual(len(jobs), len(before["LaunchDaemons"]) + 1)
        self.assertEqual(jobs[f"/System/Library/LaunchDaemons/{LABEL}.plist"]["Label"], LABEL)
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            patch_job_cache(result)

    @unittest.skipUnless(PLAN.exists(), "Local boot plan required")
    def test_boot_args_and_embedded_tree_are_consistent(self):
        source_plan = json.loads(PLAN.read_text())
        original = Path(source_plan["image"]).read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "boot"
            prepare_boot(PLAN, out)
            plan = json.loads((out / "boot_plan.json").read_text())
            image = (out / "KernelMemory.bin").read_bytes()
            ba = plan["boot_args_pa"] - plan["dram_base"]
            self.assertEqual(image[ba + 108:ba + 716].split(b"\0")[0].decode(), plan["boot_args"])
            tree = (out / "DeviceTree.runtime").read_bytes()
            dt = plan["devicetree_pa"] - plan["dram_base"]
            self.assertEqual(image[dt:dt + len(tree)], tree)
            self.assertEqual(struct.unpack_from("<I", image, ba + 104)[0], len(tree))
            root, _ = parse_node(tree)
            chosen = root.find_node("chosen")
            self.assertEqual(chosen.get_prop("debug-enabled"), b"\x01\0\0\0")
            self.assertEqual(chosen.get_prop("boot-args"), plan["boot_args"].encode() + b"\0")
            self.assertIn(b"boot-args=" + plan["boot_args"].encode() + b"\0",
                          chosen.get_prop("nvram-proxy-data"))
            self.assertEqual(image[:ba], original[:ba])
        self.assertEqual(Path(source_plan["image"]).read_bytes(), original)

    def test_native_api_evidence_requires_restart_and_disk_agreement(self):
        rows = [{"sample": i, "valid": True, "ActivationState": "Activated",
                 "BrickState": False, "after_restart": i > 0} for i in (0, 1)]
        state = {"-BrickState": False, "com.apple.mobile.lockdown_cache-ActivationState": "Activated"}
        result = summarize(rows, state, "PID = 40;", "PID = 90;")
        self.assertTrue(result["activation_checks_pass"])
        self.assertFalse(result["home_screen_verified"])
        self.assertFalse(summarize(rows, state, "PID = 40;", "PID = 40;")["activation_checks_pass"])
        self.assertFalse(summarize(rows, {}, "PID = 40;", "PID = 90;")["activation_checks_pass"])
        # A failed latest boot must not borrow passing evidence from an older boot.
        rows.append({"sample": 0, "valid": False})
        self.assertFalse(summarize(rows, state, "PID = 40;", "PID = 90;")["activation_checks_pass"])

    def test_guest_ownership_changes_only_catalog_bsd_ids(self):
        image = bytearray(5120)
        image[1024:1026] = b"H+"
        struct.pack_into(">I", image, 1028, 0x100)  # cleanly unmounted
        struct.pack_into(">I", image, 1064, 512)
        struct.pack_into(">II", image, 1024 + 288, 8, 2)
        struct.pack_into(">I", image, 4096 + 24, 1)  # first leaf
        struct.pack_into(">H", image, 4096 + 32, 512)
        leaf = 4608
        image[leaf + 8] = 0xff
        struct.pack_into(">H", image, leaf + 10, 1)
        struct.pack_into(">H", image, leaf + 510, 14)
        struct.pack_into(">HIH", image, leaf + 14, 16, 2, 5)
        image[leaf + 22:leaf + 32] = "probe".encode("utf-16-be")
        struct.pack_into(">h", image, leaf + 32, 2)
        struct.pack_into(">I", image, leaf + 40, 123)
        struct.pack_into(">II", image, leaf + 64, 501, 20)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "system.raw"
            path.write_bytes(image)
            result = set_catalog_owner(path, 123)
            self.assertEqual(result["name"], "probe")
            expected = bytearray(image)
            expected[leaf + 64:leaf + 72] = bytes(8)
            self.assertEqual(path.read_bytes(), expected)
            with self.assertRaisesRegex(ValueError, "exactly one"):
                set_catalog_owner(path, 124)
            dirty = bytearray(expected)
            struct.pack_into(">I", dirty, 1028, 0)
            path.write_bytes(dirty)
            with self.assertRaisesRegex(ValueError, "not cleanly unmounted"):
                set_catalog_owner(path, 123)
            self.assertEqual(path.read_bytes(), dirty)

    def test_diagnostic_signature_hashes_and_read_only_entitlements(self):
        source = bytearray(0x9000)
        struct.pack_into("<8I", source, 0, 0xfeedfacf, 0x100000c, 0, 2, 2, 144, 0, 0)
        for index, name in enumerate((b"__TEXT", b"__LINKEDIT")):
            offset = 32 + index * 72
            struct.pack_into("<II16s4Q4I", source, offset, 0x19, 72, name,
                             0x100000000 + index * 0x8000, 0x8000 if index == 0 else 0x1000,
                             index * 0x8000, 0x8000 if index == 0 else 0x1000, 5, 5, 0, 0)
        signed = sign_probe(bytes(source))
        sig_offset, sig_size = struct.unpack_from("<II", signed, 32 + 144 + 8)
        magic, length, count, _, cd_offset, _, ent_offset = struct.unpack_from(">7I", signed, sig_offset)
        self.assertEqual((magic, length, count), (0xfade0cc0, sig_size, 2))
        cd_start = sig_offset + cd_offset
        fields = struct.unpack_from(">9I", signed, cd_start)
        hash_offset, specials, slots, limit = fields[4], fields[6], fields[7], fields[8]
        self.assertEqual(specials, 5)
        self.assertEqual(limit, sig_offset)
        for slot in range(slots):
            expected = hashlib.sha1(signed[slot * 4096:min((slot + 1) * 4096, limit)]).digest()
            offset = cd_start + hash_offset + slot * 20
            self.assertEqual(signed[offset:offset + 20], expected)
        ent_start = sig_offset + ent_offset
        ent_size = struct.unpack_from(">I", signed, ent_start + 4)[0]
        ent_blob = signed[ent_start:ent_start + ent_size]
        self.assertEqual(plistlib.loads(ent_blob[8:]), ENTITLEMENTS)
        self.assertEqual(signed[cd_start + hash_offset - 100:cd_start + hash_offset - 80],
                         hashlib.sha1(ent_blob).digest())
        with self.assertRaisesRegex(ValueError, "already signed"):
            sign_probe(signed)


if __name__ == "__main__":
    unittest.main()
