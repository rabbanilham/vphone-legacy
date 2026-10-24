"""Missing research artifacts must fail before cloning multi-gigabyte disks."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
spec = importlib.util.spec_from_file_location('public_live_run', ROOT / 'backend/live_run.py')
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)


class ResearchProfilePreflightTests(unittest.TestCase):
    def check_missing(self, args, expected):
        error = io.StringIO()
        with patch.object(sys, 'argv', ['live_run.py', *args]), \
                patch.object(live.tempfile, 'mkdtemp') as create, \
                patch.object(live, 'clone_or_copy') as clone, \
                contextlib.redirect_stderr(error), self.assertRaises(SystemExit) as exit:
            live.main()
        self.assertEqual(exit.exception.code, 2)
        self.assertIn(expected, error.getvalue())
        create.assert_not_called()
        clone.assert_not_called()

    def test_missing_plan(self):
        self.check_missing(['--plan', '/nonexistent/vphone-public/plan.json'], 'Missing input:')

    def test_missing_auxiliary_image_before_disk_clones(self):
        with tempfile.TemporaryDirectory(prefix='profile with spaces ') as folder:
            root = Path(folder)
            profile = root / 'profile'
            profile.mkdir()
            boot = profile / 'boot'
            boot.mkdir()
            storage = profile / 'storage-before'
            storage.mkdir()
            for name in ('system.raw', 'data.raw', 'kernel.bin', 'qemu'):
                (root / name).touch()
            (storage / 'utility.raw').touch()
            missing = root / 'missing-aux.bin'
            plan = boot / 'boot_plan.json'
            plan.write_text(json.dumps({'image': str(root / 'kernel.bin'),
                                        'auxiliary_images': [{'path': str(missing), 'address': 0}]}))
            self.check_missing(['--plan', str(plan), '--system', str(root / 'system.raw'),
                                '--data', str(root / 'data.raw'), '--qemu-binary', str(root / 'qemu')],
                               str(missing))


if __name__ == '__main__':
    unittest.main()
