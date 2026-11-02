"""Firmware-free IPA validation. No signing or installation is performed."""
from pathlib import Path
import plistlib
import re
import stat
import struct
import tempfile
import unicodedata
import zipfile
from xml.parsers.expat import ExpatError


class IPAError(ValueError):
    """Malformed, unsafe or unsupported IPA."""


def _parts(name):
    parts = name.rstrip('/').split('/')
    if (not name or name.startswith('/') or '\\' in name or ':' in name
            or any(p in ('', '.', '..') or p.endswith((' ', '.')) for p in parts)
            or any(unicodedata.category(c).startswith('C') for c in name)):
        raise IPAError(f'Unsafe archive path: {name!r}')
    return parts


def extract_ipa(ipa, destination, *, max_total_size=512 * 1024**2,
                max_ratio=200, max_entries=50000):
    """Extract into a new directory only; validate all entries before writing."""
    destination = Path(destination)
    try:
        with zipfile.ZipFile(ipa) as archive:
            entries = archive.infolist()
            if len(entries) > max_entries:
                raise IPAError('Archive has too many entries')
            seen, files, paths, total = set(), set(), [], 0
            for entry in entries:
                parts = _parts(entry.orig_filename)
                key = unicodedata.normalize('NFD', '/'.join(parts)).casefold()
                if key in seen:
                    raise IPAError('Duplicate archive path')
                seen.add(key)
                paths.append(key)
                if not entry.is_dir():
                    files.add(key)
                kind = stat.S_IFMT(entry.external_attr >> 16)
                if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise IPAError('Archive symlink or special file rejected')
                if entry.flag_bits & 1:
                    raise IPAError('Encrypted archive entry rejected')
                total += entry.file_size
                if total > max_total_size or entry.file_size > max_ratio * max(1, entry.compress_size):
                    raise IPAError('Archive size or compression ratio limit exceeded')
            for key in paths:
                parts = key.split('/')
                if any('/'.join(parts[:i]) in files for i in range(1, len(parts))):
                    raise IPAError('Archive file/directory path collision')
            destination.mkdir(parents=True, exist_ok=False)
            for entry in entries:
                target = destination.joinpath(*_parts(entry.filename))
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(entry) as source, target.open('xb') as output:
                        remaining = entry.file_size
                        while chunk := source.read(min(1024 * 1024, remaining + 1)):
                            remaining -= len(chunk)
                            if remaining < 0:
                                raise IPAError('Archive entry exceeds declared size')
                            output.write(chunk)
                    target.chmod((entry.external_attr >> 16) & 0o777 or 0o644)
    except (zipfile.BadZipFile, RuntimeError, EOFError) as exc:
        raise IPAError(f'Invalid ZIP: {exc}') from exc
    return destination


def macho_slices(data):
    """Return validated thin/fat slice headers, preferring arm64, armv7s, armv7."""
    def thin(blob, offset=0):
        magics = {b'\xce\xfa\xed\xfe': ('<', 28), b'\xcf\xfa\xed\xfe': ('<', 32),
                  b'\xfe\xed\xfa\xce': ('>', 28), b'\xfe\xed\xfa\xcf': ('>', 32)}
        if blob[:4] not in magics:
            raise IPAError('Invalid Mach-O magic')
        endian, size = magics[blob[:4]]
        if len(blob) < size:
            raise IPAError('Truncated Mach-O header')
        cpu, subtype, filetype, ncmds, sizeofcmds = struct.unpack_from(endian + '5I', blob, 4)
        if sizeofcmds > len(blob) - size or ncmds > sizeofcmds // 8:
            raise IPAError('Invalid Mach-O load command bounds')
        arch = {(0x100000c, 0): 'arm64', (12, 9): 'armv7', (12, 11): 'armv7s'}.get((cpu, subtype & 0xffffff))
        if (cpu == 0x100000c) != (size == 32) and cpu in (12, 0x100000c):
            raise IPAError('Mach-O CPU/header mismatch')
        return dict(architecture=arch, cpu_type=cpu, cpu_subtype=subtype,
                    offset=offset, size=len(blob), file_type=filetype)
    fat = {b'\xca\xfe\xba\xbe': ('>', False), b'\xbe\xba\xfe\xca': ('<', False),
           b'\xca\xfe\xba\xbf': ('>', True), b'\xbf\xba\xfe\xca': ('<', True)}
    if data[:4] not in fat:
        return [thin(data)]
    endian, wide = fat[data[:4]]
    if len(data) < 8:
        raise IPAError('Truncated fat header')
    count = struct.unpack_from(endian + 'I', data, 4)[0]
    stride = 32 if wide else 20
    end = 8 + count * stride
    if not count or count > 128 or end > len(data):
        raise IPAError('Invalid fat architecture table')
    result, ranges = [], []
    for i in range(count):
        cpu, sub, offset, size, align = struct.unpack_from(endian + ('IIQQI' if wide else '5I'), data, 8 + stride * i)
        if align > 31 or offset % (1 << align) or offset < end or size < 28 or offset + size > len(data):
            raise IPAError('Invalid fat slice bounds/alignment')
        if any(offset < b and a < offset + size for a, b in ranges):
            raise IPAError('Overlapping fat slices')
        ranges.append((offset, offset + size))
        item = thin(data[offset:offset + size], offset)
        if (cpu, sub) != (item['cpu_type'], item['cpu_subtype']):
            raise IPAError('Fat/thin architecture mismatch')
        result.append(item)
    return result


