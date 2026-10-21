"""Tests for diagnostic disk seeding and migration preference preparation."""
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from prepare_diagnostic_disks import seed_migration_preferences


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


if __name__ == "__main__":
    unittest.main()
