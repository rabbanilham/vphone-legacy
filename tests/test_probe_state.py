import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from probe_state import RunClock,ReturnRegistry,ScanoutTracker,pixel_present,instruction_mapped
from display_probe import memory_state, parse_hook_dumps, capture_hook_dumps, derive_exe_slide, install_exe_hooks

class ProbeStateTests(unittest.TestCase):
    def test_opt_in_hook_dumps_bound_reads_and_preserve_failures(self):
        self.assertEqual(parse_hook_dumps([]), {})
        specs = parse_hook_dumps(['frame=x25:96', 'frame=x1:0x10'])
        self.assertEqual(specs, {'frame': [(25, 96, 0), (1, 16, 0)]})
        for invalid in ['frame=x31:8', 'frame=x32:8', 'frame=x1:0',
                        'frame=x1:4097', 'frame=x1:-1', '=x0:8',
                        'frame=sp+4097:8', 'frame=x1-4097:8', 'frame=sp+:8']:
            with self.assertRaises(ValueError): parse_hook_dumps([invalid])
        regs = [0] * 31; regs[25] = 0x1234
        with patch('display_probe.memory', return_value=b'abc') as read:
            dumps = capture_hook_dumps(None, regs, specs['frame'])
        read.assert_called_once_with(None, 0x1234, 96)
        self.assertEqual(dumps[0]['bytes'], '616263')
        self.assertFalse(dumps[0]['readable'])
        self.assertEqual(dumps[1]['address'], '0x0')
        self.assertFalse(dumps[1]['readable'])

    def test_hook_stack_offsets_do_not_read_null_or_wrapped_addresses(self):
        specs = parse_hook_dumps(['gate=sp+0x1c0:192', 'gate=x29-16:8'])['gate']
        regs = [0] * 32
        regs[31] = 0x10000
        with patch('display_probe.memory', return_value=bytes(192)) as read:
            dumps = capture_hook_dumps(None, regs, specs)
        read.assert_called_once_with(None, 0x101c0, 192)
        self.assertEqual(dumps[0]['register'], 'sp')
        self.assertEqual(dumps[0]['offset'], 0x1c0)
        self.assertFalse(dumps[1]['readable'])
        regs[29] = 8
        regs[31] = (1 << 64) - 1
        with patch('display_probe.memory') as read:
            dumps = capture_hook_dumps(None, regs, specs)
        read.assert_not_called()
        self.assertTrue(all(not dump['readable'] for dump in dumps))

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

    def test_derive_exe_slide_from_known_return_site(self):
        hdr = bytes.fromhex('cffaedfe0c0000010000000002000000')
        known = {'bks_digitizer_info': '0x100016ef0'}
        lr = 0x1000feef0  # slide 0xe8000
        def mock_mem(gs, addr, size):
            return hdr if addr == 0x1000e8000 else b''
        slide = derive_exe_slide(None, mock_mem, 'backboardd', hdr, known_sites=known, lr=lr, hook_name='bks_digitizer_info')
        self.assertEqual(slide, 0xe8000)

    def test_derive_exe_slide_from_page_scan(self):
        hdr = bytes.fromhex('cffaedfe0c0000010000000002000000')
        def mock_mem(gs, addr, size):
            return hdr if addr == 0x100064000 else b''
        slide = derive_exe_slide(None, mock_mem, 'backboardd', hdr, max_scan_bytes=0x100000)
        self.assertEqual(slide, 0x64000)

    def test_derive_exe_slide_not_found(self):
        hdr = bytes.fromhex('cffaedfe0c0000010000000002000000')
        slide = derive_exe_slide(None, lambda gs, a, s: b'', 'backboardd', hdr, max_scan_bytes=0x10000)
        self.assertIsNone(slide)

    def test_install_exe_hooks_verifies_and_registers(self):
        hooks = {
            'bb_route': '0x10001616c',
            'bb_forward': {'address': '0x100013540', 'expected': '0102030405060708090a0b0c0d0e0f10'}
        }
        expected = {'bb_route': 'aabbccddeeff00112233445566778899'}
        bps = {}; user_expected = {}
        slide = 0xe8000
        mem_map = {
            0x10001616c + slide: bytes.fromhex(expected['bb_route']),
            0x100013540 + slide: bytes.fromhex(hooks['bb_forward']['expected'])
        }
        rsp_log = []
        def mock_rsp(gs, cmd):
            rsp_log.append(cmd)
            return 'OK'
        verified = install_exe_hooks(None, lambda gs, a, s: mem_map.get(a, b''), mock_rsp, 'backboardd', slide, hooks, expected, bps, user_expected)
        self.assertEqual(len(verified), 2)
        self.assertIn(0x10001616c + slide, bps)
        self.assertEqual(bps[0x10001616c + slide], 'bb_route')
        self.assertEqual(user_expected[0x10001616c + slide], bytes.fromhex(expected['bb_route']))
        self.assertEqual(len(rsp_log), 2)
        self.assertTrue(all(v['exe'] for v in verified))

    def test_install_exe_hooks_rejects_instruction_mismatch(self):
        hooks = {'bb_route': '0x10001616c'}
        expected = {'bb_route': 'aabbccddeeff00112233445566778899'}
        bps = {}; user_expected = {}
        with self.assertRaises(AssertionError):
            install_exe_hooks(None, lambda gs, a, s: bytes(16), lambda gs, c: 'OK', 'backboardd', 0xe8000, hooks, expected, bps, user_expected)

    def test_touch_owner_hooks_schema_and_contents(self):
        path = Path(__file__).resolve().parent.parent / 'backend' / 'touch_owner_hooks.json'
        self.assertTrue(path.exists(), 'backend/touch_owner_hooks.json must exist')
        data = json.loads(path.read_text())
        self.assertIn('symbols', data)
        self.assertIn('exe_hooks', data)
        self.assertIn('backboardd', data['exe_hooks'])
        self.assertIn('suggested_hook_dumps', data)

        # Check critical CA server and overlay symbols
        for req_sym in ('ca_server_create_context', 'ca_server_context_created',
                        'ca_server_local_context', 'ca_context_task_result',
                        'pui_create_context', 'pui_set_visible', 'pui_dealloc'):
            self.assertIn(req_sym, data['symbols'])
            self.assertTrue(data['symbols'][req_sym].startswith('0x18'))

        # Check critical backboardd overlay exe hooks
        bb_hooks = data['exe_hooks']['backboardd']
        for req_hook in ('bb_overlay_create', 'bb_overlay_created',
                         'bb_overlay_dismiss_delay_trigger', 'bb_overlay_dismiss_worker',
                         'bb_overlay_set_hidden', 'bb_overlay_release',
                         'bb_sentinel_dismiss_call', 'bb_sentinel_checkin_dismiss_call'):
            self.assertIn(req_hook, bb_hooks)
            info = bb_hooks[req_hook]
            self.assertTrue(info['address'].startswith('0x1000'))
            self.assertEqual(len(info['expected']), 32)
            bytes.fromhex(info['expected'])

        # Check suggested dumps
        self.assertIn('ca_context_task_result', data['suggested_hook_dumps'])
        self.assertEqual(data['suggested_hook_dumps']['ca_context_task_result'], ['x1:256'])
        self.assertIn('ca_server_context_created', data['suggested_hook_dumps'])
        self.assertEqual(data['suggested_hook_dumps']['ca_server_context_created'], ['x21:184'])

    def test_touch_queue_hooks_schema_and_contents(self):
        path = Path(__file__).resolve().parent.parent / 'backend' / 'touch_queue_hooks.json'
        self.assertTrue(path.exists(), 'backend/touch_queue_hooks.json must exist')
        data = json.loads(path.read_text())
        self.assertIn('symbols', data)
        self.assertIn('exe_hooks', data)
        self.assertIn('backboardd', data['exe_hooks'])
        self.assertIn('SpringBoard', data['exe_hooks'])
        self.assertIn('suggested_hook_dumps', data)
        self.assertIn('known_return_sites', data)
        self.assertIn('SpringBoard', data['known_return_sites'])
        self.assertIn('exe_headers', data)
        self.assertIn('SpringBoard', data['exe_headers'])

        # Check critical queue IPC shared cache symbols
        for req_sym in ('hid_connection_dispatch', 'hid_connection_enqueue_result',
                        'hid_queue_enqueue', 'hid_data_queue_enqueue', 'hid_data_queue_enqueue_tail',
                        'hid_data_queue_notify', 'hid_data_queue_notify_msg', 'hid_data_queue_notify_ret',
                        'hid_queue_set_notify_port', 'hid_data_queue_set_port',
                        'hid_server_create_queue', 'hid_server_start_queue', 'hid_server_stop_queue',
                        'bks_register_callback', 'hid_client_schedule_runloop', 'hid_client_register_callback',
                        'cf_mach_port_create_source', 'cf_mach_port_perform',
                        'hid_client_queue_callback', 'hid_client_dequeued', 'hid_client_callback_call'):
            self.assertIn(req_sym, data['symbols'])
            self.assertTrue(data['symbols'][req_sym].startswith('0x18'))

        # Check SpringBoard exe hooks
        sb_hooks = data['exe_hooks']['SpringBoard']
        for req_hook in ('sb_hid_monitor_create', 'sb_hid_monitor_callback'):
            self.assertIn(req_hook, sb_hooks)
            info = sb_hooks[req_hook]
            self.assertTrue(info['address'].startswith('0x1000'))
            self.assertEqual(len(info['expected']), 32)
            bytes.fromhex(info['expected'])

        # Check backboardd exe hooks
        bb_hooks = data['exe_hooks']['backboardd']
        for req_hook in ('bb_forward_task', 'bb_forward_task_conn_gate',
                         'bb_forward_task_dispatch', 'bb_client_for_task',
                         'bb_client_conn_added', 'bb_client_conn_added_worker',
                         'bb_client_conn_added_cfport', 'bb_system_gesture_stealing_gate'):
            self.assertIn(req_hook, bb_hooks)
            info = bb_hooks[req_hook]
            self.assertTrue(info['address'].startswith('0x1000'))
            self.assertEqual(len(info['expected']), 32)
            bytes.fromhex(info['expected'])

        # Check suggested dumps
        self.assertIn('hid_data_queue_enqueue', data['suggested_hook_dumps'])
        self.assertEqual(data['suggested_hook_dumps']['hid_data_queue_enqueue'], ['x0:32'])
        self.assertIn('hid_data_queue_notify_msg', data['suggested_hook_dumps'])
        self.assertEqual(data['suggested_hook_dumps']['hid_data_queue_notify_msg'], ['sp:24'])
        self.assertIn('cf_mach_port_perform', data['suggested_hook_dumps'])
        self.assertEqual(data['suggested_hook_dumps']['cf_mach_port_perform'], ['x1:24', 'x3:288'])
        self.assertIn('hid_client_queue_created', data['suggested_hook_dumps'])
        self.assertEqual(data['suggested_hook_dumps']['hid_client_queue_created'], ['x19:288'])

    def test_springboard_exe_hooks_registration(self):
        hooks = {
            'sb_hid_monitor_create': '0x100068590',
            'sb_hid_monitor_callback': {'address': '0x100068cac', 'expected': 'fd7bbfa9fd030091f44fbfa9f657bfa9'}
        }
        expected = {'sb_hid_monitor_create': 'fad20994f30300aa932600f993580494'}
        bps = {}; user_expected = {}
        slide = 0xe4000
        mem_map = {
            0x100068590 + slide: bytes.fromhex(expected['sb_hid_monitor_create']),
            0x100068cac + slide: bytes.fromhex(hooks['sb_hid_monitor_callback']['expected'])
        }
        rsp_log = []
        def mock_rsp(gs, cmd):
            rsp_log.append(cmd)
            return 'OK'
        verified = install_exe_hooks(None, lambda gs, a, s: mem_map.get(a, b''), mock_rsp, 'SpringBoard', slide, hooks, expected, bps, user_expected)
        self.assertEqual(len(verified), 2)
        self.assertIn(0x100068590 + slide, bps)
        self.assertEqual(bps[0x100068590 + slide], 'sb_hid_monitor_create')
        self.assertEqual(user_expected[0x100068590 + slide], bytes.fromhex(expected['sb_hid_monitor_create']))
        self.assertEqual(len(rsp_log), 2)
        self.assertTrue(all(v['exe'] and v['process'] == 'SpringBoard' for v in verified))

    def test_touch_runloop_hooks_schema_and_contents(self):
        path = Path(__file__).resolve().parent.parent / 'backend' / 'touch_runloop_hooks.json'
        self.assertTrue(path.exists(), 'backend/touch_runloop_hooks.json must exist')
        data = json.loads(path.read_text())
        self.assertIn('symbols', data)
        self.assertIn('exe_hooks', data)
        self.assertIn('backboardd', data['exe_hooks'])
        self.assertIn('suggested_hook_dumps', data)
        self.assertIn('notes', data)

        # Check safe run loop investigation symbols
        safe_syms = {
            'cf_runloop_run_specific': '0x181599c0c',
            'cf_runloop_run': '0x1816587b0',
            'cf_runloop_add_source': '0x1815da808',
            'cf_mach_port_create_source': '0x1815da548',
            'hid_client_schedule_runloop': '0x1825bbafc',
            'hid_client_unschedule_runloop': '0x1825bc490',
            'bks_register_callback_schedule': '0x185cdd824',
            'bks_register_callback_post_schedule': '0x185cdd82c',
            'hid_data_queue_enqueue_tail': '0x18259df50',
            'hid_data_queue_notify': '0x18259e0c4',
            'hid_data_queue_notify_ret': '0x18259e118',
        }
        for name, addr in safe_syms.items():
            self.assertIn(name, data['symbols'])
            self.assertEqual(data['symbols'][name], addr)

        # Ensure unsafe hooks that stalled probe 105/106 are EXCLUDED
        for unsafe_sym in (
            'cf_mach_port_perform',
            'hid_server_create_queue',
            'hid_server_start_queue',
            'hid_server_stop_queue',
            'hid_queue_set_notify_port',
            'hid_data_queue_set_port',
        ):
            self.assertNotIn(unsafe_sym, data['symbols'])

        # Check suggested hook dumps
        dumps = data['suggested_hook_dumps']
        self.assertIn('cf_runloop_run_specific', dumps)
        self.assertIn('x1:32', dumps['cf_runloop_run_specific'])
        self.assertIn('cf_runloop_add_source', dumps)
        self.assertIn('x1:176', dumps['cf_runloop_add_source'])
        self.assertIn('x2:32', dumps['cf_runloop_add_source'])
        self.assertIn('hid_client_schedule_runloop', dumps)
        self.assertIn('x0:256', dumps['hid_client_schedule_runloop'])
        self.assertIn('bks_register_callback_post_schedule', dumps)
        self.assertIn('x20:256', dumps['bks_register_callback_post_schedule'])

if __name__=='__main__':unittest.main()
