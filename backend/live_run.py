#!/usr/bin/env python3
"""Run the guest interactively in the native vphone player (no debugger, no hooks).

Defaults to the virtual-activated testing profile and disposable disk clones,
so the source images are never written. Clones are removed on exit unless
--keep-images is given. Explicit --plan/--system/--data override the defaults.
"""
import argparse
from host_wifi import configure_wifi
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import platform
import plistlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from display_probe import FB  # noqa: E402

DEFAULT_PROFILE = ROOT / 'build/virtual-activated-profile-v5'


def clone_or_copy(src, dst):
    try:
        import ctypes
        libc = ctypes.CDLL(None)
        libc.clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
        if libc.clonefile(str(src).encode(), str(dst).encode(), 0) == 0:
            return
    except Exception:
        pass
    shutil.copyfile(src, dst)


def build_player():
    sources = sorted((ROOT / 'sources/vphone-player').glob('*.swift'))
    app = ROOT / 'build/player/vphone-player.app'
    binary = app / 'Contents/MacOS/vphone-player'
    if not binary.exists() or any(p.stat().st_mtime > binary.stat().st_mtime for p in sources):
        binary.parent.mkdir(parents=True, exist_ok=True)
        (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({
            'CFBundleExecutable': 'vphone-player', 'CFBundleIdentifier': 'com.vphone.legacy.player',
            'CFBundleName': 'vphone', 'CFBundlePackageType': 'APPL',
            'NSHighResolutionCapable': True, 'LSMinimumSystemVersion': '14.0',
        }))
        cache = (ROOT / 'build/perf/perf-gpu-present/module-cache').resolve() if (ROOT / 'build/perf/perf-gpu-present').exists() else (ROOT / 'build/player/module-cache').resolve()
        cache.mkdir(parents=True, exist_ok=True)
        subprocess.run(['swiftc', '-O', '-target', f'{platform.machine()}-apple-macosx14.0',
                        '-module-cache-path', str(cache),
                        *map(str, sources), '-o', str(binary)], check=True)
        subprocess.run(['codesign', '--force', '--sign', '-', str(app)], check=True)
    return binary


def run_player(cmd, env, work, player):
    with (work / 'qemu.log').open('wb') as log:
        qemu = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT)
        viewer = None
        try:
            deadline = time.monotonic() + 20
            while not (work / 'vnc').exists():
                if qemu.poll() is not None:
                    raise RuntimeError(f'QEMU gagal: {(work / "qemu.log").read_text()}')
                if time.monotonic() > deadline:
                    raise RuntimeError('QEMU tidak membuka socket layar dalam 20 detik')
                time.sleep(0.05)
            viewer = subprocess.Popen([str(player), str(work / 'vnc'), str(work / 'qmp')])
            return viewer.wait()
        finally:
            if viewer is not None and viewer.poll() is None:
                viewer.terminate()
                viewer.wait(timeout=5)
            if qemu.poll() is None:
                qemu.terminate()
                try:
                    qemu.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    qemu.kill()
                    qemu.wait()
            for name in ('vnc', 'qmp'):
                (work / name).unlink(missing_ok=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--plan', type=Path, default=DEFAULT_PROFILE / 'boot/boot_plan.json')
    p.add_argument('--system', type=Path, default=DEFAULT_PROFILE / 'system.raw')
    p.add_argument('--data', type=Path, default=DEFAULT_PROFILE / 'data.raw')
    p.add_argument('--qemu-binary', type=Path, help='Explicit QEMU binary for a reproducible diagnostic run')
    p.add_argument('--keep-images', action='store_true')
    p.add_argument('--collect-crashlogs', action='store_true',
                   help='Collect guest crash reports from data clone before cleanup')
    p.add_argument('--wifi', action='store_true', help='Expose an open Wi-Fi AP backed by user-mode NAT')
    p.add_argument('--qemu-arg', action='append', default=[])
    args = p.parse_args()
    for path in (args.plan, args.system, args.data):
        if not path.is_file():
            p.error(f'Missing input: {path}. Prepare the virtual-activated profile '
                    'with scripts/prepare_virtual_activation.py or provide explicit '
                    '--plan, --system and --data paths.')

    import json
    plan_path = args.plan.resolve()
    plan = json.loads(plan_path.read_text())
    storage = plan_path.parent.parent / 'storage-before'
    if not storage.exists():
        storage = ROOT / 'build/ios712-validated'
    binary = args.qemu_binary.resolve() if args.qemu_binary else ROOT / 'backend/bin/qemu-system-aarch64'
    required = [storage / 'utility.raw', Path(plan['image']), binary]
    required += [Path(image['path']) for image in plan.get('auxiliary_images', [])]
    for path in required:
        if not path.is_file():
            p.error(f'Missing input: {path}. See docs/firmware.md for the required profile artifacts and backend build.')
    help_text = subprocess.run([str(binary), '-help'], capture_output=True, text=True, check=True).stdout
    if '-vnc ' not in help_text:
        p.error('Backend belum mendukung VNC. Rebuild: .venv/bin/python scripts/qemu/build_backend.py')
    player = build_player()
    work = Path(tempfile.mkdtemp(prefix='vphone-live-', dir='/tmp'))
    images = work / 'images'
    images.mkdir()
    machine = (f"s5l8960x,boot-el=3,boot-entry={plan['initial_entry_pa']:#x},"
               f"boot-args={plan['initial_boot_args_pa']:#x},framebuffer={FB:#x},"
               f"system-image={images}/system.raw,data-image={images}/data.raw,"
               f"utility-image={images}/utility.raw,system-writable=1")
    cmd = [str(binary), '-L', str(ROOT / 'backend/upstream/pc-bios'), '-machine', machine,
           '-accel', 'tcg,thread=' + os.environ.get('VPHONE_TCG_THREAD', 'multi'), '-smp', str(plan.get('cpu_count', 1)), '-m', '1024M',
           '-kernel', plan['image'], '-display', 'none',
           '-vnc', f'unix:{work}/vnc',
           '-serial', f'file:{work}/serial.log',
           '-qmp', f'unix:{work}/qmp,server=on,wait=off'] + args.qemu_arg
    for image in plan.get('auxiliary_images', []):
        cmd += ['-device', f"loader,file={image['path']},addr={image['address']:#x},force-raw=on"]
    env = configure_wifi(cmd, args.wifi)
    print(f'[live] work dir {work} (serial log: {work}/serial.log)')
    print('[live] close the vphone window or press Ctrl-C here to stop')
    try:
        for kind, src in (('system', args.system), ('data', args.data),
                          ('utility', storage / 'utility.raw')):
            clone_or_copy(src.resolve(), images / f'{kind}.raw')
        return run_player(cmd, env, work, player)
    except KeyboardInterrupt:
        return 130
    finally:
        if args.collect_crashlogs:
            data_raw = images / 'data.raw'
            if data_raw.exists():
                crashlogs_dir = work / 'crashlogs'
                try:
                    from crashlogs import collect_crashlogs
                    collect_crashlogs(data_raw, crashlogs_dir)
                    print(f'[live] collected crashlogs in {crashlogs_dir}')
                except Exception as exc:
                    print(f'[live] warning: crashlog collection failed: {exc}', file=sys.stderr)
        if args.keep_images:
            print(f'[live] kept images in {images}')
        else:
            shutil.rmtree(images, ignore_errors=True)
            print(f'[live] removed disk clones; serial log kept at {work}/serial.log')


if __name__ == '__main__':
    sys.exit(main())
