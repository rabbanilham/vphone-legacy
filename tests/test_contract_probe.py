from pathlib import Path
import json
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import contract_probe


class ContractProbeTests(unittest.TestCase):
    def write(self, root, name, data):
        (root / name).write_text(json.dumps(data))

    def event(self, name='aks_external_method', selector=14, result='0xe00002f0'):
        registers = ['0x0'] * 33
        registers[0] = result
        registers[31] = '0x800'
        return dict(name=name, registers=registers, identity=dict(process='keybagd', thread='0x123'),
                    return_of=dict(thread='0x123', stack='0x800', argument1=hex(selector)))

    def test_selector_requires_exact_thread_stack_selector_process_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid = self.event()
            wrong_thread = self.event(); wrong_thread['identity']['thread'] = '0x999'
            wrong_stack = self.event(); wrong_stack['registers'][31] = '0x900'
            wrong_process = self.event(); wrong_process['identity']['process'] = 'installd'
            self.write(root, 'breakpoints.json', [wrong_thread, wrong_stack, wrong_process,
                       self.event(selector=5), self.event(result='0x0'), valid])
            report = contract_probe.summarize(root)
            self.assertEqual(report['milestones'][0]['evidence'], ['breakpoints.json#/5'])
            self.assertEqual(report['first_unobserved_milestone'], 'keybagd_systembag_loading')

    def test_no_inference_from_process_names_log_or_unqualified_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events = [dict(name='anything', identity=dict(process=p), registers=['0x0'])
                      for p in ('installd', 'lsd', 'backboardd', 'SpringBoard')]
            unlock = self.event('aks_system_unlock', selector=5, result='0x0')
            unlock['contract_observation'] = dict(ready_flag=1)
            events.append(unlock)
            self.write(root, 'breakpoints.json', events)
            (root / 'serial.log').write_text('systembag loaded, SpringBoard running')
            report = contract_probe.summarize(root)
            self.assertTrue(all(m['status'] == 'unobserved' for m in report['milestones']))

    def test_nonzero_frame_or_candidate_is_not_visual_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root, 'framebuffer_analysis.json', dict(non_zero_rgb_bytes=1, sha256='frame'))
            self.write(root, 'ca-scanout-candidate.json', dict(needs_surface_commit_attribution=True))
            self.assertEqual(contract_probe.summarize(root)['milestones'][-1]['status'], 'unobserved')
            att = dict(surface_commit_attributed=True, physical_swap_completed=True,
                       visually_verified=True, framebuffer_sha256='wrong')
            self.write(root, 'ca-frame-attribution.json', att)
            self.assertEqual(contract_probe.summarize(root)['milestones'][-1]['status'], 'unobserved')
            att['framebuffer_sha256'] = 'frame'; self.write(root, 'ca-frame-attribution.json', att)
            self.assertEqual(contract_probe.summarize(root)['milestones'][-1]['status'], 'observed')

    def test_measured_clocks_and_unknown_phases_are_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root, 'observations.json', dict(guest_running_seconds=90.1, wall_seconds=120.2))
            self.write(root, 'phases.json', dict(build=3.5, cleanup=1.0))
            report = contract_probe.summarize(root)
            self.assertEqual(report['phases_seconds']['guest_running'], 90.1)
            self.assertAlmostEqual(report['phases_seconds']['paused_inspection'], 30.1)
            self.assertIsNone(report['phases_seconds']['preparation'])
            self.assertEqual(report['phases_seconds']['build'], 3.5)
            self.assertIn('No speedup claim', report['performance_claim'])

    def test_readiness_requires_same_provider_and_order_before_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ready = dict(name='aks_ready_before', contract_observation=dict(provider='0x44', ready_flag=1))
            unlock = self.event('aks_system_unlock', selector=5, result='0x0')
            unlock['contract_observation'] = dict(provider='0x44')
            wake = dict(name='aks_wakeup_before', contract_observation=dict(provider='0x45', ready_flag=1))
            progress = self.event('ls_lookup', result='0x12345')
            progress['identity']['process'] = 'lsd'
            self.write(root, 'breakpoints.json', [ready, unlock, wake, progress])
            report = contract_probe.summarize(root)
            self.assertEqual(report['milestones'][2]['status'], 'unobserved')
            self.assertEqual(report['milestones'][4]['status'], 'unobserved')
            wake['contract_observation']['provider'] = '0x44'
            self.write(root, 'breakpoints.json', [progress, ready, unlock, wake, progress])
            report = contract_probe.summarize(root)
            self.assertEqual(report['milestones'][2]['evidence'], ['breakpoints.json#/1', 'breakpoints.json#/2', 'breakpoints.json#/3'])
            self.assertEqual(report['milestones'][4]['evidence'], ['breakpoints.json#/4'])
            self.write(root, 'breakpoints.json', [unlock, ready, wake])
            self.assertEqual(contract_probe.summarize(root)['milestones'][2]['status'], 'unobserved')

    def loader_events(self):
        identity = dict(process='keybagd', thread='0x123')
        path = '/private/var//keybags/systembag.kb'
        opened = dict(name='keybagd_open_result_before', identity=identity.copy(),
                      contract_observation=dict(path=path, verified_user_mapping=True, fd='0xffffffffffffffff'))
        error = dict(name='keybagd_errno_before', identity=identity.copy(),
                     contract_observation=dict(path=path, verified_user_mapping=True,
                                               errno=dict(readable=True, value='0x2', bytes='02000000')))
        return [opened, error]

    def test_missing_systembag_requires_matching_native_open_and_readable_errno(self):
        events = self.loader_events()
        blocker = contract_probe.loader_blocker(events)
        self.assertEqual(blocker['errno_name'], 'ENOENT')
        self.assertEqual(blocker['evidence'], ['breakpoints.json#/0', 'breakpoints.json#/1'])
        events[1]['contract_observation']['errno']['readable'] = False
        self.assertIsNone(contract_probe.loader_blocker(events))
        events = self.loader_events(); events[0]['contract_observation']['path'] = '/private/var/keybags/other.kb'
        self.assertIsNone(contract_probe.loader_blocker(events))
        events = self.loader_events(); events[1]['identity']['thread'] = '0x999'
        self.assertIsNone(contract_probe.loader_blocker(events))
        events = self.loader_events(); events[0]['contract_observation']['verified_user_mapping'] = False
        self.assertIsNone(contract_probe.loader_blocker(events))
        events = self.loader_events(); events[0]['contract_observation']['fd'] = '0x3'
        self.assertIsNone(contract_probe.loader_blocker(events))
        self.assertIsNone(contract_probe.loader_blocker(list(reversed(self.loader_events()))))

    def test_loader_stop_and_hash_bound_contract_remain_separate_from_frame_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'run'; root.mkdir()
            self.write(root, 'breakpoints.json', self.loader_events())
            self.write(root, 'observations.json', dict(kernel_panic=False, guest_running_seconds=10.4, wall_seconds=10.8))
            self.write(root, 'loader-stop.json', dict(reason='Native open failed', event_index=1))
            (root.parent / 'native-contract.json').write_text(json.dumps(dict(kernel_sha256='kernel', boot_image_sha256='boot')))
            report = contract_probe.summarize(root)
            self.assertEqual(report['blocker']['kind'], 'missing_restored_systembag')
            self.assertIs(report['outcome']['kernel_panic'], False)
            self.assertIn('unused guest budget', report['outcome']['budget_semantics'])
            self.assertEqual(report['native_contract']['evidence'], '../native-contract.json')
            self.assertEqual(len(report['native_contract']['sha256']), 64)
            self.assertEqual(report['milestones'][-1]['status'], 'unobserved')
            self.assertIn('unclaimed', report['next_hypothesis']['loaded_state_support'])

    def test_media_verification_requires_all_three_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root, 'diagnostic-disk-cleanup.json', {})
            self.assertFalse(contract_probe.summarize(root)['source_media_verified'])
            media = {name: dict(source_unchanged=True) for name in ('system', 'data', 'utility')}
            self.write(root, 'diagnostic-disk-cleanup.json', media)
            self.assertTrue(contract_probe.summarize(root)['source_media_verified'])
            media['data']['source_unchanged'] = False
            self.write(root, 'diagnostic-disk-cleanup.json', media)
            self.assertFalse(contract_probe.summarize(root)['source_media_verified'])

    def test_preset_retains_boot_inputs_and_ca_stop_condition(self):
        cmd = contract_probe.command(Path('/tmp/probe12-test'))
        self.assertIn('ca-scanout', cmd)
        self.assertEqual(cmd[cmd.index('--seconds') + 1], '90')
        self.assertIn('--returns', cmd)
        self.assertIn('boot-plan-art/boot_plan.json', cmd[cmd.index('--plan') + 1])
        self.assertIn('system-diagnostic-sep.raw', cmd[cmd.index('--diagnostic-system') + 1])


if __name__ == '__main__':
    unittest.main()
