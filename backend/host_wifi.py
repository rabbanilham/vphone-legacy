"""Opt-in slirp networking and unprivileged macOS SSID discovery.

The SSID is passed in the subprocess environment, so commas and Unicode do
not acquire QEMU option-string syntax. No host network configuration changes.
"""
import os
import plistlib
import re
import subprocess
import sys

FALLBACK_SSID = 'vphone-wifi'


def _valid(value):
    return isinstance(value, str) and 0 < len(value.encode('utf-8')) <= 32 and '\0' not in value and value.strip().lower() not in {'<redacted>', 'redacted', '<hidden>', 'n/a'}


def _run(argv, timeout):
    try:
        return subprocess.run(argv, capture_output=True, timeout=timeout, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return b''


def _plist_ssid(value):
    if isinstance(value, dict):
        current = value.get('spairport_current_network_information')
        if isinstance(current, dict) and _valid(current.get('_name')):
            return current['_name']
        for child in value.values():
            found = _plist_ssid(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _plist_ssid(child)
            if found:
                return found
    return None


def resolve_ssid(environ=None):
    environ = os.environ if environ is None else environ
    override = environ.get('VPHONE_WIFI_SSID')
    if override:
        if not _valid(override):
            raise ValueError('VPHONE_WIFI_SSID must contain 1..32 UTF-8 bytes')
        return override
    if sys.platform == 'darwin':
        text = _run(['/usr/sbin/networksetup', '-getairportnetwork', 'en0'], 2).decode('utf-8', 'replace').strip()
        prefix = 'Current Wi-Fi Network: '
        if text.startswith(prefix) and _valid(text[len(prefix):]):
            return text[len(prefix):]
        # wdutil is unavailable without privileges on some macOS versions.
        # Never invoke sudo or prompt; continue to system_profiler on failure.
        text = _run(['/usr/bin/wdutil', 'info'], 2).decode('utf-8', 'replace')
        match = re.search(r'^\s*SSID\s*:\s*(.+?)\s*$', text, re.MULTILINE)
        if match and _valid(match[1]):
            return match[1]
        raw = _run(['/usr/sbin/system_profiler', 'SPAirPortDataType', '-xml'], 15)
        try:
            found = _plist_ssid(plistlib.loads(raw))
            if found:
                return found
        except (ValueError, plistlib.InvalidFileException):
            pass
    return FALLBACK_SSID


def configure_wifi(cmd, enabled, environ=None):
    """Return child environment, adding Wi-Fi to an existing QEMU command."""
    env = dict(os.environ if environ is None else environ)
    if enabled:
        env['VPHONE_WIFI_SSID'] = resolve_ssid(env)
        machine = cmd.index('-machine') + 1
        cmd[machine] += ',wifi-netdev=wifi0'
        cmd.extend(['-netdev', 'user,id=wifi0'])
    return env
