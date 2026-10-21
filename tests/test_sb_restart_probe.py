"""Keep fault evidence readable and batch respawn accounting process-specific."""
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from crash_batch import summarize_run
from display_probe import dispatch_release_evidence


class RestartProbeTests(unittest.TestCase):
    def test_springboard_respawn_is_counted_without_backboardd_respawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            events = [dict(identity=dict(process=proc, pid=pid)) for proc, pid in
                      [('backboardd', 17), ('SpringBoard', 38), ('SpringBoard', 102)]]
            events.append(dict(identity=dict(process='SpringBoard', pid=None)))
            (run / 'breakpoints.json').write_text(json.dumps(events))
            self.assertTrue(summarize_run(run, 'SpringBoard')['crashed'])
            self.assertFalse(summarize_run(run, 'backboardd')['crashed'])

    def test_missing_or_empty_evidence_is_unobserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            self.assertFalse(summarize_run(run, 'SpringBoard')['observed'])
            (run / 'breakpoints.json').write_text('[]')
            self.assertFalse(summarize_run(run, 'SpringBoard')['observed'])

    def test_fault_capture_preserves_block_and_queue_and_unreadable_object(self):
        regs = [0] * 33
        regs[0], regs[19], regs[24], regs[27], regs[31] = 0x10000078c, 0x2000, 0x3000, 0x4000, 0x5000
        block = bytearray(128)
        struct.pack_into('<QQ', block, 16, 0x6000, 0x7000)
        queue = bytearray(128)
        struct.pack_into('<Q', queue, 0x60, 0x9000)
        data = {0x2000: bytes(block), 0x30a0: struct.pack('<Q', 0x8000),
                0x6000: b'I' * 64, 0x7000: b'D' * 64,
                0x8000: bytes(queue), 0x9000: b'queue\0' + bytes(122)}
        with patch('display_probe.memory', side_effect=lambda gs, addr, size: data.get(addr, b'')[:size]):
            result = dispatch_release_evidence(None, regs)
        self.assertFalse(result['object']['readable'])
        self.assertEqual(result['block_candidate']['invoke'], '0x6000')
        self.assertEqual(result['block_candidate']['invoke_bytes'], (b'I' * 64).hex())
        self.assertEqual(result['queue_candidate']['address'], '0x8000')
        self.assertEqual(bytes.fromhex(result['queue_candidate']['label_bytes']).split(b'\0')[0], b'queue')

    def test_unreadable_candidate_is_not_decoded(self):
        with patch('display_probe.memory', return_value=b''):
            result = dispatch_release_evidence(None, [0] * 33)
        self.assertNotIn('invoke', result['block_candidate'])
        self.assertNotIn('queue_candidate', result)
