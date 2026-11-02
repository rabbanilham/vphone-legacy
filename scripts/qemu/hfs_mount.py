"""Mount disposable raw HFS images. Callers must stop the VM before attaching."""
from contextlib import contextmanager
from pathlib import Path
import plistlib
import subprocess
from typing import NamedTuple


class MountedVolume(NamedTuple):
    device: str
    mount_point: Path


def detach(device):
    """Retry a busy detach once, then force; never silently leak a device."""
    for _ in range(2):
        result = subprocess.run(['hdiutil', 'detach', str(device)], capture_output=True, text=True)
        if result.returncode == 0:
            return
    subprocess.run(['hdiutil', 'detach', '-force', str(device)],
                   capture_output=True, text=True, check=True)


def attach(image, readonly=False, *, raw=True):
    cmd = ['hdiutil', 'attach', '-plist', '-nobrowse', '-noautoopen']
    # Do not skip verification for arbitrary disk images. Raw images have no checksum.
    if raw:
        cmd += ['-noverify', '-imagekey', 'diskimage-class=CRawDiskImage']
    if readonly:
        cmd.append('-readonly')
    cmd.append(str(image))
    result = subprocess.run(cmd, capture_output=True, check=True)
    devices = []
    try:
        entities = plistlib.loads(result.stdout)['system-entities']
        devices = [e['dev-entry'] for e in entities if 'dev-entry' in e]
        volumes = [e for e in entities if 'mount-point' in e]
        if not devices or len(volumes) != 1:
            raise RuntimeError('Expected exactly one mounted filesystem')
        return MountedVolume(devices[0], Path(volumes[0]['mount-point']))
    except Exception:
        if devices:
            detach(devices[0])
        raise


@contextmanager
def mounted(image, readonly=False, **kwargs):
    """Yield MountedVolume(device, mount_point); detach even if the body fails."""
    volume = attach(image, readonly, **kwargs)
    try:
        yield volume
    finally:
        detach(volume.device)
