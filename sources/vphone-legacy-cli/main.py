#!/usr/bin/env python3
"""Manage validated firmware and experimental A7 kernel bring-up."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

PROJECT_ROOT = Path(os.environ.get('VPHONE_LEGACY_ROOT', Path(__file__).resolve().parents[2])).expanduser().resolve()
SCRIPTS_DIR = PROJECT_ROOT / 'scripts'
sys.path.insert(0, str(SCRIPTS_DIR))
from bundle import validate_bundle
from fw_manifest import LEGACY_FIRMWARES
from qemu.runtime import stop
from cli_json import Emitter, error_hint
from setup_vm import cleanup_stale, delete_bundle, status as vm_status, summary
import setup_check
import app_cli


def vm_root():
    return Path(os.environ.get('VPHONE_LEGACY_VMS_DIR', str(Path.home() / '.vphone-legacy/VMs'))).expanduser().resolve()


def vm_path(name):
    if not name or name in ('.', '..') or Path(name).name != name or '\\' in name or '\0' in name:
        raise ValueError('VM name must be a single directory name')
    path = (vm_root() / name).resolve()
    if path.parent != vm_root():
        raise ValueError('VM path escapes the configured VM directory')
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(prog='vphone-legacy', description=__doc__)
    groups = parser.add_subparsers(dest='group', required=True)
    vm = groups.add_parser('vm').add_subparsers(dest='action', required=True)
    vm.add_parser('list').add_argument('--json', action='store_true')
    delete = vm.add_parser('delete')
    delete.add_argument('name')
    delete.add_argument('--json', action='store_true')
    create = vm.add_parser('create')
    create.add_argument('name')
    create.add_argument('--force', action='store_true')
    create.add_argument('--progress-json', action='store_true')
    create.add_argument('--ios', default='7.1.2', choices=['7.1.2'])
    create.add_argument('--ipsw', required=True, help='Local n53ap iPhone6,2 IPSW; missing firmware is an error')
    launch = vm.add_parser('launch')
    launch.add_argument('name')
    launch.add_argument('--headless', action='store_true')
    launch.add_argument('--probe-seconds', type=float, help='Bounded bring-up probe (0 < seconds <= 30), records actual registers and framebuffer')
    launch.add_argument('--paused', action='store_true', help='Start paused for QMP/debugging')
    launch.add_argument('--display', choices=['player', 'cocoa', 'none'], help='Display backend (default: player for UI profiles, cocoa otherwise)')
    launch.add_argument('--wifi', action='store_true', help='Expose host-mirrored Wi-Fi scan list backed by user-mode NAT')
    prep = vm.add_parser('prepare-ui')
    prep.add_argument('name')
    prep.add_argument('--force', action='store_true')
    prep.add_argument('--progress-json', action='store_true')
    prep.add_argument('--ipsw', help='IPSW to extract SEP firmware from (default: recorded in the manifest)')
    imp = vm.add_parser('import-profile')
    imp.add_argument('name')
    imp.add_argument('--from', dest='source', required=True)
    imp.add_argument('--force', action='store_true')
    imp.add_argument('--progress-json', action='store_true')
    doctor = vm.add_parser('doctor')
    doctor.add_argument('name')
    doctor.add_argument('--json', action='store_true')
    doctor.add_argument('--firmware-only', action='store_true')
    status = vm.add_parser('status')
    status.add_argument('name')
    status.add_argument('--json', action='store_true')
    shutdown = vm.add_parser('stop')
    shutdown.add_argument('name')
    shutdown.add_argument('--force', action='store_true', help='Terminate through QMP; guest filesystem shutdown is NOT clean')
    app = groups.add_parser('app').add_subparsers(dest='action', required=True)
    app_install_p = app.add_parser('install')
    app_install_p.add_argument('name')
    app_install_p.add_argument('ipa')
    app_install_p.add_argument('--enable-unsigned', action='store_true')
    app_install_p.add_argument('--progress-json', action='store_true')
    app_install_p.add_argument('--json', action='store_true')
    app_list_p = app.add_parser('list')
    app_list_p.add_argument('name')
    app_list_p.add_argument('--json', action='store_true')
    app_remove_p = app.add_parser('remove')
    app_remove_p.add_argument('name')
    app_remove_p.add_argument('bundle_id')
    app_remove_p.add_argument('--json', action='store_true')
    fw = groups.add_parser('fw').add_subparsers(dest='action', required=True)
    fw.add_parser('list')
    groups.add_parser('gui')
    setup = groups.add_parser('setup').add_subparsers(dest='action', required=True)
    setup.add_parser('check').add_argument('--json', action='store_true')
    install = setup.add_parser('install')
    install.add_argument('id')
    install.add_argument('--progress-json', action='store_true')
    args = parser.parse_args(argv)
    emitter = Emitter() if getattr(args, 'progress_json', False) else None
    try:
        if args.group == 'setup':
            if args.action == 'check':
                report = setup_check.check(PROJECT_ROOT, vm_root())
                if args.json:
                    print(json.dumps(report))
                else:
                    for item in report['items']:
                        print(f"{item['id']}: {item['status']} — {item['detail']}")
                return 0 if report['ok'] else 1
            setup_check.install(args.id, PROJECT_ROOT, vm_root(), emitter)
        elif args.group == 'app':
            path = vm_path(args.name)
            if not path.is_dir():
                raise ValueError('VM does not exist')
            if args.action == 'install':
                report = app_cli.install(path, Path(args.ipa).expanduser().resolve(), args.enable_unsigned, emitter)
                if emitter:
                    emitter.result(True, report)
                elif args.json:
                    print(json.dumps(dict(schema=1, ok=True, data=report)))
                else:
                    print(f"Installed {report['bundle_id']} at {report['app_path']}")
            elif args.action == 'list':
                apps = app_cli.list_apps(path)
                print(json.dumps(dict(schema=1, apps=apps)) if args.json else '\n'.join(f"{a['bundle_id']}  {a['path']}" for a in apps))
            else:
                report = app_cli.remove(path, args.bundle_id)
                print(json.dumps(dict(schema=1, ok=True, data=report)) if args.json else f'Removed {args.bundle_id}')
        elif args.group == 'gui':
            app = PROJECT_ROOT / 'dist/vphone-legacy.app'
            if not app.exists():
                subprocess.run([str(SCRIPTS_DIR / 'build_app.sh')], check=True)
            subprocess.run(['open', str(app)], check=True)
        elif args.group == 'fw':
            for version, meta in LEGACY_FIRMWARES.items():
                target = version == '7.1.2'
                print(f"iOS {version} ({meta['build']}) — {meta['device']} ({meta['board']}): "
                      + ('firmware preparation supported; boot/UI experimental' if target else 'unsupported in this implementation'))
        elif args.action == 'list':
            root = vm_root()
            cleanup_stale(root)
            if args.json:
                paths = sorted(root.iterdir()) if root.exists() else []
                print(json.dumps(dict(schema=1, root=str(root), vms=[summary(p) for p in paths if p.is_dir() and (p / 'vm_manifest.json').is_file()])))
                return 0
            if not root.exists():
                print('No VMs. Use vm create NAME --ipsw FILE.')
                return 0
            for path in sorted(root.iterdir()):
                if not path.is_dir():
                    continue
                try:
                    manifest = json.loads((path / 'vm_manifest.json').read_text())
                    if not isinstance(manifest, dict):
                        raise ValueError('Manifest must be an object')
                    state = 'prepared; boot unverified' if manifest.get('schema_version') == 2 else 'legacy bundle — rebuild required'
                    print(f"{path.name}: iOS {manifest.get('version')} / {manifest.get('device')} — {state}")
                except (OSError, ValueError):
                    print(f'{path.name}: invalid/incomplete bundle')
        elif args.action == 'create':
            path = vm_path(args.name)
            command = [sys.executable, str(SCRIPTS_DIR / 'fw_prepare.py'), '--version', args.ios,
                       '--ipsw', str(Path(args.ipsw).expanduser().resolve()), '--out-dir', str(path)]
            if args.force:
                command.append('--force')
            if emitter:
                command.append('--progress-json')
            result = subprocess.run(command)
            if result.returncode:
                return result.returncode
            if not emitter:
                print(f'Prepared firmware: {path}. iOS boot and UI are not yet verified.')
        elif args.action in ('prepare-ui', 'import-profile'):
            path = vm_path(args.name)
            if not path.is_dir():
                raise ValueError('VM does not exist')
            command = [sys.executable, str(SCRIPTS_DIR / 'ui_profile.py'), args.action, str(path)]
            if args.force:
                command.append('--force')
            if getattr(args, 'progress_json', False):
                command.append('--progress-json')
            if args.action == 'prepare-ui' and args.ipsw:
                command += ['--ipsw', str(Path(args.ipsw).expanduser().resolve())]
            if args.action == 'import-profile':
                command += ['--from', str(Path(args.source).expanduser().resolve())]
            result = subprocess.run(command)
            if result.returncode:
                return result.returncode
        elif args.action == 'delete':
            path = vm_path(args.name)
            if not path.is_dir():
                raise ValueError('VM does not exist')
            delete_bundle(path)
            if args.json:
                print(json.dumps(dict(schema=1, ok=True, data={'name': args.name, 'path': str(path)})))
            else:
                print(f'Deleted VM: {args.name}')
        elif args.action == 'doctor':
            report = validate_bundle(vm_path(args.name), check_backend=not args.firmware_only)
            if args.json:
                print(json.dumps(report, indent=2))
            else:
                for check in report['checks']:
                    print(('PASS ' if check['ok'] else 'FAIL ') + check['name'] + ('' if check['ok'] else ': ' + str(check['detail'])))
                for limitation in report['limitations']:
                    print('UNVERIFIED ' + limitation)
                print('Firmware prepared: ' + ('yes' if report['prepared'] else 'no'))
                print('iOS boot/UI acceptance: not verified')
            return 1 if report['errors'] else 0
        elif args.action == 'launch':
            command = [sys.executable, str(SCRIPTS_DIR / 'qemu/runtime.py'), str(vm_path(args.name))]
            if args.headless:
                command.append('--headless')
            if args.paused:
                command.append('--paused')
            if args.probe_seconds is not None:
                command += ['--probe-seconds', str(args.probe_seconds)]
            if args.wifi:
                command.append('--wifi')
            if args.display:
                command += ['--display', args.display]
            # Replace the CLI so GUI signals reach the single owning launcher.
            os.execv(sys.executable, command)
        elif args.action == 'stop':
            stop(vm_path(args.name), args.force)
            print('Forced termination requested; shutdown unclean.' if args.force else 'Guest shutdown requested; await verified completion.')
        elif args.action == 'status':
            path = vm_path(args.name)
            if not path.is_dir():
                raise ValueError('VM does not exist')
            print(json.dumps(vm_status(path)) if args.json else (path / 'runtime.json').read_text())
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as exc:
        hint = error_hint(exc)
        if emitter and not emitter.finished:
            emitter.error(str(exc), hint)
            emitter.result(False, {'exit_code': 1})
        elif getattr(args, 'json', False):
            print(json.dumps(dict(schema=1, ok=False, error=str(exc), hint=hint)))
        print(f'Error: {exc}\nHint: {hint}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
