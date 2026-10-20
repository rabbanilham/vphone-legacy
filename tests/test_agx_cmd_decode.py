import contextlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    "agx_cmd_decode", Path(__file__).resolve().parents[1] / "backend/agx_cmd_decode.py")
decoder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(decoder)


def dump(words, queue=0xc0474000):
    raw = struct.pack(f"<{len(words)}I", *words)
    return f"prefix AGX DUMP queue 0x{queue:x} cmd 0xc0010000 len 0x{len(raw):x} hex={raw.hex()}\n"


class DecodeTests(unittest.TestCase):
    def test_words_and_addresses(self):
        command = decoder.parse_lines([dump([1, 0x40438000, 0xc0448000, 0xe1])])[0]
        self.assertEqual(command['u32'][1], 0x40438000)
        self.assertEqual(command['u64'][1], 0xe1c0448000)
        report = decoder.format_report([command])
        self.assertIn('META? -> GPU 0xe1c0438000', report)
        self.assertIn('GPU VA?', report)
        self.assertEqual(decoder.address_hint(0x3fffffff), '')
        self.assertTrue(decoder.address_hint(0xffffffff))
        self.assertEqual(decoder.address_hint(0xe200000000), '')

    def test_group_filter_diff(self):
        commands = decoder.parse_lines([
            'unrelated\n', dump([1, 2]), dump([3, 9]),
            dump([1, 8], queue=0xc0488000), dump([1, 7])])
        self.assertEqual(len(decoder.group_commands(commands)), 3)
        self.assertIn('+0x004: 0x00000002 -> 0x00000007',
                      decoder.format_report(commands))
        self.assertEqual(len(decoder.parse_lines([dump([1]), dump([3])], 3)), 1)
        self.assertEqual(decoder.word_diff([1, 2], [1]),
                         [dict(offset=4, before=2, after=None)])

    def test_invalid_dump(self):
        for line in ['AGX DUMP bad', dump([1]).replace('len 0x4', 'len 0x8'),
                     dump([1]).replace('01000000', '0100000g'),
                     dump([1]).replace('01000000', '0100000')]:
            with self.subTest(line=line), self.assertRaisesRegex(ValueError, 'line 1:'):
                decoder.parse_lines([line])

    def test_full_lengths_cli_json(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / 'qemu-trace.log'
            result = Path(directory) / 'out.json'
            trace.write_text(dump([1] + [0] * 159) + dump([3] + [0] * 15)
                             + dump([1, 42] + [0] * 158))
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(decoder.main([str(trace), '--type', '1',
                                               '--json', str(result)]), 0)
            groups = json.loads(result.read_text())
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0]['commands'][0]['length'], 0x280)
            self.assertEqual(groups[0]['commands'][1]['changes'],
                             [dict(offset=4, before=0, after=42)])
            self.assertIn('u64:', output.getvalue())
            self.assertEqual(decoder.parse_lines(trace.read_text().splitlines())[1]['length'], 0x40)


if __name__ == '__main__':
    unittest.main()
