from pathlib import Path
import struct
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from qemu.hfs_ownership import collect_catalog_owners, set_catalog_owner_many


def image_fixture():
    image = bytearray(5120)
    image[1024:1026] = b'H+'
    struct.pack_into('>I', image, 1028, 0x100)
    struct.pack_into('>I', image, 1064, 512)
    struct.pack_into('>I', image, 1024 + 284, 2)
    struct.pack_into('>II', image, 1024 + 288, 8, 2)
    struct.pack_into('>I', image, 4096 + 24, 1)
    struct.pack_into('>H', image, 4096 + 32, 512)
    image[4616] = 0xff
    struct.pack_into('>H', image, 4618, 2)
    for i in range(2):
        record = 14 + i * 100
        struct.pack_into('>H', image, 4608 + 510 - i * 2, record)
        struct.pack_into('>HIH', image, 4608 + record, 8, 2, 1)
        image[4608 + record + 8:4608 + record + 10] = chr(65 + i).encode('utf-16-be')
        value = 4608 + record + 10
        struct.pack_into('>h', image, value, 1 + i)
        struct.pack_into('>I', image, value + 8, 123 + i)
    return image


class BulkTests(unittest.TestCase):
    def test_bulk_only_changes_requested_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'disk'
            original = image_fixture()
            p.write_bytes(original)
            results = set_catalog_owner_many(p, {123: (501, 20), 124: (0, 0)})
            expected = bytearray(original)
            for cnid, item in results.items():
                offset = item['file_offset']
                expected[offset:offset + 8] = struct.pack('>II', *item['after_uid_gid'])
            self.assertEqual(p.read_bytes(), expected)
            self.assertEqual(results[123]['name'], 'A')
            self.assertEqual(results[123]['after_uid_gid'], [501, 20])

    def test_rejections_leave_image_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'disk'
            for kind in ('missing', 'dirty', 'overflow', 'duplicate', 'bad_uid'):
                data = image_fixture()
                owners = {123: (501, 20), 124: (0, 0)}
                if kind == 'missing':
                    owners[999] = (0, 0)
                if kind == 'dirty':
                    struct.pack_into('>I', data, 1028, 0)
                if kind == 'overflow':
                    struct.pack_into('>I', data, 1024 + 284, 3)
                if kind == 'duplicate':
                    struct.pack_into('>I', data, 4608 + 114 + 10 + 8, 123)
                if kind == 'bad_uid':
                    owners[123] = (-1, 20)
                p.write_bytes(data)
                with self.subTest(kind=kind), self.assertRaises(ValueError):
                    set_catalog_owner_many(p, owners)
                self.assertEqual(p.read_bytes(), data)

    def test_collect_includes_directories_without_following_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'dir').mkdir()
            (root / 'dir/file').touch()
            (root / 'link').symlink_to('/does/not/exist')
            owners = collect_catalog_owners(root, 501, 20)
            self.assertEqual(len(owners), 4)
            self.assertEqual(owners[(root / 'link').lstat().st_ino], (501, 20))
