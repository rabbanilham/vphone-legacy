#!/usr/bin/env python3
"""Prepare persistent diagnostic disks for display and AGX probes.

1. build/diagnostic/system-diagnostic-sep.raw:
   Clone of build/ios712-validated/system.raw with H6SURamDisk.dmg removed to free space,
   and sep-firmware.n53.RELEASE.im4p installed as /usr/standalone/firmware/sep-firmware.img4.

2. build/diagnostic/data-diagnostic-systembag.raw:
   Clone of build/ios712-validated/data.raw seeded with the /private/var
   template from system.raw, plus /keybags/systembag.kb for unencrypted keybag loading
   and com.apple.migration.plist so DataMigration does not rerun on every boot.
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


def seed_migration_preferences(data_mount, build_version="11D257"):
    """Seed migration preferences to prevent both backboardd and accountsd from triggering DataMigration.

    1. com.apple.migration.plist:
       DMPerformMigrationIfNeeded (DataMigration.framework) compares LastSystemVersion in
       com.apple.migration with ProductBuildVersion. If absent or mismatched, backboardd
       calls DMPerformMigration, blocking system app check-in. On successful skip, it also
       records PreMigrationSystemVersion.

    2. com.apple.accountsd.plist:
       -[ACDServer init] (AccountsDaemon.framework) calls +[ACAccountMigrationLock migrationFinished],
       which reads LastMigrationSystemVersion from com.apple.accountsd. If absent or mismatched with
       _currentSystemVersion, accountsd calls _DMPerformMigration("accountsd needs migration to finish"),
       launching com.apple.datamigrator (pid 84) at ~16.8 s. The migrator creates a PUIProgressWindow
       CA context at level 2999.0 that never hides because plugin MobileSlideShow hangs waiting on
       assetsd (com.apple.PersistentURLTranslator.Gatekeeper), swallowing all touch events.

    Seeding both preference files for 'mobile' and 'root' ensures that:
    - backboardd skips migration (dm_skip).
    - accountsd logs 'Account migration has already occurred. No need to block non-migration connections.'
      and bypasses DMPerformMigration entirely.
    """
    migration_payload = plistlib.dumps(
        {
            "LastSystemVersion": build_version,
            "PreMigrationSystemVersion": build_version,
        },
        fmt=plistlib.FMT_BINARY,
    )
    accountsd_payload = plistlib.dumps(
        {"LastMigrationSystemVersion": build_version},
        fmt=plistlib.FMT_BINARY,
    )

    for home in ("mobile", "root"):
        prefs = Path(data_mount) / home / "Library/Preferences"
        prefs.mkdir(parents=True, exist_ok=True)
        (prefs / "com.apple.migration.plist").write_bytes(migration_payload)
        (prefs / "com.apple.accountsd.plist").write_bytes(accountsd_payload)


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

            # Seed migration preferences for backboardd and accountsd
            seed_migration_preferences(data_mount, "11D257")
            print("[*] Wrote com.apple.migration.plist and com.apple.accountsd.plist (11D257)")
        finally:
            detach(data_dev)
    finally:
        detach(sys_dev)
    sha = digest(target)
    print(f"[+] Successfully prepared {target} (SHA256: {sha})")
    return target, sha


def seed_setup_done(data_mount, build_version="11D257"):
    """Seed the state a device has after finishing Setup Assistant offline.

    Verified in 11D257 (agy A1 report, strings checked in the cache/lockdownd):
    - Preferences.framework PSSetupAssistantNeedsToRun (0x189522298) returns
      NO for ForceNoBuddy, and also checks SetupDone / SetupVersion (>= 5) /
      AssistantPresented; SpringBoard _SBUpdateBuddyMode (0x1000ded5c) then
      leaves buddy mode, so the lock screen unlocks to the home screen.
    - lockdownd keeps "-ActivationState" = FactoryActivated without an
      activation record ("No Ticket, but allowing factory activation"), and
      SBLockdownManager reads "-BrickState" (must be false) from
      /var/root/Library/Lockdown/data_ark.plist. Activation records are
      signature-checked, so none are fabricated.
    """
    bplist = lambda value: plistlib.dumps(value, fmt=plistlib.FMT_BINARY)
    purplebuddy = bplist({
        "SetupDone": True,
        "SetupFinishedAllSteps": True,
        "SetupVersion": 5,
        "AssistantPresented": True,
        "SetupState": "SetupUsingAssistant",
        "ForceNoBuddy": True,
    })
    mobile = Path(data_mount) / "mobile/Library/Preferences"
    mobile.mkdir(parents=True, exist_ok=True)
    (mobile / "com.apple.purplebuddy.plist").write_bytes(purplebuddy)
    (mobile / "com.apple.purplebuddy.notbackedup.plist").write_bytes(bplist({
        "CloudConfigPresented": True,
        "LocationServices2Presented": True,
        "ShouldRunKeychainSyncMiniBuddy": False,
        "ShouldRunMesaMiniBuddy": False,
    }))
    root = Path(data_mount) / "root/Library/Preferences"
    root.mkdir(parents=True, exist_ok=True)
    (root / "com.apple.purplebuddy.plist").write_bytes(purplebuddy)
    lockdown = Path(data_mount) / "root/Library/Lockdown"
    lockdown.mkdir(parents=True, exist_ok=True)
    (lockdown / "data_ark.plist").write_bytes(bplist({
        "-ActivationState": "FactoryActivated",
        "-BrickState": False,
        "com.apple.purplebuddy-SetupState": "SetupUsingAssistant",
    }))


def prepare_setup_done_variant():
    """Copy the prepared data disk and add the setup-done state to the copy."""
    source = DIAG_DIR / "data-diagnostic-systembag.raw"
    target = DIAG_DIR / "data-diagnostic-setupdone.raw"
    if target.exists():
        target.unlink()
    shutil.copyfile(source, target)
    dev, mount = attach(target)
    try:
        seed_setup_done(mount)
        print("[*] Seeded purplebuddy and lockdown setup-done state")
    finally:
        detach(dev)
    sha = digest(target)
    print(f"[+] Prepared {target} (SHA256: {sha})")
    return target, sha


def main():
    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    sys_img, sys_sha = prepare_system()
    data_img, data_sha = prepare_data()
    print("[*] Diagnostic images ready:")
    print("    System:", sys_img, sys_sha)
    print("    Data:  ", data_img, data_sha)


if __name__ == "__main__":
    if sys.argv[1:] == ["--setup-done-variant"]:
        prepare_setup_done_variant()
    else:
        main()
