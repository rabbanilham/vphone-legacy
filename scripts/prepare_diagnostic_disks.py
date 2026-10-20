#!/usr/bin/env python3
"""Prepare persistent diagnostic disks for display and AGX probes.

1. build/diagnostic/system-diagnostic-sep.raw:
   Clone of build/ios712-validated/system.raw with H6SURamDisk.dmg removed to free space,
   and sep-firmware.n53.RELEASE.im4p installed as /usr/standalone/firmware/sep-firmware.img4.

2. build/diagnostic/data-diagnostic-systembag.raw:
   Clone of build/ios712-validated/data.raw seeded with the /private/var
   template from system.raw, plus /keybags/systembag.kb for unencrypted keybag loading.
"""
import hashlib
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
VALIDATED = ROOT / "build/ios712-validated"
DIAG_DIR = ROOT / "build/diagnostic"
SEP_FW = ROOT / "research/firmware-artifacts/sep-firmware.n53.RELEASE.im4p"


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def attach(image_path, readonly=False):
    cmd = ["hdiutil", "attach", "-imagekey", "diskimage-class=CRawDiskImage", "-nobrowse"]
    if readonly:
        cmd.append("-readonly")
    cmd.append(str(image_path))
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    lines = res.stdout.strip().splitlines()
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[-1].startswith("/Volumes/"):
            return parts[0], Path(parts[-1])
    raise RuntimeError(f"Could not find mount point from: {res.stdout}")


def detach(dev):
    subprocess.run(["hdiutil", "detach", str(dev)], check=True, capture_output=True)


def prepare_system():
    target = DIAG_DIR / "system-diagnostic-sep.raw"
    if target.exists():
        target.unlink()
    print(f"[*] Creating {target} from {VALIDATED / 'system.raw'}...")
    shutil.copyfile(VALIDATED / "system.raw", target)
    dev, mount = attach(target)
    try:
        # Free space by removing unused update ramdisk
        ramdisk = mount / "usr/standalone/update/ramdisk/H6SURamDisk.dmg"
        if ramdisk.exists():
            print(f"[*] Removing {ramdisk} to free 22 MB...")
            ramdisk.unlink()
        fw_dest = mount / "usr/standalone/firmware/sep-firmware.img4"
        fw_dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"[*] Copying SEP firmware to {fw_dest}...")
        shutil.copyfile(SEP_FW, fw_dest)
        fw_dest.chmod(0o644)
    finally:
        detach(dev)
    sha = digest(target)
    print(f"[+] Successfully prepared {target} (SHA256: {sha})")
    return target, sha


def prepare_data():
    target = DIAG_DIR / "data-diagnostic-systembag.raw"
    if target.exists():
        target.unlink()
    print(f"[*] Creating {target} from {VALIDATED / 'data.raw'}...")
    shutil.copyfile(VALIDATED / "data.raw", target)
    
    # Mount system.raw read-only to copy the /private/var template
    sys_dev, sys_mount = attach(VALIDATED / "system.raw", readonly=True)
    try:
        data_dev, data_mount = attach(target)
        try:
            var_src = sys_mount / "private/var"
            print(f"[*] Seeding /private/var template to {data_mount}...")
            for item in var_src.iterdir():
                dst = data_mount / item.name
                if item.is_dir():
                    shutil.copytree(item, dst, symlinks=True, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dst, follow_symlinks=False)
            
            # Create /keybags/systembag.kb
            keybags_dir = data_mount / "keybags"
            keybags_dir.mkdir(exist_ok=True)
            systembag_path = keybags_dir / "systembag.kb"
            # Unencrypted mode: plist containing KeyBagKeys blob
            payload = plistlib.dumps({"KeyBagKeys": b"\x00" * 32}, fmt=plistlib.FMT_BINARY)
            systembag_path.write_bytes(payload)
            systembag_path.chmod(0o644)
            print(f"[*] Wrote systembag.kb to {systembag_path}")
        finally:
            detach(data_dev)
    finally:
        detach(sys_dev)
    sha = digest(target)
    print(f"[+] Successfully prepared {target} (SHA256: {sha})")
    return target, sha


def main():
    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    sys_img, sys_sha = prepare_system()
    data_img, data_sha = prepare_data()
    print("[*] Diagnostic images ready:")
    print("    System:", sys_img, sys_sha)
    print("    Data:  ", data_img, data_sha)


if __name__ == "__main__":
    main()
