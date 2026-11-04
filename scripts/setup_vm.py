"""Non-booting bundle inspection and exclusive management operations."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shutil
import time


def read_json(path, default=None):
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else (default or {})
    except (OSError, ValueError):
        return default or {}


@contextmanager
def bundle_lock(path):
    path = Path(path)
    # Existing lock inode must survive until deletion completes.
    with (path / 'runtime.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('VM runtime.lock is held; VM may be running') from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def lock_state(path):
    lock = Path(path) / 'runtime.lock'
    if not lock.exists():
        return 'free'
    with lock.open('r') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 'held'
        fcntl.flock(stream, fcntl.LOCK_UN)
    return 'free'


def cleanup_stale(root, name=None):
    root = Path(root)
    if not root.exists():
        return
    for path in root.iterdir():
        prefix, marker, suffix = path.name.rpartition('.preparing-')
        if not marker or not suffix or (name is not None and prefix != name) or path.is_symlink() or not path.is_dir():
            continue
        # A new directory may not have opened its lock yet. Legacy lockless
        # leftovers are stale after one minute; locked preparations are never removed.
        if not (path / 'runtime.lock').exists() and time.time() - path.stat().st_mtime < 60:
            continue
        # Active preparation holds this lock for the complete operation.
        try:
            with bundle_lock(path):
                shutil.rmtree(path)
        except (ValueError, FileNotFoundError):
            pass


def delete_bundle(path):
    with bundle_lock(path):
        shutil.rmtree(path)


def disk_bytes(path):
    seen = set()
    total = 0
    for directory, dirs, files in os.walk(path, followlinks=False):
        for entry in [Path(directory), *(Path(directory) / name for name in files)]:
            stat = entry.lstat()
            key = (stat.st_dev, stat.st_ino)
            if key not in seen:
                seen.add(key)
                total += stat.st_blocks * 512
    return total


def status(path):
    value = {'schema': 1, 'session_id': None,
             'process': {'phase': 'stopped', 'pid': None, 'exit_code': None},
             'shutdown': {'clean': False, 'reason': None}, 'log_directory': None,
             'control': {}, 'boot_stage': 'none', 'display': 'none'}
    value.update(read_json(Path(path) / 'runtime.json'))
    value['schema'] = 1
    value['lock'] = lock_state(path)
    return value


def summary(path):
    manifest = read_json(path / 'vm_manifest.json')
    runtime = status(path)
    return dict(name=path.name, path=str(path), ios=manifest.get('version'),
                build=manifest.get('build'), device=manifest.get('device'),
                profile=manifest.get('profile', 'baseline'), memory_mb=manifest.get('memory_mb', 1024),
                disk_bytes=disk_bytes(path), phase=runtime['process']['phase'],
                pid=runtime['process'].get('pid'), boot_stage=runtime.get('boot_stage', 'none'),
                apps=manifest.get('apps', []), legacy=manifest.get('schema_version') != 2)
