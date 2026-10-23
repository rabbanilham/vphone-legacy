#!/usr/bin/env python3
"""Run the guest interactively in a Cocoa window (no debugger, no hooks).

Uses the same boot plan and disposable disk clones as display_probe.py, so
the original/diagnostic images are never written. Clones are removed on exit
unless --keep-images is given. Touch is only injectable via
VPHONE_TOUCH_SCRIPT (see backend/README.md); mouse input is not wired yet.
"""
import argparse
from host_wifi import configure_wifi
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from display_probe import FB  # noqa: E402

EVIDENCE = ROOT / 'backend/evidence/keybag-contract-20261015'


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


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--plan', type=Path, default=EVIDENCE / 'boot-plan-vram/boot_plan.json')
    p.add_argument('--system', type=Path, default=ROOT / 'build/diagnostic/system-diagnostic-sep.raw')
    p.add_argument('--data', type=Path, default=ROOT / 'build/diagnostic/data-diagnostic-systembag.raw')
    p.add_argument('--qemu-binary', type=Path, help='Explicit QEMU binary for a reproducible diagnostic run')
    p.add_argument('--keep-images', action='store_true')
    p.add_argument('--collect-crashlogs', action='store_true',
                   help='Collect guest crash reports from data clone before cleanup')
    p.add_argument('--wifi', action='store_true', help='Expose an open Wi-Fi AP backed by user-mode NAT')
    p.add_argument('--qemu-arg', action='append', default=[])
    args = p.parse_args()

    import json
    plan_path = args.plan.resolve()
    plan = json.loads(plan_path.read_text())
    storage = plan_path.parent.parent / 'storage-before'
    if not storage.exists():
        storage = ROOT / 'build/ios712-validated'
    work = Path(tempfile.mkdtemp(prefix='vphone-live-', dir='/tmp'))
    images = work / 'images'
    images.mkdir()
    for kind, src in (('system', args.system), ('data', args.data),
                      ('utility', storage / 'utility.raw')):
        clone_or_copy(src.resolve(), images / f'{kind}.raw')

    machine = (f"s5l8960x,boot-el=3,boot-entry={plan['initial_entry_pa']:#x},"
               f"boot-args={plan['initial_boot_args_pa']:#x},framebuffer={FB:#x},"
               f"system-image={images}/system.raw,data-image={images}/data.raw,"
               f"utility-image={images}/utility.raw,system-writable=1")
    binary = args.qemu_binary.resolve() if args.qemu_binary else ROOT / 'backend/bin/qemu-system-aarch64'
    cmd = [str(binary), '-machine', machine,
           '-accel', 'tcg,thread=' + os.environ.get('VPHONE_TCG_THREAD', 'single'), '-smp', str(plan.get('cpu_count', 1)), '-m', '1024M',
           '-kernel', plan['image'], '-display', 'cocoa',
           '-serial', f'file:{work}/serial.log',
           '-qmp', f'unix:{work}/qmp,server=on,wait=off'] + args.qemu_arg
    for image in plan.get('auxiliary_images', []):
        cmd += ['-device', f"loader,file={image['path']},addr={image['address']:#x},force-raw=on"]
    env = configure_wifi(cmd, args.wifi)
    print(f'[live] work dir {work} (serial log: {work}/serial.log)')
    print('[live] close the QEMU window or press Ctrl-C here to stop')
    try:
        return subprocess.call(cmd, env=env)
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
