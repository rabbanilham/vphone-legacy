from pathlib import Path
import os
import stat
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from qemu.copy_tree_clean import copy_tree_clean


class CopyTests(unittest.TestCase):
    def test_modes_content_and_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp) / 'src', Path(tmp) / 'dst'
            src.mkdir()
            (src / 'dir').mkdir(mode=0o750)
            file = src / 'dir/app'
            file.write_bytes(b'hello')
            file.chmod(0o751)
            (src / '.DS_Store').touch()
            (src / 'dir/._app').touch()
            key = 'com.apple.quarantine' if sys.platform == 'darwin' else 'user.quarantine'
            if sys.platform == 'darwin':
                subprocess.run(['xattr', '-w', key, 'fixture', str(file)], check=True)
            else:
                os.setxattr(file, key, b'fixture')
            copy_tree_clean(src, dst)
            self.assertEqual((dst / 'dir/app').read_bytes(), b'hello')
            self.assertEqual(stat.S_IMODE((dst / 'dir/app').stat().st_mode), 0o751)
            self.assertEqual(stat.S_IMODE((dst / 'dir').stat().st_mode), 0o750)
            if sys.platform == 'darwin':
                self.assertEqual(subprocess.check_output(['xattr', str(dst / 'dir/app')]), b'')
                self.assertIn(key.encode(), subprocess.check_output(['xattr', str(file)]))
            else:
                self.assertEqual(os.listxattr(dst / 'dir/app'), [])
                self.assertIn(key, os.listxattr(file))
            self.assertFalse((dst / '.DS_Store').exists())
            self.assertFalse((dst / 'dir/._app').exists())
            with self.assertRaises(FileExistsError):
                copy_tree_clean(src, dst)

    def test_reject_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / 'src'
            src.mkdir()
            (src / 'link').symlink_to('/tmp')
            with self.assertRaises(ValueError):
                copy_tree_clean(src, Path(tmp) / 'dst')
            self.assertFalse((Path(tmp) / 'dst').exists())
