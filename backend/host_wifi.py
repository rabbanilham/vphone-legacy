"""Opt-in slirp networking and unprivileged macOS SSID discovery.

The SSID is passed in the subprocess environment, so commas and Unicode do
not acquire QEMU option-string syntax. No host network configuration changes.
"""
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

FALLBACK_SSID = 'vphone-wifi'
DEFAULT_SCAN_FILE = 'build/wifi-scan.json'
VALID_SECURITIES = {'open', 'wpa', 'wpa2', 'wpa3', 'enterprise'}

_scan_cache = {'time': 0.0, 'data': None}
_SCAN_CACHE_TTL = 8.0
_active_refresher = None
_write_lock = threading.Lock()


def _valid(value):
    return isinstance(value, str) and 0 < len(value.encode('utf-8')) <= 32 and '\0' not in value and value.strip().lower() not in {'<redacted>', 'redacted', '<hidden>', 'n/a'}


def _run(argv, timeout):
    try:
        return subprocess.run(argv, capture_output=True, timeout=timeout, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return b''


def _app_bundle(argv):
    exe = Path(argv[0])
    if len(argv) == 1 and exe.name == 'host_wifi_scan' and exe.parent.name == 'MacOS' and exe.parent.parent.name == 'Contents':
        return exe.parent.parent.parent
    return None


def _run_scan(argv, timeout):
    bundle = _app_bundle(argv)
    if bundle is not None:
        # Location authorization belongs to the app bundle, so launch it through
        # LaunchServices (a direct exec is attributed to the parent terminal).
        # An app has no usable stdout, so it writes the JSON to a file.
        out = bundle.parent / ('.scan-%d-%s.json' % (os.getpid(), uuid.uuid4().hex[:8]))
        try:
            proc = subprocess.run(['/usr/bin/open', '-W', '-n', str(bundle), '--args', '--output', str(out)],
                                  capture_output=True, timeout=max(timeout, 10.0))
            data = out.read_bytes() if proc.returncode == 0 else b''
            return proc.returncode, data, proc.stderr
        except (OSError, subprocess.SubprocessError):
            return -1, b'', b''
        finally:
            try:
                out.unlink()
            except OSError:
                pass
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except (OSError, subprocess.SubprocessError):
        return -1, b'', b''


def _find_helper_candidates():
    candidates = []
    override = os.environ.get('VPHONE_HOST_WIFI_SCAN')
    if override:
        p = Path(override)
        if p.is_dir() and (p / 'Contents' / 'MacOS' / 'host_wifi_scan').is_file():
            candidates.append([str(p / 'Contents' / 'MacOS' / 'host_wifi_scan')])
        else:
            candidates.append([override])
    root = Path(__file__).resolve().parents[1]
    app_bin = root / 'build' / 'host_wifi_scan.app' / 'Contents' / 'MacOS' / 'host_wifi_scan'
    if app_bin.is_file() and os.access(app_bin, os.X_OK):
        candidates.append([str(app_bin)])
    bin_path = root / 'build' / 'host_wifi_scan'
    if bin_path.is_file() and os.access(bin_path, os.X_OK):
        candidates.append([str(bin_path)])
    script_path = root / 'scripts' / 'host_wifi_scan.swift'
    if script_path.is_file():
        swift = shutil.which('swift') or '/usr/bin/swift'
        if os.path.exists(swift):
            candidates.append([swift, str(script_path)])
    return candidates


def map_channel(channel):
    try:
        ch = int(channel)
        if ch <= 0:
            return 1
        # The guest regulatory domain is US: iOS hides channels 12-14, so a
        # network on them would silently vanish from the Wi-Fi list.
        if 1 <= ch <= 11:
            return ch
        return ((ch - 1) % 11) + 1
    except (ValueError, TypeError):
        return 1


def clamp_rssi(rssi):
    try:
        r = int(rssi)
        return max(-100, min(-30, r))
    except (ValueError, TypeError):
        return -65


def normalize_security(sec):
    if isinstance(sec, str) and sec.lower() in VALID_SECURITIES:
        return sec.lower()
    return 'open'


def _sanitize_networks(raw_networks):
    if not isinstance(raw_networks, list):
        return []
    valid_nets = []
    seen = {}
    for net in raw_networks:
        if not isinstance(net, dict):
            continue
        ssid = net.get('ssid')
        if not _valid(ssid):
            continue
        ch = map_channel(net.get('channel', 1))
        rssi = clamp_rssi(net.get('rssi', -65))
        sec = normalize_security(net.get('security', 'open'))
        if os.environ.get('VPHONE_WIFI_SECURITY') != 'mirror':
            # The Darwin driver stops before issuing any join ioctl for a secured
            # BSS (no WPA supplicant is modeled), reporting "incorrect password".
            # Internet always goes through the host, so the guest sees open APs.
            sec = 'open'
        conn = bool(net.get('connected', False))
        entry = {
            'ssid': ssid,
            'channel': ch,
            'rssi': rssi,
            'security': sec,
            'connected': conn,
        }
        if ssid in seen:
            idx = seen[ssid]
            existing = valid_nets[idx]
            if conn or (not existing['connected'] and rssi > existing['rssi']):
                entry['connected'] = conn or existing['connected']
                valid_nets[idx] = entry
        else:
            seen[ssid] = len(valid_nets)
            valid_nets.append(entry)

    if not valid_nets:
        return []

    # Sort connected first, then RSSI descending
    valid_nets.sort(key=lambda n: (1 if n['connected'] else 0, n['rssi']), reverse=True)

    # Ensure exactly one network has connected=True
    has_conn = False
    for n in valid_nets:
        if n['connected']:
            if not has_conn:
                has_conn = True
            else:
                n['connected'] = False
    if not has_conn:
        valid_nets[0]['connected'] = True

    # Cap to max 16 networks
    return valid_nets[:16]


def scan_host(force_refresh=False, helper_path=None):
    now = time.time()
    if not force_refresh and _scan_cache['data'] is not None:
        if now - _scan_cache['time'] < _SCAN_CACHE_TTL:
            return _scan_cache['data']

    if sys.platform != 'darwin':
        return None

    if helper_path:
        hp = Path(helper_path)
        if hp.is_dir() and (hp / 'Contents' / 'MacOS' / 'host_wifi_scan').is_file():
            candidates = [[str(hp / 'Contents' / 'MacOS' / 'host_wifi_scan')]]
        else:
            candidates = [[helper_path]]
    else:
        candidates = _find_helper_candidates()
    if not candidates:
        return None

    for argv in candidates:
        code, stdout, _ = _run_scan(argv, timeout=4.0)
        if code != 0 or not stdout:
            continue
        try:
            payload = json.loads(stdout.decode('utf-8', errors='replace'))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        try:
            version_val = int(payload.get('version', 0))
        except (ValueError, TypeError):
            continue
        if version_val != 1:
            continue
        try:
            networks = _sanitize_networks(payload.get('networks'))
            if not networks:
                continue

            try:
                updated_val = int(payload.get('updated', int(now)))
            except (ValueError, TypeError):
                updated_val = int(now)

            result = {
                'version': 1,
                'updated': updated_val,
                'networks': networks,
            }
            _scan_cache['time'] = now
            _scan_cache['data'] = result
            return result
        except Exception:
            continue

    return None


def remove_scan_file(path=None):
    target = Path(path or DEFAULT_SCAN_FILE)
    with _write_lock:
        try:
            if target.exists():
                target.unlink()
        except OSError:
            pass


def write_scan_file(scan_data, path=None):
    target = Path(path or DEFAULT_SCAN_FILE)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_suffix = f"{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}"
    tmp_target = target.with_name(f"{target.name}.tmp.{temp_suffix}")
    with _write_lock:
        try:
            content = json.dumps(scan_data, indent=2).encode('utf-8')
            with open(tmp_target, 'wb') as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_target, target)
            return str(target)
        except Exception:
            if tmp_target.exists():
                try:
                    tmp_target.unlink()
                except OSError:
                    pass
            return None


