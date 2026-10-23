import contextlib
import importlib.util
import io
from pathlib import Path
import struct
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    'agx_frame', Path(__file__).resolve().parents[1] / 'backend/agx_frame.py')
frame_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(frame_module)


def synthetic(flags=1):
    # Pack independently of the reader's structs, matching the C byte offsets.
    header = struct.pack('<4sIIIQQIIIIQQ', b'AGXF', 1, 1, flags,
                         0x803000000, 0x400000000, 0x280, 0x280,
                         0xc0474000, 0xc0470000, 4, 0x803cbd000)
    render = struct.pack('<II', 1, 1) + bytes(0x278)
    ta = struct.pack('<II', 0, 1) + bytes(0x278)
    records = b''.join(struct.pack('<QQ4096s', gva, pa, data) for gva, pa, data in [
        (0x1000, 0x801000000, bytes(range(256)) * 16),
        (0x2000, 0x801100000, b'Z' * 4096),
        (0x4000, 0x801000000, bytes(range(256)) * 16),  # alias, sparse hole
        (0x680004000, 0x801200000, b'P' * 4096),
    ])
    return header + render + ta + records


class FrameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'frame-1.bin'
        self.path.write_bytes(synthetic())

    def test_round_trip(self):
        frame = frame_module.Frame(self.path)
        self.assertEqual(frame.header['catalogue_pa'], 0x803000000)
        self.assertEqual(frame.header['pbe_target'], 0x400000000)
        self.assertEqual(frame.header['record_count'], 4)
        self.assertEqual(frame.header['display_mmu_table'], 0x803cbd000)
        self.assertEqual(frame.type1_command[:8], struct.pack('<II', 1, 1))
        self.assertEqual(frame.ta_command[:8], struct.pack('<II', 0, 1))
        self.assertTrue(frame.ta_valid)
        self.assertFalse(frame.capped)
        self.assertEqual(frame.physical[0x4000], frame.physical[0x1000])
        self.assertEqual(frame.read(0x1ffe, 5), b'\xfe\xffZZZ')
        self.assertEqual(frame.u32(0x1000), 0x03020100)
        self.assertEqual(frame.u64(0x1ffc), 0x5a5a5a5afffefdfc)
        self.assertEqual(frame.read(0x680004000, 3), b'PPP')
        self.assertEqual(frame.read(0x3000, 0), b'')
        for address, size in [(0x3000, 1), (0x2fff, 2), (0, 1)]:
            with self.subTest(address=address), self.assertRaises(KeyError):
                frame.read(address, size)
        for address, size in [(-1, 1), (0, -1), ((1 << 40) - 1, 2)]:
            with self.subTest(address=address), self.assertRaises(ValueError):
                frame.read(address, size)
        self.assertEqual(frame.ranges_by_pc(), {
            0: [(0x1000, 0x3000), (0x4000, 0x5000)],
            26: [(0x680004000, 0x680005000)]})

    def test_cli_and_flags(self):
        self.path.write_bytes(synthetic(flags=2))
        frame = frame_module.Frame(self.path)
        self.assertFalse(frame.ta_valid)
        self.assertTrue(frame.capped)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(frame_module.main([str(self.path)]), 0)
        text = output.getvalue()
        self.assertIn('"catalogue_pa": "0x803000000"', text)
        self.assertIn('PC[ 26]      1 pages', text)
        self.assertIn('0x0000001000..0x0000003000', text)
        self.assertIn('capped: True', text)

    def test_reject_unrelated_fallback_ta(self):
        raw = bytearray(synthetic())
        struct.pack_into('<I', raw, 64 + 0x10, 0xc0460230)
        struct.pack_into('<I', raw, 64 + 0x280 + 0x14, 0xc0460640)
        self.path.write_bytes(raw)
        with self.assertRaisesRegex(ValueError, 'no matching TA'):
            frame_module.Frame(self.path).require_matching_ta()
        struct.pack_into('<I', raw, 64 + 0x280 + 0x14, 0xc0460230)
        self.path.write_bytes(raw)
        frame_module.Frame(self.path).require_matching_ta()
        struct.pack_into('<I', raw, 12, 0)
        self.path.write_bytes(raw)
        with self.assertRaisesRegex(ValueError, 'no matching TA'):
            frame_module.Frame(self.path).require_matching_ta()

    def test_reject_corrupt_files(self):
        valid = synthetic()
        variants = [b'', valid[:63], valid[:-1], valid + b'x',
                    b'BAD!' + valid[4:]]
        for offset, fmt, value in [(4, '<I', 2), (12, '<I', 4),
                                   (32, '<I', 1), (48, '<Q', 1000000),
                                   (1344, '<Q', 0x1001),
                                   (1344 + 8, '<Q', 1),
                                   (1344, '<Q', 1 << 40),
                                   (1344 + 4112, '<Q', 0x1000)]:
            changed = bytearray(valid)
            struct.pack_into(fmt, changed, offset, value)
            variants.append(changed)
        for i, contents in enumerate(variants):
            with self.subTest(variant=i):
                self.path.write_bytes(contents)
                with self.assertRaises(ValueError):
                    frame_module.Frame(self.path)


if __name__ == '__main__':
    unittest.main()
