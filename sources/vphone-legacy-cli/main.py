#!/usr/bin/env python3
"""Manage validated firmware and experimental A7 kernel bring-up."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = PROJECT_ROOT / 'scripts'
sys.path.insert(0, str(SCRIPTS_DIR))
from bundle import validate_bundle
from fw_manifest import LEGACY_FIRMWARES
from qemu.runtime import stop


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
    vm.add_parser('list')
    create = vm.add_parser('create')
    create.add_argument('name')
    create.add_argument('--ios', default='7.1.2', choices=['7.1.2'])
    create.add_argument('--ipsw', required=True, help='Local n53ap iPhone6,2 IPSW; missing firmware is an error')
    launch = vm.add_parser('launch')
    launch.add_argument('name')
    launch.add_argument('--headless', action='store_true')
    launch.add_argument('--probe-seconds', type=float, help='Bounded bring-up probe (0 < seconds <= 30), records actual registers and framebuffer')
    launch.add_argument('--paused', action='store_true', help='Start paused for QMP/debugging')
    launch.add_argument('--wifi', action='store_true', help='Expose host-mirrored Wi-Fi scan list backed by user-mode NAT')
    doctor = vm.add_parser('doctor')
    doctor.add_argument('name')
    doctor.add_argument('--json', action='store_true')
    doctor.add_argument('--firmware-only', action='store_true')
    status = vm.add_parser('status')
    status.add_argument('name')
    shutdown = vm.add_parser('stop')
    shutdown.add_argument('name')
    shutdown.add_argument('--force', action='store_true', help='Terminate through QMP; guest filesystem shutdown is NOT clean')
    fw = groups.add_parser('fw').add_subparsers(dest='action', required=True)
    fw.add_parser('list')
    groups.add_parser('gui')
    args = parser.parse_args(argv)
    try:
        if args.group == 'gui':
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
            if path.exists():
                raise ValueError('VM already exists. Choose a new name; existing bundles are never overwritten.')
            ipsw = Path(args.ipsw).expanduser().resolve()
            if not ipsw.is_file():
                raise ValueError('IPSW file does not exist')
            subprocess.run([sys.executable, str(SCRIPTS_DIR / 'fw_prepare.py'), '--version', args.ios,
                            '--ipsw', str(ipsw), '--out-dir', str(path)], check=True)
            print(f'Prepared firmware: {path}. iOS boot and UI are not yet verified.')
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
            # Replace the CLI so GUI signals reach the single owning launcher.
            os.execv(sys.executable, command)
        elif args.action == 'stop':
            stop(vm_path(args.name), args.force)
            print('Forced termination requested; shutdown unclean.' if args.force else 'Guest shutdown requested; await verified completion.')
        elif args.action == 'status':
            print((vm_path(args.name) / 'runtime.json').read_text())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
