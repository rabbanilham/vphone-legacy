from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from probe_state import RunClock,ReturnRegistry,ScanoutTracker,pixel_present,instruction_mapped
from display_probe import memory_state

class ProbeStateTests(unittest.TestCase):
    def test_instruction_page_fault_is_deferred_and_wrong_image_rejected(self):
        expected=bytes(range(16))
        self.assertFalse(instruction_mapped(b'',expected));self.assertTrue(instruction_mapped(expected,expected))
        with self.assertRaises(ValueError):instruction_mapped(bytes(16),expected)
    def test_legacy_first_pixel_stops_on_alpha_but_ca_requires_rgb(self):
        analysis=dict(non_zero_bytes=4,non_zero_rgb_bytes=0)
        self.assertTrue(pixel_present(analysis,'first-pixel'));self.assertFalse(pixel_present(analysis,'ca-scanout'))
    def test_pause_does_not_consume_guest_budget(self):
        now=[0.0];clock=RunClock(lambda:now[0]);clock.resume();now[0]=2;clock.pause();now[0]=102
        self.assertEqual(clock.elapsed(),2);self.assertEqual(clock.wall(),102)
        clock.resume();now[0]=105;self.assertEqual(clock.elapsed(),5)
    def test_shared_return_address_keeps_other_thread_and_nested_call(self):
        registry=ReturnRegistry();a=dict(thread='A',stack='0x10');b=dict(thread='B',stack='0x10');nested=dict(thread='A',stack='0x8')
        self.assertTrue(registry.add(42,a));self.assertFalse(registry.add(42,b));registry.add(42,nested)
        self.assertIsNone(registry.match(42,'C','0x10'));self.assertIs(registry.match(42,'A','0x8'),nested)
        self.assertFalse(registry.remove(42,nested));self.assertIs(registry.match(42,'A','0x10'),a)
        self.assertFalse(registry.remove(42,a));self.assertTrue(registry.remove(42,b));self.assertNotIn(42,registry)
    def test_unmapped_memory_is_not_zero(self):
        with patch('display_probe.memory',return_value=b''):
            state=memory_state(None,123);self.assertFalse(state['readable']);self.assertIsNone(state['value'])
        with patch('display_probe.memory',return_value=bytes(8)):
            state=memory_state(None,123);self.assertTrue(state['readable']);self.assertEqual(state['value'],'0x0')
    def test_console_spinner_and_wireless_swap_cannot_satisfy_ca_gate(self):
        tracker=ScanoutTracker();self.assertFalse(tracker.eligible(1101,0x18e0000,9))
        tracker.observe(dict(name='ca_commit',identity=dict(process='SpringBoard'),registers=['0x1']))
        tracker.observe(dict(name='surface_create',return_of=dict(object='0x0'),registers=['0x120000000']))
        tracker.observe(dict(name='swap_submit',registers=['0x77'],provider_properties=dict(IOClass=dict(text='AppleWirelessDisplay'))))
        tracker.observe(dict(name='swap_submit',return_of=dict(object='0x77'),registers=['0x0']))
        self.assertFalse(tracker.eligible(1101,0x18e0000,9))
        tracker.observe(dict(name='swap_submit',registers=['0x88'],provider_properties=dict(IOClass=dict(text='AppleMobileADBE0'))))
        tracker.observe(dict(name='swap_submit',return_of=dict(object='0x88'),registers=['0xe00002bc']))
        self.assertFalse(tracker.eligible(1101,0x18e0000,9))
        tracker.observe(dict(name='swap_submit',return_of=dict(object='0x88'),registers=['0x0']))
        self.assertFalse(tracker.eligible(1101,0,None));self.assertFalse(tracker.eligible(0,0x18e0000,9));self.assertTrue(tracker.eligible(1101,0x18e0000,9))

if __name__=='__main__':unittest.main()
