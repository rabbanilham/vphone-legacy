"""`vphone-legacy app ...`: offline sideloading into a VM bundle (VM must be stopped)."""
import fcntl
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'qemu'))

from bundle import resolve_artifact  # noqa: E402
import app_install  # noqa: E402

AMFI = app_install.AMFI_ARGS


class AppError(RuntimeError):
    pass


def _manifest(bundle):
    path = Path(bundle) / 'vm_manifest.json'
    try:
        return path, json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise AppError(f'Unreadable VM manifest: {exc}') from exc


def _write(path, manifest):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(manifest, indent=2) + '\n')
    tmp.replace(path)


def _images(bundle, manifest):
    artifacts = manifest.get('artifacts', {})
    system = artifacts.get('system_runtime_disk') or artifacts.get('system_disk')
    data = artifacts.get('data_disk')
    if not system or not data:
        raise AppError('VM has no system/data disk in its manifest')
    system, data = resolve_artifact(bundle, system), resolve_artifact(bundle, data)
    if Path(system).name == 'system.raw':
        raise AppError('The pristine system.raw is immutable; launch the VM once to create system-runtime.raw')
    return Path(system), Path(data)


class _Lock:
    """Hold the launcher lock so no VM can start while images are modified."""
    def __init__(self, bundle):
        self.path = Path(bundle) / 'runtime.lock'

    def __enter__(self):
        self.fd = open(self.path, 'a+')
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.fd.close()
            raise AppError('VM has an active launcher; stop it first (lock held)') from exc
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        self.fd.close()


def _stage(emitter, id, status, message, fraction=None):
    if emitter:
        emitter.stage(id, status, message, fraction, None)


def install(bundle, ipa, enable_unsigned=False, emitter=None):
    bundle = Path(bundle)
    path, manifest = _manifest(bundle)
    boot_args = manifest.get('boot_args', '')
    relaxed = app_install.boot_args_ok(boot_args)
    if not relaxed and not enable_unsigned:
        raise AppError('This VM boots with full code-signing enforcement, so ad-hoc apps would be killed. '
                       'Retry with --enable-unsigned to add the relaxed AMFI boot arguments to this VM only.')
    with _Lock(bundle):
        system, data = _images(bundle, manifest)
        _stage(emitter, 'inspect', 'start', 'Inspecting IPA')
        try:
            report = app_install.install_app(system, data, ipa)
        except app_install.InstallError as exc:
            raise AppError(str(exc)) from exc
        _stage(emitter, 'copy', 'done', f"Installed {report['app_path']}", 1.0)
        if not relaxed:
            manifest['boot_args'] = (boot_args + ' ' + ' '.join(a for a in AMFI if a not in boot_args.split())).strip()
        apps = [a for a in manifest.get('apps', []) if a.get('bundle_id') != report['bundle_id']]
        apps.append({'bundle_id': report['bundle_id'], 'path': report['app_path']})
        manifest['apps'] = apps
        _write(path, manifest)
    return report


def list_apps(bundle):
    _, manifest = _manifest(bundle)
    return manifest.get('apps', [])


def remove(bundle, bundle_id, emitter=None):
    bundle = Path(bundle)
    with _Lock(bundle):
        path, manifest = _manifest(bundle)
        entry = next((a for a in manifest.get('apps', []) if a.get('bundle_id') == bundle_id), None)
        if not entry:
            raise AppError(f'{bundle_id} is not recorded as installed in this VM')
        system, data = _images(bundle, manifest)
        try:
            report = app_install.remove_app(system, data, Path(entry['path']).name)
        except app_install.InstallError as exc:
            raise AppError(str(exc)) from exc
        manifest['apps'] = [a for a in manifest['apps'] if a.get('bundle_id') != bundle_id]
        _write(path, manifest)
    return report
