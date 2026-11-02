"""Read-only prerequisite checks and explicit, streaming setup repairs."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

PACKAGES = ('python@3.13', 'keystone', 'glib', 'pixman', 'openssl@3', 'qemu', 'libslirp', 'pcre2')
SETUP_PATH = '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin'


def run(command, **kwargs):
    try:
        return subprocess.run(command, capture_output=True, text=True, **kwargs)
    except OSError as exc:
        return subprocess.CompletedProcess(command, 127, '', str(exc))


def check(root, vms, runner=run, exists=None, free_bytes=None):
    root, vms = Path(root), Path(vms)
    exists = exists or (lambda p: Path(p).exists())
    env = {**os.environ, 'PATH': SETUP_PATH}
    def probe(command):
        return runner(command, cwd=str(root), env=env)
    items = []
    def add(id, title, ok, detail, command=None, instructions=None, fixable=False):
        items.append(dict(id=id, title=title, status='ok' if ok else ('fixable' if fixable else 'missing'),
                          detail=detail, fix=None if ok else dict(kind='auto' if command else 'manual',
                          command=command, instructions=instructions)))
    clt = probe(['xcode-select', '-p'])
    add('xcode-clt', 'Xcode Command Line Tools', clt.returncode == 0, clt.stdout.strip() or 'Not installed', instructions='Run xcode-select --install')
    brew = next((p for p in ('/opt/homebrew/bin/brew', '/usr/local/bin/brew') if exists(p)), None)
    add('homebrew', 'Homebrew', bool(brew), brew or 'Not installed', instructions='Install Homebrew from https://brew.sh')
    missing = []
    for package in PACKAGES:
        result = probe([brew or 'brew', 'list', '--versions', package])
        if result.returncode or not result.stdout.strip():
            missing.append(package)
    add('brew-packages', 'Homebrew packages', not missing, 'missing: ' + ', '.join(missing) if missing else 'All packages installed',
        command='brew install ' + ' '.join(missing), fixable=bool(brew))
    python = root / '.venv/bin/python'
    venv_ok = exists(python) and probe([str(python), '-c', 'import keystone, typer']).returncode == 0
    add('venv', 'Python environment', venv_ok, str(python), command='scripts/setup_venv.sh', fixable=True)
    binary = root / 'backend/bin/qemu-system-aarch64'
    backend_ok = False
    try:
        manifest = json.loads((root / 'backend/backend.json').read_text())
        expected = manifest.get('binary_sha256') or manifest.get('sha256')
        if binary.is_file() and expected:
            digest = hashlib.sha256()
            with binary.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            backend_ok = digest.hexdigest() == expected
    except (OSError, ValueError):
        pass
    add('backend', 'QEMU backend', backend_ok, 'Verified backend hash' if backend_ok else 'backend/bin/qemu-system-aarch64 missing or hash mismatch', command='scripts/qemu/build_backend.py --fetch --install-build-deps')
    swift = probe(['xcrun', '--find', 'swiftc']).returncode == 0
    player = root / 'build/player/vphone-player.app/Contents/MacOS/vphone-player'
    add('player', 'iPhone player', exists(player), str(player), command="python -c 'from backend.live_run import build_player; build_player()'" if swift else None,
        instructions=None if swift else 'Install Xcode Command Line Tools', fixable=swift)
    add('vfdecrypt', 'VFDecrypt', exists(root / 'dist/vfdecrypt'), 'dist/vfdecrypt', command="python -c 'from scripts.fw_prepare import ensure_vfdecrypt; ensure_vfdecrypt(\".\")'", fixable=True)
    qemu = probe(['qemu-img', '--version'])
    add('qemu-img', 'QEMU disk tools', qemu.returncode == 0, qemu.stdout.strip() or 'qemu-img missing', command='brew install qemu', fixable=bool(brew))
    ancestor = vms
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    available = free_bytes if free_bytes is not None else shutil.disk_usage(ancestor).free
    add('disk-space', 'VM disk space', available >= 20 * 1024**3, f'{available} bytes free in {ancestor}', instructions='Free at least 20 GiB in the VM directory')
    add('wifi-helper', 'Wi-Fi scan helper (optional)', exists(root / 'build/host_wifi_scan.app'), 'build/host_wifi_scan.app', command='make wifi-helper')
    return dict(schema=1, ok=all(i['status'] == 'ok' for i in items if i['id'] != 'wifi-helper'), items=items)


def install(id, root, vms, emitter=None, runner=run, popen=subprocess.Popen):
    report = check(root, vms, runner=runner)
    item = next((i for i in report['items'] if i['id'] == id), None)
    if item is None:
        raise ValueError('Unknown setup item: ' + id)
    if item['status'] == 'ok':
        if emitter:
            emitter.stage(id, 'skipped', 'Already installed', 1.0)
            emitter.result(True, {'id': id})
        return
    fix = item['fix']
    if fix['kind'] != 'auto':
        raise ValueError(fix['instructions'])
    root = Path(root)
    commands = {
        'venv': ['zsh', str(root / 'scripts/setup_venv.sh')],
        'backend': [str(root / '.venv/bin/python'), str(root / 'scripts/qemu/build_backend.py'), '--fetch', '--install-build-deps'],
        'player': [str(root / '.venv/bin/python'), '-c', 'from backend.live_run import build_player; build_player()'],
        'vfdecrypt': [str(root / '.venv/bin/python'), '-c', 'import sys; sys.path.insert(0,"scripts"); from fw_prepare import ensure_vfdecrypt; ensure_vfdecrypt(".")'],
    }
    command = commands.get(id) or shlex.split(fix['command'])
    if emitter:
        emitter.stage(id, 'start', 'Installing ' + item['title'])
    env = {**os.environ, 'PATH': SETUP_PATH, 'VPHONE_LEGACY_ROOT': str(root)}
    process = popen(command, cwd=str(root), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors='replace')
    for line in process.stdout:
        print(line.rstrip(), file=sys.stderr, flush=True)
        if emitter:
            emitter.log(line.rstrip())
    if process.wait():
        raise RuntimeError('Setup failed. Retry: ' + fix['command'])
    updated = check(root, vms, runner=runner)
    verified = next(i for i in updated['items'] if i['id'] == id)
    if verified['status'] != 'ok':
        raise RuntimeError('Installation finished but prerequisite check failed: ' + verified['detail'])
    if emitter:
        emitter.stage(id, 'done', 'Installed ' + item['title'], 1.0)
        emitter.result(True, {'id': id})
