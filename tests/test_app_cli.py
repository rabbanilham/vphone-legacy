"""`app` subcommands: manifest bookkeeping, AMFI opt-in and the launcher-lock guard."""
import fcntl
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'scripts/qemu'))

import app_cli  # noqa: E402
import app_install  # noqa: E402

UI_ARGS = ('-v serial=3 amfi_allow_any_signature=1 amfi_get_out_of_my_way=1 cs_enforcement_disable=1')


class AppCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bundle = Path(self.tmp.name) / 'vm'
        self.bundle.mkdir()
        for name in ('system-runtime.raw', 'data.raw'):
            (self.bundle / name).write_bytes(b'')
        self.write_manifest(UI_ARGS)
        self.report = {'bundle_id': 'com.vphone.hello', 'app_path': '/Applications/Hello.app',
                       'executable': 'Hello', 'architecture': 'arm64', 'signed': True,
                       'removed_cache': [], 'owners': 8}
        patches = [mock.patch.object(app_install, 'install_app', return_value=self.report),
                   mock.patch.object(app_install, 'remove_app', return_value={'removed': 'Hello.app'})]
        self.install_mock, self.remove_mock = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)

    def write_manifest(self, boot_args, apps=None):
        manifest = {'schema_version': 2, 'boot_args': boot_args, 'artifacts': {
            'system_runtime_disk': {'path': 'system-runtime.raw'}, 'data_disk': {'path': 'data.raw'}}}
        if apps is not None:
            manifest['apps'] = apps
        (self.bundle / 'vm_manifest.json').write_text(json.dumps(manifest))

    def manifest(self):
        return json.loads((self.bundle / 'vm_manifest.json').read_text())

    def test_install_records_app_and_replaces_duplicates(self):
        app_cli.install(self.bundle, Path('x.ipa'))
        app_cli.install(self.bundle, Path('x.ipa'))
        self.assertEqual(self.manifest()['apps'], [{'bundle_id': 'com.vphone.hello', 'path': '/Applications/Hello.app'}])
        self.assertEqual(app_cli.list_apps(self.bundle)[0]['bundle_id'], 'com.vphone.hello')

    def test_strict_vm_requires_opt_in_then_gets_relaxed_args(self):
        self.write_manifest('-v serial=3')
        with self.assertRaisesRegex(app_cli.AppError, '--enable-unsigned'):
            app_cli.install(self.bundle, Path('x.ipa'))
        self.install_mock.assert_not_called()
        app_cli.install(self.bundle, Path('x.ipa'), enable_unsigned=True)
        self.assertTrue(app_install.boot_args_ok(self.manifest()['boot_args']))
        self.assertTrue(self.manifest()['boot_args'].startswith('-v serial=3'))

    def test_refuses_while_launcher_lock_is_held(self):
        with open(self.bundle / 'runtime.lock', 'a+') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(app_cli.AppError, 'lock held'):
                app_cli.install(self.bundle, Path('x.ipa'))
            with self.assertRaisesRegex(app_cli.AppError, 'lock held'):
                app_cli.remove(self.bundle, 'com.vphone.hello')
        self.install_mock.assert_not_called()

    def test_remove_updates_manifest_and_rejects_unknown(self):
        self.write_manifest(UI_ARGS, [{'bundle_id': 'com.vphone.hello', 'path': '/Applications/Hello.app'}])
        app_cli.remove(self.bundle, 'com.vphone.hello')
        self.remove_mock.assert_called_once()
        self.assertEqual(self.remove_mock.call_args.args[2], 'Hello.app')
        self.assertEqual(self.manifest()['apps'], [])
        with self.assertRaisesRegex(app_cli.AppError, 'not recorded'):
            app_cli.remove(self.bundle, 'com.example.none')

    def test_pristine_system_image_is_rejected(self):
        manifest = self.manifest()
        manifest['artifacts'] = {'system_runtime_disk': {'path': 'system.raw'}, 'data_disk': {'path': 'data.raw'}}
        (self.bundle / 'system.raw').write_bytes(b'')
        (self.bundle / 'vm_manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(app_cli.AppError, 'immutable'):
            app_cli.install(self.bundle, Path('x.ipa'))


if __name__ == '__main__':
    unittest.main()
