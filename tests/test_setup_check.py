import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import setup_check
from cli_json import Emitter, parse_ndjson


class Setup(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        binary = self.root / 'backend/bin/qemu-system-aarch64'
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b'fake backend')
        (self.root / 'backend/backend.json').write_text(json.dumps({'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest()}))
        self.calls = []

    def runner(self, command, **kwargs):
        self.calls.append((command, kwargs))
        text = '/Library/Developer/CommandLineTools' if command == ['xcode-select', '-p'] else 'installed 1.0'
        return subprocess.CompletedProcess(command, 0, text, '')

    def check(self, **kwargs):
        return setup_check.check(self.root, self.root / 'VMs', runner=self.runner, exists=lambda p: 'host_wifi_scan' not in str(p), free_bytes=30 * 1024**3, **kwargs)

    def test_checks_and_optional_wifi(self):
        report = self.check()
        self.assertTrue(report['ok'])
        self.assertEqual(len(report['items']), 10)
        fixture = json.loads((ROOT / 'tests/fixtures/gui/setup-check.json').read_text())
        self.assertEqual(set(report), set(fixture))
        for item in report['items']:
            self.assertEqual(set(item), set(fixture['items'][0]))
        for command, kwargs in self.calls:
            self.assertEqual(kwargs['env']['PATH'], setup_check.SETUP_PATH)
            self.assertNotIn('install', command)
        self.assertEqual(sum('list' in cmd for cmd, _ in self.calls), len(setup_check.PACKAGES))

    def test_hash_and_space_failure(self):
        (self.root / 'backend/bin/qemu-system-aarch64').write_bytes(b'changed')
        report = setup_check.check(self.root, self.root / 'VMs', runner=self.runner, exists=lambda p: True, free_bytes=0)
        self.assertFalse(report['ok'])
        items = {i['id']: i for i in report['items']}
        self.assertEqual(items['backend']['status'], 'missing')
        self.assertEqual(items['disk-space']['status'], 'missing')

    def test_missing_packages(self):
        def runner(command, **kwargs):
            if 'list' in command and command[-1] in ('keystone', 'libslirp'):
                return subprocess.CompletedProcess(command, 1, '', '')
            return self.runner(command, **kwargs)
        report = setup_check.check(self.root, self.root, runner=runner, exists=lambda p: True, free_bytes=30 * 1024**3)
        packages = next(i for i in report['items'] if i['id'] == 'brew-packages')
        expected = json.loads((ROOT / 'tests/fixtures/gui/setup-check.json').read_text())['items'][2]
        self.assertEqual(packages, expected)

    def test_runner_oserror_is_missing(self):
        result = setup_check.run(['/no/such/executable'])
        self.assertEqual(result.returncode, 127)

    def test_install_streams_and_uses_structured_command(self):
        from unittest.mock import patch
        missing = dict(schema=1, ok=False, items=[dict(id='backend', title='QEMU backend', status='missing', detail='missing',
                        fix=dict(kind='auto', command='scripts/qemu/build_backend.py --fetch --install-build-deps', instructions=None))])
        ready = dict(schema=1, ok=True, items=[dict(missing['items'][0], status='ok', fix=None)])
        calls = []
        class Process:
            stdout = io.StringIO('fetching\nbuilding\n')
            def wait(self):
                return 0
        def popen(command, **kwargs):
            calls.append((command, kwargs))
            return Process()
        stream = io.StringIO()
        import contextlib
        with patch.object(setup_check, 'check', side_effect=[missing, ready]), contextlib.redirect_stderr(io.StringIO()):
            setup_check.install('backend', self.root, self.root, Emitter(stream), self.runner, popen)
        command, kwargs = calls[0]
        self.assertEqual(command, [str(self.root / '.venv/bin/python'), str(self.root / 'scripts/qemu/build_backend.py'), '--fetch', '--install-build-deps'])
        self.assertEqual(kwargs['env']['PATH'], setup_check.SETUP_PATH)
        events = parse_ndjson(stream.getvalue())
        self.assertEqual([e['event'] for e in events], ['stage', 'log', 'log', 'stage', 'result'])
        self.assertTrue(events[-1]['ok'])

    def test_install_manual_and_failed_recheck(self):
        from unittest.mock import patch
        manual = dict(schema=1, ok=False, items=[dict(id='homebrew', status='missing', fix=dict(kind='manual', instructions='Install Homebrew'))])
        with patch.object(setup_check, 'check', return_value=manual):
            with self.assertRaisesRegex(ValueError, 'Install Homebrew'):
                setup_check.install('homebrew', self.root, self.root)
            with self.assertRaisesRegex(ValueError, 'Unknown'):
                setup_check.install('not-an-id', self.root, self.root)
