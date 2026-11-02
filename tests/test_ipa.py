import io
from pathlib import Path
import plistlib
import stat
import struct
import sys
import tempfile
import unittest
import zipfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from ipa import IPAError, extract_ipa, inspect_ipa, make_synthetic_ipa, macho_slices, select_slice


class IPATests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ipa = self.root / 'hello.ipa'

    def test_thin_architectures(self):
        for arch in ('arm64', 'armv7', 'armv7s'):
            make_synthetic_ipa(self.ipa, architecture=arch)
            info = inspect_ipa(self.ipa)
            self.assertEqual(info['selected_slice']['architecture'], arch)
            self.assertEqual(info['bundle_id'], 'org.vphone.hello')
            self.assertEqual(info['minimum_os_version'], '7.0')
            self.assertEqual(info['device_family'], [1])
            self.assertEqual(info['required_capabilities'], ['arm64'])

    def archive(self, entries):
        with zipfile.ZipFile(self.ipa, 'w', compression=zipfile.ZIP_DEFLATED) as z:
            for name, data in entries:
                z.writestr(name, data)

    def test_paths(self):
        for name in ('../escape', '/abs', 'a/../../b', 'a\\b', 'a//b', 'a/./b',
                     'C:/a', 'a\x00b', 'a\nb', 'a./b', 'a /b'):
            with self.subTest(name=name):
                # zipfile truncates NUL when writing; test the original-name guard directly.
                if '\x00' in name:
                    from ipa import _parts
                    with self.assertRaises(IPAError):
                        _parts(name)
                    continue
                self.archive([(name, b'x')])
                with self.assertRaises(IPAError):
                    extract_ipa(self.ipa, self.root / 'out')
                self.assertFalse((self.root / 'out').exists())

    def test_symlink_special_collision_and_bombs(self):
        for mode in (stat.S_IFLNK, stat.S_IFIFO):
            entry = zipfile.ZipInfo('link')
            entry.external_attr = (mode | 0o777) << 16
            self.archive([(entry, b'outside')])
            with self.assertRaises(IPAError):
                inspect_ipa(self.ipa)
        for entries, limits in [([('a', b'x'), ('A', b'y')], {}),
                                ([('a', b'x'), ('a/b', b'y')], {}),
                                ([('café', b'x'), ('café', b'y')], {}),
                                ([('a', b'x' * 10000)], {'max_ratio': 2}),
                                ([('a', b'xx')], {'max_total_size': 1}),
                                ([('a', b'x')], {'max_entries': 0})]:
            self.archive(entries)
            with self.assertRaises(IPAError):
                inspect_ipa(self.ipa, **limits)

    def test_invalid_bundles(self):
        for entries, message in [([('foo', b'x')], 'Payload'),
            ([('Payload/A.app/x', b''), ('Payload/B.app/x', b'')], 'exactly one'),
            ([('Payload/A.app/Info.plist', b'broken')], 'Info.plist'),
            ([('Payload/A.app/Info.plist', b'<?xml version="1.0"?><plist><dict>')], 'Info.plist'),
            ([('Payload/A.app/Info.plist', plistlib.dumps([]))], 'Info.plist')]:
            self.archive(entries)
            with self.assertRaisesRegex(IPAError, message):
                inspect_ipa(self.ipa)
        make_synthetic_ipa(self.ipa, info={'CFBundleExecutable': '../escape'})
        with self.assertRaises(IPAError):
            inspect_ipa(self.ipa)
        self.ipa.write_bytes(b'not a zip')
        with self.assertRaises(IPAError):
            inspect_ipa(self.ipa)

    def test_fat_selection_and_bounds(self):
        arm = struct.pack('<7I', 0xfeedface, 12, 9, 2, 0, 0, 0)
        arm64 = struct.pack('<8I', 0xfeedfacf, 0x100000c, 0, 2, 0, 0, 0, 0)
        for endian in ('>', '<'):
            fat = (struct.pack(endian + '2I', 0xcafebabe, 2)
                   + struct.pack(endian + '5I', 12, 9, 48, 28, 0)
                   + struct.pack(endian + '5I', 0x100000c, 0, 76, 32, 0) + arm + arm64)
            self.assertEqual(select_slice(fat)['architecture'], 'arm64')
            self.assertEqual(select_slice(fat)['offset'], 76)
            for invalid in (fat[:60], fat[:7], fat[:48] + arm64 + arm):
                with self.assertRaises(IPAError):
                    macho_slices(invalid)
        x86 = struct.pack('<7I', 0xfeedface, 7, 3, 2, 0, 0, 0)
        with self.assertRaisesRegex(IPAError, 'A7'):
            select_slice(x86)
        for data in (b'', arm[:20], arm[:20] + struct.pack('<I', 100) + arm[24:]):
            with self.assertRaises(IPAError):
                macho_slices(data)

    def test_fat64_big_endian_thin_and_invalid_architecture_tables(self):
        arm64 = struct.pack('>8I', 0xfeedfacf, 0x100000c, 0, 2, 0, 0, 0, 0)
        self.assertEqual(select_slice(arm64)['architecture'], 'arm64')
        for endian in ('>', '<'):
            fat = (struct.pack(endian + '2I', 0xcafebabf, 1)
                   + struct.pack(endian + 'IIQQII', 0x100000c, 0, 40, 32, 0, 0) + arm64)
            self.assertEqual(select_slice(fat)['offset'], 40)
        arm = struct.pack('<7I', 0xfeedface, 12, 9, 2, 0, 0, 0)
        for offset, size, align, cpu in ((48, 28, 0, 7), (49, 28, 2, 12), (0, 28, 0, 12),
                                          (48, 900, 0, 12), (48, 28, 32, 12)):
            fat = (struct.pack('>2I', 0xcafebabe, 2)
                   + struct.pack('>5I', cpu, 9, offset, size, align)
                   + struct.pack('>5I', 12, 9, 48, 28, 0) + arm)
            with self.assertRaises(IPAError):
                macho_slices(fat)

    def test_optional_metadata_types(self):
        for info in ({'MinimumOSVersion': []}, {'UIDeviceFamily': '1'},
                     {'UIRequiredDeviceCapabilities': {'arm64': 'yes'}},
                     {'CFBundleIdentifier': '../bad'}):
            make_synthetic_ipa(self.ipa, info=info)
            with self.assertRaises(IPAError):
                inspect_ipa(self.ipa)
        make_synthetic_ipa(self.ipa, info={'UIRequiredDeviceCapabilities': {'arm64': True}})
        self.assertEqual(inspect_ipa(self.ipa)['required_capabilities'], {'arm64': True})
