#!/usr/bin/env python3
"""Offline app install into a guest image pair (iOS 7.1.2 system app route).

The app is copied to /Applications on the system volume and owned by root:admin
(the registration route documented in research/reports/sideload-registration.md);
the installd cache and LaunchServices store on the data volume are removed so
installd rebuilds them from /Applications on the next boot. The guest must run
with relaxed AMFI boot arguments for ad-hoc or unsigned binaries to execute.
Never run this against an image a VM is using.
"""
import argparse
import json
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'scripts/qemu'))

from ipa import IPAError, extract_ipa, inspect_ipa  # noqa: E402
from hfs_mount import mounted  # noqa: E402
from hfs_ownership import collect_catalog_owners, set_catalog_owner_many  # noqa: E402
from copy_tree_clean import copy_tree_clean  # noqa: E402

ADMIN_GID = 80
AMFI_ARGS = ('amfi_allow_any_signature=1', 'amfi_get_out_of_my_way=1', 'cs_enforcement_disable=1')
CACHE_PATHS = ('mobile/Library/Caches/com.apple.mobile.installation.plist',)
CACHE_GLOBS = ('db/lsd/com.apple.LaunchServices-*.csstore',)


class InstallError(RuntimeError):
    pass


def images_in_use(*images):
    """True if a QEMU process has any of these image paths on its command line."""
    out = subprocess.run(['ps', '-axo', 'command='], capture_output=True, text=True).stdout
    names = {str(Path(i).resolve()) for i in images}
    return any('qemu-system' in line and any(n in line for n in names) for line in out.splitlines())


def adhoc_sign(binary, entitlements=None):
    """Ad-hoc sign with ldid when available; return True if signed."""
    ldid = shutil.which('ldid')
    if not ldid:
        return False
    args = [ldid, f'-S{entitlements}' if entitlements else '-S', str(binary)]
    subprocess.run(args, check=True, capture_output=True)
    return True


def boot_args_ok(boot_args):
    return all(a in (boot_args or '').split() for a in AMFI_ARGS)


def install_app(system_image, data_image, ipa, *, sign=True, uid=0, gid=ADMIN_GID):
    system_image, data_image = Path(system_image), Path(data_image)
    for image in (system_image, data_image):
        if not image.is_file():
            raise InstallError(f'Missing image: {image}')
    if images_in_use(system_image, data_image):
        raise InstallError('A QEMU process is using these images; stop the VM first')
    try:
        info = inspect_ipa(ipa)
    except IPAError as exc:
        raise InstallError(f'Invalid IPA: {exc}') from exc
    slice_info = info['selected_slice']
    if slice_info['architecture'] not in ('arm64', 'armv7', 'armv7s'):
        raise InstallError(f"Unsupported architecture {slice_info['architecture']}")
    report = {'bundle_id': info['bundle_id'], 'executable': info['executable'],
              'architecture': slice_info['architecture'], 'signed': False,
              'removed_cache': [], 'owners': 0}
    with tempfile.TemporaryDirectory(prefix='vphone-install-') as tmp:
        contents = extract_ipa(ipa, Path(tmp) / 'contents')
        app_src = contents / info['app_path']
        binary = app_src / info['executable']
        binary.chmod(0o755)
        if sign:
            report['signed'] = adhoc_sign(binary)
        with mounted(system_image) as volume:
            apps = Path(volume.mount_point) / 'Applications'
            if not apps.is_dir():
                raise InstallError('System volume has no /Applications')
            dest = apps / app_src.name
            if dest.exists():
                shutil.rmtree(dest)
            copy_tree_clean(app_src, dest)
            for directory, dirs, files in os.walk(dest):
                for name in dirs:
                    Path(directory, name).chmod(0o755)
                for name in files:
                    Path(directory, name).chmod(0o644)
            binary_dest = dest / info['executable']
            binary_dest.chmod(0o755)
            dest.chmod(0o755)
            owners = collect_catalog_owners(dest, uid, gid)
            report['app_path'] = f'/Applications/{app_src.name}'
        # Ownership is rewritten on the cleanly unmounted raw image.
        set_catalog_owner_many(system_image, owners)
        report['owners'] = len(owners)
    with mounted(data_image) as volume:
        mount = Path(volume.mount_point)
        for rel in CACHE_PATHS:
            target = mount / rel
            if target.exists():
                target.unlink()
                report['removed_cache'].append(rel)
        for pattern in CACHE_GLOBS:
            for target in mount.glob(pattern):
                target.unlink()
                report['removed_cache'].append(str(target.relative_to(mount)))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('ipa', type=Path)
    parser.add_argument('--system', required=True, type=Path)
    parser.add_argument('--data', required=True, type=Path)
    parser.add_argument('--no-sign', action='store_true')
    args = parser.parse_args(argv)
    try:
        report = install_app(args.system, args.data, args.ipa, sign=not args.no_sign)
    except InstallError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
