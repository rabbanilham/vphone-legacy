"""Offline app installer: copy, permissions, ownership and cache removal (mocked mounts)."""
from contextlib import contextmanager
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'scripts/qemu'))

import app_install  # noqa: E402
from hfs_mount import MountedVolume  # noqa: E402
from ipa import make_synthetic_ipa  # noqa: E402


class AppInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.system_dir, self.data_dir = root / 'system', root / 'data'
        (self.system_dir / 'Applications' / 'Calculator.app').mkdir(parents=True)
        cache = self.data_dir / 'mobile/Library/Caches'
        cache.mkdir(parents=True)
        (cache / 'com.apple.mobile.installation.plist').write_bytes(b'stale')
        (self.data_dir / 'db/lsd').mkdir(parents=True)
        (self.data_dir / 'db/lsd/com.apple.LaunchServices-123.csstore').write_bytes(b'stale')
        self.system_image, self.data_image = root / 'system.raw', root / 'data.raw'
        self.system_image.write_bytes(b'')
        self.data_image.write_bytes(b'')
        self.ipa = root / 'app.ipa'
        make_synthetic_ipa(self.ipa)
        dirs = {str(self.system_image): self.system_dir, str(self.data_image): self.data_dir}

        @contextmanager
        def fake_mounted(image, readonly=False, **kwargs):
            yield MountedVolume('/dev/diskX', dirs[str(image)])
        self.owners = mock.Mock()
        patches = [mock.patch.object(app_install, 'mounted', fake_mounted),
                   mock.patch.object(app_install, 'set_catalog_owner_many', self.owners),
                   mock.patch.object(app_install, 'images_in_use', return_value=False),
                   mock.patch.object(app_install, 'adhoc_sign', return_value=True)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_install_copies_app_sets_modes_owners_and_clears_caches(self):
        report = app_install.install_app(self.system_image, self.data_image, self.ipa)
        app = self.system_dir / report['app_path'].lstrip('/')
        self.assertTrue(app.is_dir())
        self.assertNotEqual(app.name, 'Calculator.app')
        executable = app / report['executable']
        self.assertEqual(executable.stat().st_mode & 0o777, 0o755)
        plist = app / 'Info.plist'
        self.assertEqual(plist.stat().st_mode & 0o777, 0o644)
        self.assertEqual(report['app_path'], f'/Applications/{app.name}')
        self.assertTrue(report['signed'])
        # Catalog ownership is applied after the volume is detached: root:admin, whole bundle.
        (image, owners), _ = self.owners.call_args
        self.assertEqual(image, self.system_image)
        self.assertEqual(set(owners.values()), {(0, app_install.ADMIN_GID)})
        self.assertEqual(len(owners), report['owners'])
        self.assertFalse((self.data_dir / 'mobile/Library/Caches/com.apple.mobile.installation.plist').exists())
        self.assertFalse(list((self.data_dir / 'db/lsd').glob('*.csstore')))
        self.assertEqual(len(report['removed_cache']), 2)

    def test_reinstall_replaces_existing_bundle(self):
        report = app_install.install_app(self.system_image, self.data_image, self.ipa)
        app = self.system_dir / report['app_path'].lstrip('/')
        (app / 'leftover').write_text('x')
        app_install.install_app(self.system_image, self.data_image, self.ipa)
        self.assertFalse((app / 'leftover').exists())

    def test_refuses_images_in_use(self):
        with mock.patch.object(app_install, 'images_in_use', return_value=True):
            with self.assertRaisesRegex(app_install.InstallError, 'stop the VM'):
                app_install.install_app(self.system_image, self.data_image, self.ipa)
        self.owners.assert_not_called()

    def test_rejects_bad_ipa_and_missing_images(self):
        bad = Path(self.tmp.name) / 'bad.ipa'
        bad.write_bytes(b'not a zip')
        with self.assertRaisesRegex(app_install.InstallError, 'Invalid IPA'):
            app_install.install_app(self.system_image, self.data_image, bad)
        with self.assertRaisesRegex(app_install.InstallError, 'Missing image'):
            app_install.install_app(Path(self.tmp.name) / 'nope.raw', self.data_image, self.ipa)

    def test_boot_args_gate(self):
        self.assertTrue(app_install.boot_args_ok('-v serial=3 amfi_allow_any_signature=1 '
                                                 'amfi_get_out_of_my_way=1 cs_enforcement_disable=1'))
        self.assertFalse(app_install.boot_args_ok('-v serial=3'))
        self.assertFalse(app_install.boot_args_ok(None))


if __name__ == '__main__':
    unittest.main()
