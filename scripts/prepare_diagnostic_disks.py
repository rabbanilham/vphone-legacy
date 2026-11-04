#!/usr/bin/env python3
"""Prepare diagnostic disk copies from an explicit bundle; never mutate baseline disks."""
import hashlib
import os
from pathlib import Path
import plistlib
import shutil
import sys

from qemu.hfs_mount import attach, detach, mounted

def clone(source, target):
    """Use APFS clonefile on Darwin, with a portable copy fallback."""
    if Path(target).exists():
        raise ValueError(f"Refusing to replace {target}")
    if sys.platform == "darwin":
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.clonefile(os.fsencode(source), os.fsencode(target), 0) == 0:
            return
        if ctypes.get_errno() not in (18, 45, 95):
            raise OSError(ctypes.get_errno(), "clonefile failed")
    shutil.copyfile(source, target)


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


def prepare_system(bundle, out, sep_fw):
    bundle, out = Path(bundle), Path(out)
    target = out / "system-diagnostic-sep.raw"
    if target.exists():
        target.unlink()
    print(f"[*] Creating {target} from {bundle / 'system.raw'}...")
    clone(bundle / "system.raw", target)
    with mounted(target) as (dev, mount):
        # Free space by removing unused update ramdisk
        ramdisk = mount / "usr/standalone/update/ramdisk/H6SURamDisk.dmg"
        if ramdisk.exists():
            print(f"[*] Removing {ramdisk} to free 22 MB...")
            ramdisk.unlink()
        fw_dest = mount / "usr/standalone/firmware/sep-firmware.img4"
        fw_dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"[*] Copying SEP firmware to {fw_dest}...")
        shutil.copyfile(sep_fw, fw_dest)
        fw_dest.chmod(0o644)
    sha = digest(target)
    print(f"[+] Successfully prepared {target} (SHA256: {sha})")
    return target, sha


def prepare_data(bundle, out):
    bundle, out = Path(bundle), Path(out)
    target = out / "data-diagnostic-systembag.raw"
    if target.exists():
        target.unlink()
    print(f"[*] Creating {target} from {bundle / 'data.raw'}...")
    clone(bundle / "data.raw", target)
    
    # Mount system.raw read-only to copy the /private/var template
    with mounted(bundle / "system.raw", readonly=True) as (sys_dev, sys_mount):
        with mounted(target) as (data_dev, data_mount):
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
    sha = digest(target)
    print(f"[+] Successfully prepared {target} (SHA256: {sha})")
    return target, sha


def seed_setup_done(data_mount, build_version="11D257"):
    """Seed Setup Assistant UI completion preferences for iOS 7.1.2.

    Verified in 11D257 (agy A1 report, strings checked in the cache/lockdownd):
    - Preferences.framework PSSetupAssistantNeedsToRun (0x189522298) returns
      NO for ForceNoBuddy, and also checks SetupDone / SetupVersion (>= 5) /
      AssistantPresented. SpringBoard _SBUpdateBuddyMode (0x1000ded5c)
      independently checks SBLockdownManager.brickedDevice; completion
      preferences alone do not guarantee that buddy mode is disabled.
    Activation state is deliberately left to the guest; this only records
    that the Setup Assistant UI has been completed. A guest reporting
    BrickState=true can still present Setup.app (see
    research/reports/setup-done-validation-20261023.md).
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


def prepare_setup_done_variant(out):
    out = Path(out)
    """Copy the prepared data disk and add the setup-done state to the copy."""
    source = out / "data-diagnostic-systembag.raw"
    target = out / "data-diagnostic-setupdone.raw"
    if target.exists():
        target.unlink()
    clone(source, target)
    with mounted(target) as (dev, mount):
        seed_setup_done(mount)
        print("[*] Seeded purplebuddy setup-done preferences")
    sha = digest(target)
    print(f"[+] Prepared {target} (SHA256: {sha})")
    return target, sha


def main():
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("bundle", type=Path)
    p.add_argument("--out", type=Path)
    p.add_argument("--sep", type=Path)
    p.add_argument("--ipsw", type=Path)
    a = p.parse_args()
    out = a.out or a.bundle / "ui/diagnostic"
    out.mkdir(parents=True, exist_ok=True)
    if a.sep is None:
        from ui_profile import extract_sep
        a.sep = extract_sep(a.bundle, out, a.ipsw)
    prepare_system(a.bundle, out, a.sep)
    prepare_data(a.bundle, out)


if __name__ == "__main__":
    main()
