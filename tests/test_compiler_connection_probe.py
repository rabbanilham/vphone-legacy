"""Recover the connection through the exact registers at each lifetime boundary."""
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from display_probe import compiler_connection_evidence, dispatch_release_evidence


class CompilerConnectionProbeTests(unittest.TestCase):
    def test_invoke_follows_raw_capture_and_keeps_reused_object_bytes(self):
        regs = [0] * 33
        regs[0], regs[19] = 0x2000, 0xdead  # x19 still belongs to dispatch wrapper
        block = bytearray(128)
        struct.pack_into('<QQ', block, 0x20, 0x3000, 0x4000)
        reused = bytearray(128)
        struct.pack_into('<Q', reused, 8, 0x10000078c)
        reused[16:36] = b'\x13BuildMachineOSBuild'
        data = {0x2000: bytes(block), 0x3000: bytes(reused), 0x4000: bytes(96)}
        with patch('display_probe.memory', side_effect=lambda gs, addr, size: data.get(addr, b'')[:size]) as read:
            evidence = compiler_connection_evidence(None, regs, 'sb_compiler_reply_invoke')
        self.assertEqual(evidence['connection_candidate']['address'], '0x3000')
        self.assertEqual(evidence['words'][1], '0x10000078c')
        self.assertFalse(evidence['queue_candidate']['readable'])
        self.assertEqual(evidence['captured_reply']['address'], '0x4000')
        self.assertNotIn(0xdead, [call.args[1] for call in read.call_args_list])
        # At the final release, x19 has been restored to the wrapper's block.
        regs[19] = regs[0]
        with patch('display_probe.memory', side_effect=lambda gs, addr, size: data.get(addr, b'')[:size]):
            fault = dispatch_release_evidence(None, regs)
        self.assertEqual(fault['block_candidate']['capture_20_candidate']['bytes'], bytes(reused).hex())

    def test_after_release_uses_connection_in_x19_without_dereferencing_x0(self):
        regs = [0] * 33
        regs[0], regs[19] = 0xbad, 0x3000
        with patch('display_probe.memory', return_value=b'') as read:
            evidence = compiler_connection_evidence(None, regs, 'sb_compiler_reply_after_xpc_release')
        self.assertEqual(evidence['connection_candidate']['address'], '0x3000')
        self.assertEqual([call.args[1] for call in read.call_args_list], [0x3000])
        self.assertNotIn('words', evidence)

    def test_owner_destroy_and_constructor_registers_resolve_same_slot(self):
        regs = [0] * 33
        regs[0], regs[19] = 0x5000, 0x6000
        for hook, owner in [('sb_compiler_owner_destroy', regs[0]),
                            ('sb_compiler_owner_created', regs[19])]:
            with self.subTest(hook=hook):
                def memory(gs, addr, size):
                    return struct.pack('<Q', 0x7000) if addr == owner + 0x280 else b''
                with patch('display_probe.memory', side_effect=memory):
                    evidence = compiler_connection_evidence(None, regs, hook)
                self.assertEqual(evidence['connection_candidate']['address'], '0x7000')

    def test_lifetime_boundaries_use_the_instruction_specific_register(self):
        regs = [0x1000 + i * 0x100 for i in range(33)]
        for hook, reg in [('sb_compiler_ctor_ready', 19),
                          ('sb_compiler_release_reply', 0),
                          ('sb_compiler_reply_enqueue', 20),
                          ('sb_compiler_request_reply', 24),
                          ('sb_compiler_destroy', 0), ('sb_compiler_delete', 19)]:
            with self.subTest(hook=hook), patch('display_probe.memory', return_value=b''):
                evidence = compiler_connection_evidence(None, regs, hook)
                self.assertEqual(evidence['connection_candidate']['address'], hex(regs[reg]))
                if hook == 'sb_compiler_request_reply':
                    self.assertEqual(evidence['reply_status'], hex(regs[22]))
