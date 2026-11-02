from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from qemu.hfs_mount import mounted, attach, detach


def response(entities):
    return subprocess.CompletedProcess([], 0, plistlib.dumps({'system-entities': entities}))


class MountTests(unittest.TestCase):
    @patch('qemu.hfs_mount.subprocess.run')
    def test_body_failure_detaches(self, run):
        run.side_effect = [response([{'dev-entry': '/dev/disk42'},
            {'dev-entry': '/dev/disk42s1', 'mount-point': '/Volumes/With spaces'}]),
            subprocess.CompletedProcess([], 0)]
        with self.assertRaisesRegex(RuntimeError, 'body'):
            with mounted('test.raw', readonly=True) as volume:
                self.assertEqual(volume.mount_point, Path('/Volumes/With spaces'))
                raise RuntimeError('body')
        self.assertIn('-plist', run.call_args_list[0].args[0])
        self.assertIn('-readonly', run.call_args_list[0].args[0])
        self.assertEqual(run.call_args.args[0], ['hdiutil', 'detach', '/dev/disk42'])

    @patch('qemu.hfs_mount.subprocess.run')
    def test_no_mount_and_multiple_mounts_detach(self, run):
        for volumes in ([], [{'mount-point': '/a'}, {'mount-point': '/b'}]):
            run.side_effect = [response([{'dev-entry': '/dev/disk42'}] + volumes),
                               subprocess.CompletedProcess([], 0)]
            with self.assertRaises(RuntimeError):
                attach('test.raw')
            self.assertEqual(run.call_args.args[0][-1], '/dev/disk42')

    @patch('qemu.hfs_mount.subprocess.run')
    def test_retry_force(self, run):
        run.side_effect = [subprocess.CompletedProcess([], 1),
                           subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0)]
        detach('/dev/disk42')
        self.assertEqual(run.call_count, 3)
        self.assertIn('-force', run.call_args.args[0])

    @unittest.skipUnless(shutil.which('hdiutil'), 'requires macOS hdiutil')
    def test_real_round_trip(self):
        # Never attach while a VM is running; restricted process inspection is a skip.
        try:
            processes = subprocess.run(['ps', '-axo', 'comm='], capture_output=True, text=True)
        except PermissionError:
            self.skipTest('Cannot verify VM is stopped in this sandbox')
        if processes.returncode:
            self.skipTest('Cannot verify VM is stopped in this sandbox')
        if any('qemu-system' in line for line in processes.stdout.splitlines()):
            self.skipTest('VM is running')
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / 'test.dmg'
            subprocess.run(['hdiutil', 'create', '-size', '8m', '-fs', 'HFS+',
                            '-volname', 'VPhone Test', str(image)], capture_output=True, check=True)
            with mounted(image, raw=False) as volume:
                (volume.mount_point / 'hello').write_text('hello')
            with mounted(image, readonly=True, raw=False) as volume:
                self.assertEqual((volume.mount_point / 'hello').read_text(), 'hello')
