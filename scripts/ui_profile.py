#!/usr/bin/env python3
"""Offline UI preparation. progress receives schema-1 event dictionaries; no VM is booted."""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import warnings
import zipfile

from bundle import digest, resolve_artifact
from prepare_diagnostic_disks import clone

ROOT = Path(__file__).resolve().parents[1]
UI_ARGS = ('-v serial=3 debug=0x14e cpus=2 csi_disable_watchdog=1 '
           'amfi_allow_any_signature=1 amfi_get_out_of_my_way=1 cs_enforcement_disable=1')
SEP_NAME = 'sep-firmware.n53.RELEASE.im4p'
SEP_MEMBER = 'Firmware/all_flash/all_flash.n53ap.production/' + SEP_NAME
STAGES = ('sep-extract', 'diagnostic-disks', 'virtual-activation', 'boot-plan', 'ui-manifest')


def event(progress, stage, status, message, hint=None):
    if progress:
        progress(dict(schema=1, event='stage', id=stage, status=status,
                      fraction=1.0 if status in ('done', 'skipped') else 0.0,
                      message=message, hint=hint))


def extract_sep(bundle, out, ipsw=None):
    bundle, out = Path(bundle), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    target = out / SEP_NAME
    if not target.exists():
        manifest = json.loads((bundle / 'vm_manifest.json').read_text())
        cached = bundle / manifest.get('ui', {}).get('generation', 'original') / SEP_NAME
        if cached.is_file():
            clone(cached, target)
        else:
            firmware = json.loads((bundle / 'vm_manifest.json').read_text())['firmware']
            source = ipsw or firmware.get('ipsw_path') or firmware.get('ipsw_filename')
            if not source or not Path(source).is_file():
                raise ValueError('Local IPSW not found; pass --ipsw PATH')
            if firmware.get('ipsw_sha256') and digest(source) != firmware['ipsw_sha256']:
                raise ValueError('IPSW checksum differs from bundle firmware')
            with zipfile.ZipFile(source) as archive:
                if archive.namelist().count(SEP_MEMBER) != 1:
                    raise ValueError('Missing or duplicate n53 SEP firmware in IPSW')
                payload = archive.read(SEP_MEMBER)
                if not payload:
                    raise ValueError('Empty SEP firmware')
                target.write_bytes(payload)
    reference = ROOT / 'research/firmware-artifacts' / SEP_NAME
    if reference.is_file() and digest(reference) != digest(target):
        warnings.warn('SEP firmware differs from developer reference', stacklevel=2)
    return target


def build_boot(bundle, out):
    from qemu.boot_image import build_image
    bundle = Path(bundle)
    manifest = json.loads((bundle / 'vm_manifest.json').read_text())
    artifact = lambda name: resolve_artifact(bundle, manifest['artifacts'][name])
    return build_image(artifact('kernel'), artifact('devicetree'), out, UI_ARGS,
                       monitor_path=artifact('monitor'), ans_path=artifact('ans_firmware'),
                       debug_enabled=True)


def relocate_plan(plan_path, old, new):
    """Rewrite only paths owned by the staged profile, including auxiliary images."""
    old, new = str(old), str(new)
    def walk(value):
        if isinstance(value, str) and value.startswith(old + os.sep):
            return new + value[len(old):]
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value
    value = walk(json.loads(Path(plan_path).read_text()))
    Path(plan_path).write_text(json.dumps(value, indent=2) + '\n')
    return value


def _record(path, relative, mutable=False):
    result = dict(path=str(relative), size=path.stat().st_size, sha256=digest(path))
    if mutable:
        result['mutable'] = True
    return result


def _publish(bundle, work, manifest):
    """Immutable generations + atomic manifest switch keep failures/retries safe."""
    if (bundle / 'ui').is_symlink():
        raise ValueError('UI output directory must remain inside the bundle')
    final = bundle / 'ui' / work.name
    plan = relocate_plan(work / 'boot/boot_plan.json', work, final)
    system = _record(work / 'system-runtime.raw', final.relative_to(bundle) / 'system-runtime.raw', True)
    source = manifest['artifacts']['system_disk']
    system.update(source_artifact='system_disk', source_sha256=source['sha256'])
    manifest['artifacts']['system_runtime_disk'] = system
    manifest['artifacts']['data_disk'] = _record(work / 'data.raw', final.relative_to(bundle) / 'data.raw', True)
    if plan.get('boot_args') != UI_ARGS:
        raise ValueError('Imported profile must use the validated v5 UI boot arguments')
    manifest.update(profile='ui', boot_args=plan['boot_args'],
                    plan=str(final.relative_to(bundle) / 'boot/boot_plan.json'),
                    ui=dict(generation=str(final.relative_to(bundle)), debug_enabled=True,
                            home_screen_verified=False, experimental=True))
    final.parent.mkdir(exist_ok=True)
    work.rename(final)
    temporary = bundle / '.vm_manifest.ui.tmp'
    try:
        temporary.write_text(json.dumps(manifest, indent=2) + '\n')
        os.replace(temporary, bundle / 'vm_manifest.json')
    except BaseException:
        temporary.unlink(missing_ok=True)
        shutil.rmtree(final)
        raise
    return manifest


