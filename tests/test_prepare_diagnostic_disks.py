"""Tests for diagnostic disk seeding, setup-done variant, and migration preferences."""
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from prepare_diagnostic_disks import (
    seed_migration_preferences,
    seed_setup_done,
)


class TestPrepareDiagnosticDisks(unittest.TestCase):
    def test_seed_migration_preferences_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_mount = Path(tmpdir)
            seed_migration_preferences(data_mount, "11D257")

            for home in ("mobile", "root"):
                prefs_dir = data_mount / home / "Library/Preferences"
                self.assertTrue(prefs_dir.is_dir(), f"{prefs_dir} should exist")

                # Check com.apple.migration.plist
                mig_path = prefs_dir / "com.apple.migration.plist"
                self.assertTrue(mig_path.is_file(), f"{mig_path} should exist")
                mig_data = plistlib.loads(mig_path.read_bytes())
                self.assertEqual(mig_data.get("LastSystemVersion"), "11D257")
                self.assertEqual(mig_data.get("PreMigrationSystemVersion"), "11D257")

                # Check com.apple.accountsd.plist
                acct_path = prefs_dir / "com.apple.accountsd.plist"
                self.assertTrue(acct_path.is_file(), f"{acct_path} should exist")
                acct_data = plistlib.loads(acct_path.read_bytes())
                self.assertEqual(acct_data.get("LastMigrationSystemVersion"), "11D257")

    def test_seed_migration_preferences_custom_version(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_mount = Path(tmpdir)
            seed_migration_preferences(data_mount, "11D201")

            for home in ("mobile", "root"):
                prefs_dir = data_mount / home / "Library/Preferences"
                mig_path = prefs_dir / "com.apple.migration.plist"
                mig_data = plistlib.loads(mig_path.read_bytes())
                self.assertEqual(mig_data.get("LastSystemVersion"), "11D201")
                self.assertEqual(mig_data.get("PreMigrationSystemVersion"), "11D201")

                acct_path = prefs_dir / "com.apple.accountsd.plist"
                acct_data = plistlib.loads(acct_path.read_bytes())
                self.assertEqual(acct_data.get("LastMigrationSystemVersion"), "11D201")

    def test_seed_setup_done_binary_plist_and_types(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_mount = Path(tmpdir)
            seed_setup_done(data_mount)

            mobile_pb = data_mount / "mobile/Library/Preferences/com.apple.purplebuddy.plist"
            mobile_nb = data_mount / "mobile/Library/Preferences/com.apple.purplebuddy.notbackedup.plist"
            root_pb = data_mount / "root/Library/Preferences/com.apple.purplebuddy.plist"

            for path in (mobile_pb, mobile_nb, root_pb):
                self.assertTrue(path.is_file(), f"{path} should exist")
                raw = path.read_bytes()
                self.assertTrue(raw.startswith(b"bplist00"), f"{path} should be a binary plist")

            mobile_data = plistlib.loads(mobile_pb.read_bytes())
            self.assertIs(type(mobile_data["SetupVersion"]), int)
            self.assertEqual(mobile_data["SetupVersion"], 5)
            for bool_key in ("SetupDone", "SetupFinishedAllSteps", "AssistantPresented", "ForceNoBuddy"):
                self.assertIs(type(mobile_data[bool_key]), bool)
                self.assertIs(mobile_data[bool_key], True)

            root_data = plistlib.loads(root_pb.read_bytes())
            self.assertIs(type(root_data["SetupVersion"]), int)
            self.assertEqual(root_data["SetupVersion"], 5)
            for bool_key in ("SetupDone", "SetupFinishedAllSteps", "AssistantPresented", "ForceNoBuddy"):
                self.assertIs(type(root_data[bool_key]), bool)
                self.assertIs(root_data[bool_key], True)

            nb_data = plistlib.loads(mobile_nb.read_bytes())
            self.assertIs(type(nb_data["CloudConfigPresented"]), bool)
            self.assertIs(nb_data["CloudConfigPresented"], True)
            self.assertIs(type(nb_data["LocationServices2Presented"]), bool)
            self.assertIs(nb_data["LocationServices2Presented"], True)
            self.assertIs(type(nb_data["ShouldRunKeychainSyncMiniBuddy"]), bool)
            self.assertIs(nb_data["ShouldRunKeychainSyncMiniBuddy"], False)
            self.assertIs(type(nb_data["ShouldRunMesaMiniBuddy"]), bool)
            self.assertIs(nb_data["ShouldRunMesaMiniBuddy"], False)

    def test_seed_setup_done_exact_payloads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_mount = Path(tmpdir)
            seed_setup_done(data_mount)

            expected_purplebuddy = {
                "SetupDone": True,
                "SetupFinishedAllSteps": True,
                "SetupVersion": 5,
                "AssistantPresented": True,
                "SetupState": "SetupUsingAssistant",
                "ForceNoBuddy": True,
            }
            expected_notbackedup = {
                "CloudConfigPresented": True,
                "LocationServices2Presented": True,
                "ShouldRunKeychainSyncMiniBuddy": False,
                "ShouldRunMesaMiniBuddy": False,
            }

            mobile_pb = data_mount / "mobile/Library/Preferences/com.apple.purplebuddy.plist"
            mobile_nb = data_mount / "mobile/Library/Preferences/com.apple.purplebuddy.notbackedup.plist"
            root_pb = data_mount / "root/Library/Preferences/com.apple.purplebuddy.plist"
            root_nb = data_mount / "root/Library/Preferences/com.apple.purplebuddy.notbackedup.plist"

            self.assertEqual(plistlib.loads(mobile_pb.read_bytes()), expected_purplebuddy)
            self.assertEqual(plistlib.loads(root_pb.read_bytes()), expected_purplebuddy)
            self.assertEqual(plistlib.loads(mobile_nb.read_bytes()), expected_notbackedup)
            self.assertEqual(mobile_pb.read_bytes(), root_pb.read_bytes())
            self.assertFalse(root_nb.exists(), "root should not have com.apple.purplebuddy.notbackedup.plist")

    def test_seed_setup_done_no_root_lockdown_data_ark_creation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_mount = Path(tmpdir)
            seed_setup_done(data_mount)

            self.assertFalse((data_mount / "root/Library/Lockdown/data_ark.plist").exists())
            self.assertFalse((data_mount / "root/Library/Lockdown").exists())
            self.assertFalse((data_mount / "Library/Lockdown/data_ark.plist").exists())
            self.assertFalse((data_mount / "Library/Lockdown").exists())
            self.assertFalse((data_mount / "mobile/Library/Lockdown/data_ark.plist").exists())
            self.assertFalse((data_mount / "mobile/Library/Lockdown").exists())
            self.assertEqual(list(data_mount.rglob("data_ark.plist")), [])

    def test_seed_setup_done_preserves_sentinel_lockdown_and_migration_prefs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_mount = Path(tmpdir)
            seed_migration_preferences(data_mount, "11D257")

            # Setup sentinel Lockdown/data_ark.plist files
            root_lockdown_dir = data_mount / "root/Library/Lockdown"
            root_lockdown_dir.mkdir(parents=True, exist_ok=True)
            root_data_ark = root_lockdown_dir / "data_ark.plist"
            sentinel_root_bytes = b"\xde\xad\xbe\xef--SENTINEL_ROOT_DATA_ARK--\xfe\xed\xfa\xce"
            root_data_ark.write_bytes(sentinel_root_bytes)

            global_lockdown_dir = data_mount / "Library/Lockdown"
            global_lockdown_dir.mkdir(parents=True, exist_ok=True)
            global_data_ark = global_lockdown_dir / "data_ark.plist"
            sentinel_global_bytes = b"\xca\xfe\xba\xbe--SENTINEL_GLOBAL_DATA_ARK--\x01\x02\x03\x04"
            global_data_ark.write_bytes(sentinel_global_bytes)

            # Record migration preference snapshots
            migration_files = [
                data_mount / "mobile/Library/Preferences/com.apple.migration.plist",
                data_mount / "mobile/Library/Preferences/com.apple.accountsd.plist",
                data_mount / "root/Library/Preferences/com.apple.migration.plist",
                data_mount / "root/Library/Preferences/com.apple.accountsd.plist",
            ]
            snapshot_before = {p: p.read_bytes() for p in migration_files}
            for p in migration_files:
                self.assertTrue(p.is_file())

            seed_setup_done(data_mount)

            # Verify sentinel data_ark.plist files remain untouched
            self.assertTrue(root_data_ark.is_file())
            self.assertEqual(root_data_ark.read_bytes(), sentinel_root_bytes)
            self.assertTrue(global_data_ark.is_file())
            self.assertEqual(global_data_ark.read_bytes(), sentinel_global_bytes)

            # Verify migration preferences remain untouched
            for p, expected_bytes in snapshot_before.items():
                self.assertTrue(p.is_file(), f"{p} should still exist")
                self.assertEqual(p.read_bytes(), expected_bytes, f"{p} content should be preserved")

    def test_seed_setup_done_guards_activation_state_boundary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_mount = Path(tmpdir)
            seed_setup_done(data_mount)

            # Exactly three files should be written by seed_setup_done
            all_files = sorted(str(p.relative_to(data_mount)) for p in data_mount.rglob("*") if p.is_file())
            expected_files = sorted([
                "mobile/Library/Preferences/com.apple.purplebuddy.plist",
                "mobile/Library/Preferences/com.apple.purplebuddy.notbackedup.plist",
                "root/Library/Preferences/com.apple.purplebuddy.plist",
            ])
            self.assertEqual(all_files, expected_files)

            # Guard that no activation keys or state markers are introduced
            activation_keys = {
                "ActivationState",
                "ActivationStateAcknowledged",
                "DeviceActivated",
                "UniqueDeviceID",
                "WildcardTicket",
                "activation-record",
                "ActivationRecord",
            }
            for rel_path in expected_files:
                plist_path = data_mount / rel_path
                data = plistlib.loads(plist_path.read_bytes())
                for key in data:
                    self.assertNotIn(key, activation_keys)
                    self.assertNotIn("activation", key.lower())

            # Guard against any Lockdown directory or activation-related paths
            self.assertFalse((data_mount / "root/Library/Lockdown").exists())
            self.assertFalse((data_mount / "mobile/Library/Lockdown").exists())
            self.assertFalse((data_mount / "Library/Lockdown").exists())


if __name__ == "__main__":
    unittest.main()
