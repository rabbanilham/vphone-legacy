import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from ui_profile import (prepare_ui, import_profile, extract_sep, import_boot,
                        UI_ARGS, SEP_NAME, SEP_MEMBER, STAGES)
from ui_profile_oracle import field_diff
from qemu.boot_check import classify_boot, read_ppm, framebuffer_stage, REFERENCE
from prepare_diagnostic_disks import clone, seed_setup_done, seed_migration_preferences


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bundle = Path(self.temp.name)
        (self.bundle / 'system.raw').write_bytes(b'baseline')
        (self.bundle / 'data.raw').write_bytes(b'baseline data')
        self.manifest = {'firmware': {}, 'artifacts': {'system_disk': {'sha256': 'source'}}}
        self.path = self.bundle / 'vm_manifest.json'
        self.path.write_text(json.dumps(self.manifest))
        self.events, self.calls = [], []

    def runner(self, stage, bundle, work, source, ipsw):
        self.calls.append(stage)
        if stage == 'virtual-activation':
            (work / 'system-runtime.raw').write_bytes(b'changed!')
            (work / 'data.raw').write_bytes(b'seeded')
        if stage == 'boot-plan':
            boot = work / 'boot'
            boot.mkdir()
            (boot / 'KernelMemory.bin').write_bytes(b'image')
            (boot / 'boot_plan.json').write_text(json.dumps({'image': str(boot / 'KernelMemory.bin'),
                                                            'boot_args': UI_ARGS}))

    def test_pipeline_atomic_idempotent_and_force(self):
        result = prepare_ui(self.bundle, self.events.append, runner=self.runner)
        self.assertEqual(self.calls, list(STAGES[:-1]))
        self.assertEqual(result['profile'], 'ui')
        self.assertFalse(result['ui']['home_screen_verified'])
        plan = json.loads((self.bundle / result['plan']).read_text())
        self.assertTrue(Path(plan['image']).is_file())
        self.assertEqual((self.bundle / 'system.raw').read_bytes(), b'baseline')
        self.assertEqual((self.bundle / 'data.raw').read_bytes(), b'baseline data')
        self.calls.clear()
        prepare_ui(self.bundle, self.events.append, runner=self.runner)
        self.assertEqual(self.calls, [])
        second = prepare_ui(self.bundle, force=True, runner=self.runner)
        self.assertNotEqual(result['plan'], second['plan'])
        for event in self.events:
            self.assertEqual(set(event), {'schema', 'event', 'id', 'status', 'fraction', 'message', 'hint'})
            self.assertEqual(event['schema'], 1)
        self.assertEqual(self.events[-1]['status'], 'skipped')

    def test_failure_keeps_manifest_and_cleans(self):
        before = self.path.read_bytes()
        def fail(*args):
            raise RuntimeError('injected')
        with self.assertRaisesRegex(RuntimeError, 'injected'):
            prepare_ui(self.bundle, self.events.append, runner=fail)
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse(list(self.bundle.glob('.ui-preparing-*')))
        self.assertEqual(self.events[-1]['status'], 'error')

    def test_publish_failure_cleans_generation(self):
        before = self.path.read_bytes()
        with patch('ui_profile.os.replace', side_effect=OSError('injected')):
            with self.assertRaises(OSError):
                prepare_ui(self.bundle, runner=self.runner)
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(list((self.bundle / 'ui').iterdir()), [])

    def test_ui_records_satisfy_existing_validator_provenance(self):
        from bundle import validate_bundle
        from test_loader_runtime import kernel_fixture, tree_fixture
        from qemu.boot_image import MONITOR_VIRT_BASE
        from fw_prepare import monitor_boot_contract
        from patchers.patch_devicetree import parse_node
        from patchers.extract_ans import ANS_SHA256
        manifest = self.manifest
        manifest.update(schema_version=2, version='7.1.2', build='11D257', device='iPhone6,2',
                        board='n53ap', chip='s5l8960x', memory_mb=1024,
                        backend={'id': 's5l8960x'}, storage={})
        manifest['firmware']['monitor_boot_contract'] = monitor_boot_contract(parse_node(tree_fixture(), 0)[0])
        for name, data in [('kernel', kernel_fixture()), ('monitor', kernel_fixture(MONITOR_VIRT_BASE)),
                           ('devicetree', tree_fixture()), ('ans_firmware', bytes(0x39000)),
                           ('system_disk', b'baseline'), ('data_disk', b'baseline data')]:
            filename = {'system_disk': 'system.raw', 'data_disk': 'data.raw'}.get(name, name)
            (self.bundle / filename).write_bytes(data)
            manifest['artifacts'][name] = dict(path=filename, size=len(data),
                                             sha256=hashlib.sha256(data).hexdigest())
        self.path.write_text(json.dumps(manifest))
        prepare_ui(self.bundle, runner=self.runner)
        # Synthetic disk bytes cannot pass HFS checks and fake ANS is not firmware;
        # all other checks, specifically existing UI disk provenance, must pass.
        with patch('patchers.patch_rootfs.validate_raw_hfs', return_value={'synthetic': True}):
            report = validate_bundle(self.bundle, check_backend=False)
        self.assertEqual(len(report['errors']), 1)
        self.assertTrue(report['errors'][0].startswith('native ANS firmware:'))
        checks = {item['name']: item['ok'] for item in report['checks']}
        self.assertTrue(checks['system_runtime_disk integrity'])
        self.assertTrue(checks['data_disk integrity'])

    def test_lock(self):
        with (self.bundle / 'runtime.lock').open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, 'stopped'):
                prepare_ui(self.bundle, runner=self.runner)

    def test_import_uses_same_transaction(self):
        import_profile(self.bundle, '/profile', runner=self.runner)
        self.assertEqual(len(self.calls), 2)

    def test_sep_and_checksum(self):
        archive = self.bundle / 'firmware.ipsw'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr(SEP_MEMBER, b'sep')
        self.assertEqual(extract_sep(self.bundle, self.bundle / 'out', archive).read_bytes(), b'sep')
        self.manifest['firmware']['ipsw_sha256'] = 'wrong'
        self.path.write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(ValueError, 'checksum'):
            extract_sep(self.bundle, self.bundle / 'other', archive)

    def test_mini_cli_emits_only_contract_ndjson_on_failure(self):
        script = Path(__file__).resolve().parents[1] / 'scripts/ui_profile.py'
        result = subprocess.run([sys.executable, str(script), 'prepare-ui', str(self.bundle),
                                 '--progress-json'], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        finals = [e for e in events if e['event'] == 'result']
        self.assertEqual(len(finals), 1)
        self.assertFalse(finals[0]['ok'])
        self.assertTrue(all(e['schema'] == 1 for e in events))
        self.assertEqual(events[-2]['status'], 'error')

    def test_sep_missing(self):
        with self.assertRaisesRegex(ValueError, 'IPSW'):
            extract_sep(self.bundle, self.bundle / 'out')

    def test_clone_and_preferences(self):
        clone(self.bundle / 'data.raw', self.bundle / 'clone.raw')
        with self.assertRaises(ValueError):
            clone(self.bundle / 'data.raw', self.bundle / 'clone.raw')
        seed_setup_done(self.bundle / 'mount')
        seed_migration_preferences(self.bundle / 'mount')
        import plistlib
        prefs = self.bundle / 'mount/mobile/Library/Preferences'
        self.assertTrue(plistlib.loads((prefs / 'com.apple.purplebuddy.plist').read_bytes())['SetupDone'])
        self.assertEqual(plistlib.loads((prefs / 'com.apple.accountsd.plist').read_bytes())['LastMigrationSystemVersion'], '11D257')

    def test_boot_builder_sets_debug_and_smp_without_mutating_inputs(self):
        import struct
        from test_loader_runtime import kernel_fixture, tree_fixture
        from qemu.boot_image import MONITOR_VIRT_BASE
        from patchers.patch_devicetree import parse_node
        from ui_profile import build_boot
        for name, filename, data in (
                ('kernel', 'kernel', kernel_fixture()),
                ('monitor', 'monitor', kernel_fixture(MONITOR_VIRT_BASE)),
                ('devicetree', 'tree', tree_fixture()),
                ('ans_firmware', 'ans', b'not-used')):
            (self.bundle / filename).write_bytes(data)
            self.manifest['artifacts'][name] = {'path': filename}
        self.path.write_text(json.dumps(self.manifest))
        # ANS construction is independently covered by test_ans_firmware.py.
        from qemu.boot_image import build_image
        with patch('qemu.boot_image.build_image', side_effect=lambda k, t, o, args, **kw:
                   build_image(k, t, o, args, monitor_path=kw['monitor_path'],
                               debug_enabled=kw['debug_enabled'])):
            plan = build_boot(self.bundle, self.bundle / 'boot')
        tree = parse_node((self.bundle / 'boot/DeviceTree.runtime').read_bytes(), 0)[0]
        self.assertEqual(plan['cpu_count'], 2)
        self.assertEqual(plan['boot_args'], UI_ARGS)
        self.assertEqual(tree.find_node('chosen').get_prop('debug-enabled'), struct.pack('<I', 1))
        self.assertEqual((self.bundle / 'tree').read_bytes(), tree_fixture())

    def test_import_materializes_auxiliary(self):
        source, out = self.bundle / 'source', self.bundle / 'out'
        source.mkdir()
        for name in ('KernelMemory.bin', 'DeviceTree.runtime', 'boot_args.bin', 'ANS.bin'):
            (source / name).write_bytes(name.encode())
        sha = lambda name: hashlib.sha256(name.encode()).hexdigest()
        (source / 'boot_plan.json').write_text(json.dumps(dict(image_sha256=sha('KernelMemory.bin'),
            auxiliary_images=[dict(path='ANS.bin', sha256=sha('ANS.bin'))])))
        import_boot(source, out)
        plan = json.loads((out / 'boot_plan.json').read_text())
        self.assertTrue(Path(plan['auxiliary_images'][0]['path']).is_file())


class BootCheckTests(unittest.TestCase):
    def test_serial(self):
        for text, stage in [('', 'none'), ('Darwin Kernel Version', 'kernel'),
                            ('lockdownd SpringBoard', 'userspace'),
                            ('SpringBoard\npanic(cpu 0 caller 0x0): failed', 'panic'),
                            ('app: panic(cpu 0 caller 0x0): quoted', 'none')]:
            self.assertEqual(classify_boot(text), stage)

    def test_ppm_and_black_not_home(self):
        ppm = b'P6\n# QMP\n32 56\n255\n' + bytes(32*56*3)
        self.assertEqual(classify_boot('launchd', ppm), 'userspace')
        self.assertEqual(read_ppm(ppm)[:2], (32, 56))
        with self.assertRaises(ValueError):
            read_ppm(ppm[:-1])
        self.assertEqual(read_ppm(b'P6\n1 1\n255\n\n\x00\x00')[2], b'\n\x00\x00')

    def test_home_reference_and_false_positives(self):
        rgb = bytes(json.loads(REFERENCE.read_text())['rgb'])
        ppm = b'P6\n32 56\n255\n' + rgb
        self.assertEqual(classify_boot('', ppm), 'home')
        self.assertEqual(classify_boot('panic(cpu 0 caller 0x0): failed', ppm), 'panic')
        self.assertEqual(framebuffer_stage(32, 56, bytes([255])*(32*56*3)), 'none')
        self.assertEqual(framebuffer_stage(32, 56, bytes([15, 30, 80])*(32*56)), 'none')

    def test_lock_layout(self):
        rgb = bytearray(bytes([15, 40, 95]) * (32*56))
        # Stylized white clock and slide-label over wallpaper, with no icon grid.
        for y in (7, 8, 9, 48):
            for x in range(8, 24):
                rgb[(y*32+x)*3:(y*32+x)*3+3] = b'\xff\xff\xff'
        self.assertEqual(framebuffer_stage(32, 56, rgb), 'lockscreen')

    def test_public_evidence_home_and_settings(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest('Pillow only needed to decode archived PNG evidence')
        root = Path(__file__).resolve().parents[1] / 'research/evidence/public-readme-20261029'
        for name, expected in [('home', 'home'), ('settings', 'none')]:
            im = Image.open(root / (name + '.png')).convert('RGB').crop((82, 399, 706, 1507))
            self.assertEqual(framebuffer_stage(im.width, im.height, im.tobytes()), expected)

    def test_oracle_fields(self):
        diff = field_diff({'a': [1, 2]}, {'a': [1, 3]})
        self.assertEqual(diff, [dict(field='$.a[1]', left=2, right=3)])


@unittest.skipUnless(shutil.which('hdiutil'), 'macOS hdiutil required')
class SyntheticMountTests(unittest.TestCase):
    def test_mount_only_synthetic_eight_mb(self):
        from qemu.hfs_mount import mounted
        with tempfile.TemporaryDirectory() as temp:
            image = Path(temp) / 'synthetic.dmg'
            result = subprocess.run(['hdiutil', 'create', '-size', '8m', '-fs', 'HFS+',
                                     '-volname', 'UIUnit', str(image)], capture_output=True)
            if result.returncode and any(message in result.stderr for message in
                    (b'Device not configured', b'Operation not permitted', b'not supported')):
                self.skipTest('Sandbox cannot create synthetic HFS image: ' + result.stderr.decode().strip())
            result.check_returncode()
            with mounted(image, raw=False) as (_, mount):
                (mount / 'unit.txt').write_text('synthetic only')
            with mounted(image, readonly=True, raw=False) as (_, mount):
                self.assertEqual((mount / 'unit.txt').read_text(), 'synthetic only')


if __name__ == '__main__':
    unittest.main()