def select_slice(data):
    slices = macho_slices(data)
    for arch in ('arm64', 'armv7s', 'armv7'):
        for item in slices:
            if item['architecture'] == arch and item['file_type'] == 2:
                return item
    raise IPAError('Mach-O has no executable slice compatible with guest A7')


def inspect_ipa(ipa, **limits):
    """Return JSON-compatible bundle metadata and selected executable slice."""
    with tempfile.TemporaryDirectory() as tmp:
        root = extract_ipa(ipa, Path(tmp) / 'contents', **limits)
        apps = list((root / 'Payload').glob('*.app'))
        if not (root / 'Payload').is_dir():
            raise IPAError('IPA has no Payload directory')
        if len(apps) != 1 or not apps[0].is_dir():
            raise IPAError(f'Expected exactly one Payload/*.app; found {len(apps)}')
        app = apps[0]
        try:
            info = plistlib.loads((app / 'Info.plist').read_bytes())
            if not isinstance(info, dict):
                raise ValueError('expected dictionary')
            bundle_id, executable = info['CFBundleIdentifier'], info['CFBundleExecutable']
            if not isinstance(bundle_id, str) or not re.fullmatch(r'[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+', bundle_id):
                raise ValueError('invalid bundle identifier')
            if not isinstance(executable, str) or len(_parts(executable)) != 1:
                raise ValueError('invalid executable name')
            minimum = info.get('MinimumOSVersion')
            family = info.get('UIDeviceFamily')
            capabilities = info.get('UIRequiredDeviceCapabilities')
            if minimum is not None and (not isinstance(minimum, str) or not re.fullmatch(r'\d+(?:\.\d+)*', minimum)):
                raise ValueError('invalid MinimumOSVersion')
            if family is not None and (not isinstance(family, list) or any(type(v) is not int for v in family)):
                raise ValueError('invalid UIDeviceFamily')
            if capabilities is not None and not (
                    isinstance(capabilities, list) and all(isinstance(v, str) for v in capabilities)
                    or isinstance(capabilities, dict) and all(isinstance(k, str) and type(v) is bool for k, v in capabilities.items())):
                raise ValueError('invalid UIRequiredDeviceCapabilities')
            binary = (app / executable).read_bytes()
        except (OSError, ValueError, KeyError, TypeError, OverflowError, ExpatError, plistlib.InvalidFileException) as exc:
            raise IPAError(f'Invalid Info.plist or executable: {exc}') from exc
        return dict(bundle_id=bundle_id, executable=executable, app_path='Payload/' + app.name,
                    minimum_os_version=info.get('MinimumOSVersion'),
                    device_family=info.get('UIDeviceFamily'),
                    required_capabilities=info.get('UIRequiredDeviceCapabilities'),
                    selected_slice=select_slice(binary))


def make_synthetic_ipa(path, *, architecture='arm64', info=None):
    """Write a header-only IPA fixture (not a runnable app)."""
    cpu, sub = {'arm64': (0x100000c, 0), 'armv7': (12, 9), 'armv7s': (12, 11)}[architecture]
    binary = struct.pack('<7I', 0xfeedfacf if architecture == 'arm64' else 0xfeedface,
                         cpu, sub, 2, 0, 0, 0)
    if architecture == 'arm64':
        binary += bytes(4)
    metadata = dict(CFBundleIdentifier='org.vphone.hello', CFBundleExecutable='Hello',
                    MinimumOSVersion='7.0', UIDeviceFamily=[1], UIRequiredDeviceCapabilities=['arm64'])
    if info:
        metadata.update(info)
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('Payload/Hello.app/Info.plist', plistlib.dumps(metadata))
        entry = zipfile.ZipInfo('Payload/Hello.app/Hello')
        entry.external_attr = (stat.S_IFREG | 0o755) << 16
        archive.writestr(entry, binary)
    return Path(path)
