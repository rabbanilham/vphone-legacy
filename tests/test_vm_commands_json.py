import contextlib
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from setup_vm import cleanup_stale, disk_bytes, lock_state
from cli_json import parse_ndjson
spec = importlib.util.spec_from_file_location('gui_cli', ROOT / 'sources/vphone-legacy-cli/main.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
FIXTURES = Path(__file__).parent / 'fixtures/gui'


class VMCommands(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, VPHONE_LEGACY_VMS_DIR=str(self.root))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.bundle = self.root / 'ios712'
        self.bundle.mkdir()
        self.example = json.loads((FIXTURES / 'vm-list.json').read_text())['vms'][0]
        self.manifest = dict(version='7.1.2', build='11D257', device='iPhone6,2', profile='ui', memory_mb=1024, apps=self.example['apps'], schema_version=2)
        (self.bundle / 'vm_manifest.json').write_text(json.dumps(self.manifest))

    def invoke(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(list(args))
        return code, json.loads(out.getvalue())

    def test_list_fixture_fields(self):
        code, output = self.invoke('vm', 'list', '--json')
        self.assertEqual(code, 0)
        self.assertEqual(set(output), {'schema', 'root', 'vms'})
        actual = output['vms'][0]
        self.assertEqual(set(actual), set(self.example))
        for field in ('name', 'ios', 'build', 'device', 'profile', 'memory_mb', 'apps', 'phase', 'pid', 'boot_stage'):
            self.assertEqual(actual[field], self.example[field])
        self.assertEqual(actual['disk_bytes'], disk_bytes(self.bundle))

    def test_status_fixture_and_lock(self):
        expected = json.loads((FIXTURES / 'vm-status.json').read_text())
        runtime = {k: v for k, v in expected.items() if k != 'lock'}
        (self.bundle / 'runtime.json').write_text(json.dumps(runtime))
        with (self.bundle / 'runtime.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.invoke('vm', 'status', 'ios712', '--json'), (0, expected))
            code, error = self.invoke('vm', 'delete', 'ios712', '--json')
            self.assertEqual(code, 1)
            self.assertIn('hint', error)
            self.assertTrue(self.bundle.exists())
        self.assertEqual(lock_state(self.bundle), 'free')

    def test_no_runtime_delete_and_traversal(self):
        code, status = self.invoke('vm', 'status', 'ios712', '--json')
        self.assertEqual(status['boot_stage'], 'none')
        self.assertEqual(status['lock'], 'free')
        self.assertEqual(self.invoke('vm', 'delete', '../escape', '--json')[0], 1)
        code, value = self.invoke('vm', 'delete', 'ios712', '--json')
        self.assertEqual(code, 0)
        self.assertFalse(self.bundle.exists())
        self.assertEqual(value['data']['name'], 'ios712')

    def test_stale_cleanup_preserves_active(self):
        stale = self.root / 'old.preparing-abc'
        stale.mkdir()
        os.utime(stale, (0, 0))
        active = self.root / 'active.preparing-abc'
        active.mkdir()
        with (active / 'runtime.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.invoke('vm', 'list', '--json')
            self.assertFalse(stale.exists())
            self.assertTrue(active.exists())
        cleanup_stale(self.root)
        self.assertFalse(active.exists())

    def test_allocated_blocks_deduplicates_hardlinks(self):
        file = self.bundle / 'disk'
        file.write_bytes(b'x' * 8192)
        before = disk_bytes(self.bundle)
        os.link(file, self.bundle / 'hardlink')
        self.assertEqual(disk_bytes(self.bundle), before)

class FirmwareProgress(unittest.TestCase):
    def test_failed_create_ndjson_and_hint(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/fw_prepare.py'), '--version', '7.1.2',
                                     '--ipsw', str(Path(tmp) / 'missing.ipsw'), '--out-dir', str(Path(tmp) / 'new'), '--progress-json'],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            events = parse_ndjson(result.stdout)
            self.assertEqual(events[0]['id'], 'preflight')
            self.assertEqual(events[-1], {'schema': 1, 'event': 'result', 'ok': False, 'data': {'exit_code': 1}})
            self.assertTrue(events[-2]['hint'])
            self.assertFalse((Path(tmp) / 'new').exists())

    def test_force_refuses_held_runtime_lock(self):
        import fw_prepare
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / 'vm'
            bundle.mkdir()
            ipsw = Path(tmp) / 'input.ipsw'
            ipsw.write_bytes(b'fake')
            with (bundle / 'runtime.lock').open('w') as lock, patch.object(fw_prepare, 'resolve_firmware', return_value={'device': 'iPhone6,2'}):
                fcntl.flock(lock, fcntl.LOCK_EX)
                with self.assertRaisesRegex(ValueError, 'runtime.lock'):
                    fw_prepare.prepare_firmware('7.1.2', ipsw, bundle, force=True)
                self.assertTrue(bundle.exists())

    def test_extraction_byte_fraction(self):
        import fw_prepare
        import zipfile
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'fixture.zip'
            data = b'x' * (1024 * 1024 + 10)
            with zipfile.ZipFile(archive, 'w') as stream:
                stream.writestr('component', data)
            fractions = []
            with zipfile.ZipFile(archive) as stream:
                fw_prepare.extract_component(stream, 'component', Path(tmp) / 'output', fractions.append)
            self.assertGreater(len(fractions), 1)
            self.assertLess(fractions[0], 1)
            self.assertEqual(fractions[-1], 1)

    def test_all_stages_without_real_firmware(self):
        import fw_prepare
        import plistlib
        import zipfile
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory() as tmp:
            ipsw = Path(tmp) / 'fake.ipsw'
            fields = {'kernelcache_path': 'kernel', 'devicetree_path': 'tree', 'iboot_path': 'iboot', 'rootfs_dmg': 'rootfs'}
            meta = dict(fields, build='11D257', device='iPhone6,2', board='n53ap', chip='s5l8960x',
                        kernel_iv='', kernel_key='', iboot_iv='', iboot_key='', devicetree_iv='', devicetree_key='', rootfs_key='')
            identity = {'Manifest': {key: {} for key in ('KernelCache', 'DeviceTree', 'OS', 'iBoot')}, 'Info': {}}
            with zipfile.ZipFile(ipsw, 'w') as stream:
                stream.writestr('BuildManifest.plist', plistlib.dumps({}))
                for name in fields.values():
                    stream.writestr(name, b'fake component')
            output = Path(tmp) / 'vm'
            events = io.StringIO()
            def disk(source, destination):
                Path(destination).write_bytes(b'fake disk')
                return {'filesystem': 'hfsx'}
            def decrypt(source, destination, *args):
                Path(destination).write_bytes(b'decrypted')
            def data(destination):
                Path(destination).write_bytes(b'data')
                return {}
            from cli_json import Emitter, StageID
            mocks = dict(resolve_firmware=lambda *a: meta, select_identity=lambda *a: identity,
                         decode_kernel_components=lambda *a: (b'kernel', b'monitor'), inspect_macho=lambda *a: {},
                         decode_ans_components=lambda *a: (b'iboot', b'ans', {}), decode_devicetree=lambda *a: (b'tree', None),
                         monitor_boot_contract=lambda *a: {}, ensure_vfdecrypt=lambda *a: 'fake-helper',
                         vfdecrypt_dmg=decrypt, convert_udif_to_raw=disk, create_data_volume=data)
            with ExitStack() as stack:
                for name, function in mocks.items():
                    stack.enter_context(patch.object(fw_prepare, name, function))
                with contextlib.redirect_stdout(io.StringIO()):
                    fw_prepare.prepare_firmware('7.1.2', ipsw, output, emitter=Emitter(events))
            parsed = parse_ndjson(events.getvalue())
            self.assertEqual([e['id'] for e in parsed if e['event'] == 'stage' and e['status'] == 'done'], [stage.value for stage in StageID])
            self.assertEqual(parsed[-1]['data']['path'], str(output.resolve()))
            self.assertEqual(lock_state(output), 'free')
            self.assertFalse(list(Path(tmp).glob('*.preparing-*')))