def _pipeline(bundle, progress, force, runner, source=None, ipsw=None):
    bundle = Path(bundle).resolve()
    with (bundle / 'runtime.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('VM must be stopped before preparing UI') from exc
        manifest = json.loads((bundle / 'vm_manifest.json').read_text())
        if manifest.get('profile') == 'ui' and not force:
            for name in ('system_runtime_disk', 'data_disk'):
                record = manifest['artifacts'][name]
                if resolve_artifact(bundle, record).stat().st_size != record['size']:
                    raise ValueError('UI disk size changed; inspect bundle before retrying')
            plan_path = resolve_artifact(bundle, {'path': manifest['plan']})
            plan = json.loads(plan_path.read_text())
            for path in [plan['image']] + [a['path'] for a in plan.get('auxiliary_images', [])]:
                relative = Path(path).resolve().relative_to(bundle)
                resolve_artifact(bundle, {'path': str(relative)})
            for stage in STAGES:
                event(progress, stage, 'skipped', 'UI profile already prepared; use --force to rebuild')
            event(progress, 'boot-check', 'skipped', 'Offline preparation; runtime verification pending')
            return manifest
        work = Path(tempfile.mkdtemp(prefix='.ui-preparing-', dir=bundle))
        stage = STAGES[0]
        try:
            for stage in STAGES[:-1]:
                if source is not None and stage in ('sep-extract', 'diagnostic-disks'):
                    event(progress, stage, 'skipped', 'Import uses existing prepared disks')
                    continue
                event(progress, stage, 'start', stage)
                with contextlib.redirect_stdout(sys.stderr):
                    runner(stage, bundle, work, source, ipsw)
                event(progress, stage, 'done', stage)
            stage = 'ui-manifest'
            event(progress, stage, 'start', 'Publishing persistent UI profile')
            result = _publish(bundle, work, manifest)
            event(progress, stage, 'done', 'Published persistent UI profile')
            event(progress, 'boot-check', 'skipped', 'Offline preparation; run e2e_ui_boot.py to verify Home')
            return result
        except BaseException as exc:
            event(progress, stage, 'error', str(exc), 'Check local IPSW and preparation tools; retry with --force')
            raise
        finally:
            if work.exists():
                shutil.rmtree(work)


def _runner(stage, bundle, work, source, ipsw):
    from prepare_diagnostic_disks import prepare_system, prepare_data
    from prepare_virtual_activation import prepare_profile
    if source is not None:
        if stage == 'virtual-activation':
            source = Path(source).resolve()
            clone(source / 'system.raw', work / 'system-runtime.raw')
            clone(source / 'data.raw', work / 'data.raw')
        elif stage == 'boot-plan':
            import_boot(Path(source) / 'boot', work / 'boot')
        return
    if stage == 'sep-extract':
        extract_sep(bundle, work, ipsw)
    elif stage == 'diagnostic-disks':
        diagnostic = work / 'diagnostic'
        diagnostic.mkdir()
        prepare_system(bundle, diagnostic, work / SEP_NAME)
        # Resolve baseline Data explicitly, including subsequent forced rebuilds.
        baseline = bundle / 'data.raw'
        if not baseline.is_file():
            raise ValueError('Missing baseline data.raw')
        prepare_data(bundle, diagnostic)
    elif stage == 'virtual-activation':
        prepare_profile(work / 'diagnostic/system-diagnostic-sep.raw',
                        work / 'diagnostic/data-diagnostic-systembag.raw', work / 'activation')
        (work / 'activation/system.raw').rename(work / 'system-runtime.raw')
        (work / 'activation/data.raw').rename(work / 'data.raw')
        shutil.rmtree(work / 'diagnostic')
    elif stage == 'boot-plan':
        build_boot(bundle, work / 'boot')


def import_boot(source, out):
    """Materialize external references so imported plans are self contained."""
    source, out = Path(source), Path(out)
    plan = json.loads((source / 'boot_plan.json').read_text())
    out.mkdir()
    for name in ('KernelMemory.bin', 'DeviceTree.runtime', 'boot_args.bin'):
        clone(source / name, out / name)
    if digest(out / 'KernelMemory.bin') != plan['image_sha256']:
        raise ValueError('Imported boot image checksum mismatch')
    plan['image'] = str(out / 'KernelMemory.bin')
    for index, record in enumerate(plan.get('auxiliary_images', [])):
        path = Path(record['path'])
        path = path if path.is_absolute() else source / path
        target = out / f'auxiliary-{index}.bin'
        clone(path, target)
        if digest(target) != record['sha256']:
            raise ValueError('Imported auxiliary image checksum mismatch')
        record['path'] = str(target)
    (out / 'boot_plan.json').write_text(json.dumps(plan, indent=2) + '\n')


def prepare_ui(bundle, progress=None, force=False, *, ipsw=None, runner=None):
    return _pipeline(bundle, progress, force, runner or _runner, ipsw=ipsw)


def import_profile(bundle, source_profile_dir, progress=None, force=False, *, runner=None):
    return _pipeline(bundle, progress, force, runner or _runner, source=source_profile_dir)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('prepare-ui', 'import-profile'):
        p = sub.add_parser(name)
        p.add_argument('bundle', type=Path)
        p.add_argument('--force', action='store_true')
        p.add_argument('--progress-json', action='store_true')
        if name == 'prepare-ui':
            p.add_argument('--ipsw', type=Path)
        else:
            p.add_argument('--from', dest='source', required=True, type=Path)
    args = parser.parse_args()
    emit = lambda value: print(json.dumps(value), flush=True)
    progress = emit if args.progress_json else None
    try:
        if args.command == 'prepare-ui':
            prepare_ui(args.bundle, progress, args.force, ipsw=args.ipsw)
        else:
            import_profile(args.bundle, args.source, progress, args.force)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        if progress:
            emit(dict(schema=1, event='result', ok=False, data={'error': str(exc)}))
        return 1
    if progress:
        emit(dict(schema=1, event='result', ok=True, data={'path': str(args.bundle.resolve())}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