class ScanRefresher(threading.Thread):
    def __init__(self, path=None, interval=10.0):
        super().__init__(name='wifi-scan-refresher', daemon=True)
        self.path = path or DEFAULT_SCAN_FILE
        self.interval = interval
        self._stop_event = threading.Event()

    def run(self):
        while not self._stop_event.is_set():
            if self._stop_event.wait(self.interval):
                break
            try:
                data = scan_host(force_refresh=True)
                if data:
                    write_scan_file(data, self.path)
                else:
                    remove_scan_file(self.path)
            except Exception:
                remove_scan_file(self.path)

    def stop(self):
        self._stop_event.set()


def start_scan_refresher(path=None, interval=10.0):
    global _active_refresher
    if _active_refresher and _active_refresher.is_alive():
        return _active_refresher
    if _active_refresher:
        stop_scan_refresher()
    refresher = ScanRefresher(path=path, interval=interval)
    refresher.start()
    _active_refresher = refresher
    return refresher


def stop_scan_refresher(timeout=5.0):
    global _active_refresher
    if _active_refresher:
        _active_refresher.stop()
        if _active_refresher.is_alive() and threading.current_thread() != _active_refresher:
            _active_refresher.join(timeout=timeout)
        _active_refresher = None


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


def resolve_ssid(environ=None, use_scan=True):
    environ = os.environ if environ is None else environ
    override = environ.get('VPHONE_WIFI_SSID')
    if override:
        if not _valid(override):
            raise ValueError('VPHONE_WIFI_SSID must contain 1..32 UTF-8 bytes')
        return override
    if sys.platform == 'darwin':
        if use_scan:
            scan = scan_host()
            if scan and isinstance(scan.get('networks'), list):
                for net in scan['networks']:
                    if net.get('connected') and _valid(net.get('ssid')):
                        return net['ssid']
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


def configure_wifi(cmd, enabled, environ=None, scan_file=None, start_refresher=True):
    """Return child environment, adding Wi-Fi to an existing QEMU command."""
    env = dict(os.environ if environ is None else environ)
    if enabled:
        machine = cmd.index('-machine') + 1
        has_env_ssid = bool(env.get('VPHONE_WIFI_SSID'))
        has_machine_ssid = 'wifi-ssid' in cmd[machine]
        use_scan = not (has_env_ssid or has_machine_ssid)

        written = None
        if use_scan:
            scan_path = scan_file or DEFAULT_SCAN_FILE
            try:
                scan = scan_host()
            except Exception:
                scan = None
            if scan:
                written = write_scan_file(scan, scan_path)
                if written and start_refresher:
                    start_scan_refresher(written)

        if has_machine_ssid and not has_env_ssid:
            m = re.search(r'(?:^|,)wifi-ssid=([^,]+)', cmd[machine])
            if m:
                env['VPHONE_WIFI_SSID'] = m.group(1)

        env['VPHONE_WIFI_SSID'] = resolve_ssid(env, use_scan=use_scan)
        if written:
            cmd[machine] += f',wifi-scan-file={written}'
        cmd[machine] += ',wifi-netdev=wifi0'
        cmd.extend(['-netdev', 'user,id=wifi0'])
    return env
