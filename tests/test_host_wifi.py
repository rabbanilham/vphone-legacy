from pathlib import Path
import plistlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import host_wifi


class HostWifiTests(unittest.TestCase):
    def test_override_and_disabled(self):
        cmd = ['qemu', '-machine', 's5l8960x']
        with patch.object(host_wifi, '_run') as run:
            env = host_wifi.configure_wifi(cmd, False, {})
            run.assert_not_called()
            self.assertEqual(env, {})
            self.assertEqual(cmd, ['qemu', '-machine', 's5l8960x'])
            env = host_wifi.configure_wifi(cmd, True, {'VPHONE_WIFI_SSID': 'Café, network'})
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
            with patch.object(host_wifi.sys, 'platform', 'darwin'), patch.object(host_wifi, '_run', side_effect=outputs):
                self.assertEqual(host_wifi.resolve_ssid({}), expected)

    def test_other_hosts(self):
        with patch.object(host_wifi.sys, 'platform', 'linux'), patch.object(host_wifi, '_run') as run:
            self.assertEqual(host_wifi.resolve_ssid({}), 'vphone-wifi')
            run.assert_not_called()
