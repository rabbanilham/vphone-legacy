import json
import os
from pathlib import Path
import plistlib
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import host_wifi


class HostWifiTests(unittest.TestCase):
    def setUp(self):
        # Reset cache between tests
        host_wifi._scan_cache = {'time': 0.0, 'data': None}
        host_wifi.stop_scan_refresher()

    def tearDown(self):
        host_wifi.stop_scan_refresher()

    def test_override_and_disabled(self):
        cmd = ['qemu', '-machine', 's5l8960x']
        with patch.object(host_wifi, 'scan_host', return_value=None), patch.object(host_wifi, '_run') as run:
            env = host_wifi.configure_wifi(cmd, False, {}, start_refresher=False)
            run.assert_not_called()
            self.assertEqual(env, {})
            self.assertEqual(cmd, ['qemu', '-machine', 's5l8960x'])
            env = host_wifi.configure_wifi(cmd, True, {'VPHONE_WIFI_SSID': 'Café, network'}, start_refresher=False)
            run.assert_not_called()
        self.assertEqual(env['VPHONE_WIFI_SSID'], 'Café, network')
        self.assertEqual(cmd, ['qemu', '-machine', 's5l8960x,wifi-netdev=wifi0', '-netdev', 'user,id=wifi0'])
        with self.assertRaises(ValueError):
            host_wifi.resolve_ssid({'VPHONE_WIFI_SSID': 'é' * 17})

    def test_macos_fallback_chain(self):
        raw = plistlib.dumps([{'_items': [{'spairport_airport_interfaces': [
            {'spairport_current_network_information': {'_name': 'Host AP'}}]}]}])
        for outputs, expected in [([b'Current Wi-Fi Network: First'], 'First'),
                                  ([b'not associated', b' SSID : Second\n'], 'Second'),
                                  ([b'', b' SSID : <redacted>', raw], 'Host AP'),
                                  ([b'', b'', b'bad xml'], 'vphone-wifi')]:
            with patch.object(host_wifi.sys, 'platform', 'darwin'), \
                 patch.object(host_wifi, 'scan_host', return_value=None), \
                 patch.object(host_wifi, '_run', side_effect=outputs):
                self.assertEqual(host_wifi.resolve_ssid({}), expected)

    def test_other_hosts(self):
        with patch.object(host_wifi.sys, 'platform', 'linux'), patch.object(host_wifi, '_run') as run:
            self.assertEqual(host_wifi.resolve_ssid({}), 'vphone-wifi')
            self.assertIsNone(host_wifi.scan_host())
            run.assert_not_called()

    def test_5ghz_6ghz_channel_mapping(self):
        # 1..11 remain unchanged (US regulatory domain hides 12-14)
        self.assertEqual(host_wifi.map_channel(1), 1)
        self.assertEqual(host_wifi.map_channel(6), 6)
        self.assertEqual(host_wifi.map_channel(11), 11)
        self.assertEqual(host_wifi.map_channel(14), 3)

        # 5 GHz channels mapped into 1..11
        self.assertEqual(host_wifi.map_channel(36), 3)
        self.assertEqual(host_wifi.map_channel(40), 7)
        self.assertEqual(host_wifi.map_channel(44), 11)
        self.assertEqual(host_wifi.map_channel(48), 4)
        self.assertEqual(host_wifi.map_channel(52), 8)
        self.assertEqual(host_wifi.map_channel(149), 6)
        self.assertEqual(host_wifi.map_channel(161), 7)

        # 6 GHz channels
        self.assertEqual(host_wifi.map_channel(5), 5)
        self.assertEqual(host_wifi.map_channel(233), 2)

        # Edge cases and bounds
        self.assertEqual(host_wifi.map_channel(0), 1)
        self.assertEqual(host_wifi.map_channel(-10), 1)
        self.assertEqual(host_wifi.map_channel('invalid'), 1)

        for ch in range(1, 250):
            mapped = host_wifi.map_channel(ch)
            self.assertGreaterEqual(mapped, 1)
            self.assertLessEqual(mapped, 14)

    def test_scan_parsing_and_contract(self):
        sample_output = {
            'version': 1,
            'updated': 1700000000,
            'networks': [
                {'ssid': 'Home-5G', 'channel': 36, 'rssi': -45, 'security': 'wpa2', 'connected': True},
                {'ssid': 'Guest', 'channel': 6, 'rssi': -15, 'security': 'open', 'connected': False},
                {'ssid': 'Weak-IoT', 'channel': 1, 'rssi': -120, 'security': 'wpa', 'connected': False},
                {'ssid': 'Enterprise-Corp', 'channel': 52, 'rssi': -60, 'security': 'enterprise', 'connected': False},
                {'ssid': 'WPA3-Net', 'channel': 161, 'rssi': -50, 'security': 'wpa3', 'connected': False},
                {'ssid': '<redacted>', 'channel': 6, 'rssi': -50, 'security': 'wpa2', 'connected': False},
            ]
        }
        stdout_bytes = json.dumps(sample_output).encode('utf-8')

        with patch.object(host_wifi.sys, 'platform', 'darwin'), \
             patch.object(host_wifi, '_run_scan', return_value=(0, stdout_bytes, b'')):
            scan = host_wifi.scan_host(force_refresh=True)

        self.assertIsNotNone(scan)
        self.assertEqual(scan['version'], 1)
        self.assertEqual(len(scan['networks']), 5)  # <redacted> filtered out

        # Check RSSI clamping: -15 clamped to -30, -120 clamped to -100
        by_ssid = {n['ssid']: n for n in scan['networks']}
        self.assertEqual(by_ssid['Guest']['rssi'], -30)
        self.assertEqual(by_ssid['Weak-IoT']['rssi'], -100)

        # Check channel mapping into 1..11: 36 -> 3, 52 -> 8, 161 -> 7
        self.assertEqual(by_ssid['Home-5G']['channel'], 3)
        self.assertEqual(by_ssid['Enterprise-Corp']['channel'], 8)
        self.assertEqual(by_ssid['WPA3-Net']['channel'], 7)

        # Guest APs are open unless VPHONE_WIFI_SECURITY=mirror
        self.assertTrue(all(n['security'] == 'open' for n in scan['networks']))
        with patch.dict(host_wifi.os.environ, {'VPHONE_WIFI_SECURITY': 'mirror'}):
            mirrored = {n['ssid']: n['security'] for n in host_wifi._sanitize_networks(sample_output['networks'])}
        self.assertEqual(mirrored['Home-5G'], 'wpa2')

        # Check connected: exactly one is connected
        connected_list = [n for n in scan['networks'] if n['connected']]
        self.assertEqual(len(connected_list), 1)
        self.assertEqual(connected_list[0]['ssid'], 'Home-5G')

    def test_capping_max_16_networks(self):
        raw_nets = []
        for i in range(25):
            raw_nets.append({
                'ssid': f'Net-{i}',
                'channel': 6,
                'rssi': -50 - i,
                'security': 'wpa2',
                'connected': (i == 20)  # Connected network is beyond index 15
            })
        sanitized = host_wifi._sanitize_networks(raw_nets)
        self.assertEqual(len(sanitized), 16)
        conn = [n for n in sanitized if n['connected']]
        self.assertEqual(len(conn), 1)
        self.assertEqual(conn[0]['ssid'], 'Net-20')

    def test_redacted_fallback(self):
        # When CoreWLAN helper exits with code 2 (missing location permission)
        with patch.object(host_wifi.sys, 'platform', 'darwin'), \
             patch.object(host_wifi, '_run_scan', return_value=(2, b'', b'Location authorization required')):
            scan = host_wifi.scan_host(force_refresh=True)
            self.assertIsNone(scan)

            cmd = ['qemu', '-machine', 's5l8960x']
            with patch.object(host_wifi, '_run', return_value=b'Current Wi-Fi Network: FallbackNet'):
                env = host_wifi.configure_wifi(cmd, True, start_refresher=False)

        # Exactly today's behaviour: no wifi-scan-file, fallback SSID used
        self.assertEqual(cmd, ['qemu', '-machine', 's5l8960x,wifi-netdev=wifi0', '-netdev', 'user,id=wifi0'])
        self.assertEqual(env['VPHONE_WIFI_SSID'], 'FallbackNet')

    def test_atomic_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scan_file = Path(tmpdir) / 'subdir' / 'wifi-scan.json'
            sample = {
                'version': 1,
                'updated': int(time.time()),
                'networks': [
                    {'ssid': 'MyAP', 'channel': 6, 'rssi': -45, 'security': 'open', 'connected': True}
                ]
            }
            written = host_wifi.write_scan_file(sample, scan_file)
            self.assertEqual(written, str(scan_file))
            self.assertTrue(scan_file.exists())

            # Verify contents
            with open(scan_file, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            self.assertEqual(loaded['version'], 1)
            self.assertEqual(loaded['networks'][0]['ssid'], 'MyAP')

            # Verify no temporary files remain
            parent_files = list(scan_file.parent.iterdir())
            self.assertEqual(parent_files, [scan_file])

    def test_configure_wifi_mutation_with_scan(self):
        mock_scan = {
            'version': 1,
            'updated': 1700000000,
            'networks': [
                {'ssid': 'ConnectedAP', 'channel': 6, 'rssi': -40, 'security': 'wpa2', 'connected': True},
                {'ssid': 'NeighborAP', 'channel': 1, 'rssi': -70, 'security': 'wpa', 'connected': False},
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            scan_path = Path(tmpdir) / 'wifi-scan.json'
            cmd = ['qemu', '-machine', 's5l8960x']

            with patch.object(host_wifi, 'scan_host', return_value=mock_scan):
                env = host_wifi.configure_wifi(cmd, True, scan_file=str(scan_path), start_refresher=False)

            self.assertEqual(env['VPHONE_WIFI_SSID'], 'ConnectedAP')
            expected_machine = f's5l8960x,wifi-scan-file={scan_path},wifi-netdev=wifi0'
            self.assertEqual(cmd, ['qemu', '-machine', expected_machine, '-netdev', 'user,id=wifi0'])
            self.assertTrue(scan_path.exists())

    def test_scan_refresher(self):
        mock_scan = {
            'version': 1,
            'updated': 1700000000,
            'networks': [{'ssid': 'LiveAP', 'channel': 6, 'rssi': -40, 'security': 'open', 'connected': True}]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            scan_path = Path(tmpdir) / 'refresh-scan.json'
            refresher = host_wifi.ScanRefresher(path=str(scan_path), interval=0.05)

            with patch.object(host_wifi, 'scan_host', return_value=mock_scan):
                refresher.start()
                time.sleep(0.15)
                refresher.stop()
                refresher.join(timeout=1.0)

            self.assertFalse(refresher.is_alive())
            self.assertTrue(scan_path.exists())

    def test_configure_wifi_explicit_ssid_skips_scan_file(self):
        mock_scan = {
            'version': 1,
            'updated': 1700000000,
            'networks': [{'ssid': 'ScannedAP', 'channel': 6, 'rssi': -40, 'security': 'open', 'connected': True}]
        }
        # Case A: VPHONE_WIFI_SSID explicitly in environ
        cmd = ['qemu', '-machine', 's5l8960x']
        with patch.object(host_wifi, 'scan_host', return_value=mock_scan) as scan_mock, \
             patch.object(host_wifi, 'write_scan_file') as write_mock:
            env = host_wifi.configure_wifi(cmd, True, {'VPHONE_WIFI_SSID': 'ExplicitAP'}, start_refresher=True)
            scan_mock.assert_not_called()
            write_mock.assert_not_called()
            self.assertEqual(env['VPHONE_WIFI_SSID'], 'ExplicitAP')
            self.assertNotIn('wifi-scan-file', cmd[2])
            self.assertEqual(cmd, ['qemu', '-machine', 's5l8960x,wifi-netdev=wifi0', '-netdev', 'user,id=wifi0'])

        # Case B: -machine wifi-ssid given in cmd
        cmd = ['qemu', '-machine', 's5l8960x,wifi-ssid=CmdAP']
        with patch.object(host_wifi, 'scan_host', return_value=mock_scan) as scan_mock, \
             patch.object(host_wifi, 'write_scan_file') as write_mock:
            env = host_wifi.configure_wifi(cmd, True, {}, start_refresher=True)
            scan_mock.assert_not_called()
            write_mock.assert_not_called()
            self.assertNotIn('wifi-scan-file', cmd[2])
            self.assertEqual(cmd, ['qemu', '-machine', 's5l8960x,wifi-ssid=CmdAP,wifi-netdev=wifi0', '-netdev', 'user,id=wifi0'])

    def test_scan_writer_unique_temp_and_lock(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scan_file = Path(tmpdir) / 'wifi-scan.json'
            sample = {'version': 1, 'updated': 1700000000, 'networks': [{'ssid': 'AP1', 'channel': 1, 'rssi': -50, 'security': 'open', 'connected': True}]}

            created_tmps = []
            orig_replace = os.replace
            def recording_replace(src, dst):
                created_tmps.append(str(src))
                return orig_replace(src, dst)

            with patch('os.replace', side_effect=recording_replace):
                host_wifi.write_scan_file(sample, scan_file)
                host_wifi.write_scan_file(sample, scan_file)

            self.assertEqual(len(created_tmps), 2)
            self.assertNotEqual(created_tmps[0], created_tmps[1])
            self.assertIn(str(os.getpid()), created_tmps[0])
            self.assertIn(str(threading.get_ident()), created_tmps[0])
            self.assertTrue(scan_file.exists())

    def test_stop_scan_refresher_joins_thread(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scan_path = Path(tmpdir) / 'refresher.json'
            refresher = host_wifi.start_scan_refresher(path=str(scan_path), interval=0.05)
            self.assertTrue(refresher.is_alive())
            host_wifi.stop_scan_refresher()
            self.assertFalse(refresher.is_alive())
            self.assertIsNone(host_wifi._active_refresher)

    def test_refresher_removes_file_on_scan_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scan_path = Path(tmpdir) / 'test-scan.json'
            sample = {'version': 1, 'updated': 1700000000, 'networks': [{'ssid': 'OldAP', 'channel': 6, 'rssi': -40, 'security': 'open', 'connected': True}]}
            host_wifi.write_scan_file(sample, str(scan_path))
            self.assertTrue(scan_path.exists())

            refresher = host_wifi.ScanRefresher(path=str(scan_path), interval=0.02)
            with patch.object(host_wifi, 'scan_host', return_value=None):
                refresher.start()
                time.sleep(0.08)
                refresher.stop()
                refresher.join(timeout=1.0)

            self.assertFalse(scan_path.exists())

    def test_malformed_helper_metadata_fallback(self):
        # 1. updated is a string or invalid type -> falls back to int(now)
        sample = {
            'version': 1,
            'updated': 'invalid-timestamp',
            'networks': [{'ssid': 'GoodAP', 'channel': 6, 'rssi': -50, 'security': 'open', 'connected': True}]
        }
        stdout_bytes = json.dumps(sample).encode('utf-8')
        with patch.object(host_wifi.sys, 'platform', 'darwin'), \
             patch.object(host_wifi, '_run_scan', return_value=(0, stdout_bytes, b'')):
            scan = host_wifi.scan_host(force_refresh=True)
        self.assertIsNotNone(scan)
        self.assertIsInstance(scan['updated'], int)
        self.assertEqual(scan['networks'][0]['ssid'], 'GoodAP')

        # Clear cache for isolated check
        host_wifi._scan_cache = {'time': 0.0, 'data': None}

        # 2. Corrupted JSON output -> configure_wifi gracefully falls back without raising
        cmd = ['qemu', '-machine', 's5l8960x']
        with patch.object(host_wifi.sys, 'platform', 'darwin'), \
             patch.object(host_wifi, '_run_scan', return_value=(0, b'{malformed: json', b'')), \
             patch.object(host_wifi, '_run', return_value=b'Current Wi-Fi Network: SafeAP'):
            env = host_wifi.configure_wifi(cmd, True, start_refresher=False)
        self.assertEqual(env['VPHONE_WIFI_SSID'], 'SafeAP')
        self.assertNotIn('wifi-scan-file', cmd[2])

    def test_helper_candidate_preference_and_direct_exec(self):
        root = Path(host_wifi.__file__).resolve().parents[1]
        app_path = root / 'build' / 'host_wifi_scan.app' / 'Contents' / 'MacOS' / 'host_wifi_scan'
        bin_path = root / 'build' / 'host_wifi_scan'

        def fake_isfile(self):
            return self in {app_path, bin_path}

        def fake_access(path, mode):
            return True

        with patch.object(Path, 'is_file', fake_isfile), \
             patch('os.access', side_effect=fake_access), \
             patch.dict(os.environ, {}, clear=True):
            candidates = host_wifi._find_helper_candidates()

        self.assertGreaterEqual(len(candidates), 2)
        self.assertEqual(candidates[0], [str(app_path)])
        self.assertEqual(candidates[1], [str(bin_path)])
        self.assertNotIn('open', candidates[0][0])


if __name__ == '__main__':
    unittest.main()
