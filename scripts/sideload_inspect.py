#!/usr/bin/env python3
"""Read-only inspection tool for guest iOS 7.1.2 installation state and cache.

Safely attaches disk images with 'hdiutil attach -readonly' and inspects:
  - System disk: bundles in /Applications/ (Info.plist metadata)
  - Data disk: /mobile/Library/Caches/com.apple.mobile.installation.plist,
               LaunchServices csstore in /db/lsd,
               SpringBoard IconState in /mobile/Library/SpringBoard.
"""
from __future__ import annotations

import argparse
import contextlib
import json
from pathlib import Path
import plistlib
import subprocess
import sys
from typing import Generator, Any


@contextlib.contextmanager
def readonly_mount(image: Path | str) -> Generator[Path, None, None]:
    """Attach a raw disk image read-only and yield mount point Path."""
    image_path = Path(image).resolve()
    if not image_path.exists():
        raise FileNotFoundError(f"Disk image not found: {image_path}")

    cmd = [
        "hdiutil", "attach",
        "-plist",
        "-nobrowse",
        "-readonly",
        "-imagekey", "diskimage-class=CRawDiskImage",
        str(image_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    try:
        entities = plistlib.loads(res.stdout.encode("utf-8"))["system-entities"]
    except Exception as e:
        raise RuntimeError(f"Failed to parse hdiutil attach plist output: {e}\n{res.stdout}")

    dev_node = None
    mount_point = None
    for ent in entities:
        if "mount-point" in ent:
            mount_point = Path(ent["mount-point"])
            dev_node = ent.get("dev-entry")
            break

    if not mount_point or not dev_node:
        for ent in entities:
            if "dev-entry" in ent:
                subprocess.run(["hdiutil", "detach", ent["dev-entry"]], capture_output=True)
        raise RuntimeError(f"No mounted filesystem found for {image_path}")

    try:
        yield mount_point
    finally:
        subprocess.run(["hdiutil", "detach", dev_node], capture_output=True)


def inspect_system_volume(mount_point: Path) -> dict[str, Any]:
    """Inspect system volume Applications."""
    apps_dir = mount_point / "Applications"
    results: dict[str, Any] = {"volume_type": "system", "applications": {}}
    if not apps_dir.is_dir():
        return results

    for app in sorted(apps_dir.glob("*.app")):
        plist_path = app / "Info.plist"
        if not plist_path.is_file():
            continue
        try:
            with plist_path.open("rb") as f:
                info = plistlib.load(f)
            bundle_id = info.get("CFBundleIdentifier", app.stem)
            results["applications"][bundle_id] = {
                "name": app.name,
                "executable": info.get("CFBundleExecutable"),
                "version": str(info.get("CFBundleVersion", "")),
                "short_version": str(info.get("CFBundleShortVersionString", "")),
                "min_os": str(info.get("MinimumOSVersion", "")),
                "device_family": info.get("UIDeviceFamily"),
            }
        except Exception as e:
            results["applications"][app.name] = {"error": str(e)}

    return results


def inspect_data_volume(mount_point: Path) -> dict[str, Any]:
    """Inspect data volume caches and registered applications."""
    results: dict[str, Any] = {
        "volume_type": "data",
        "cache_present": False,
        "metadata": None,
        "system_apps_cached": 0,
        "user_apps_cached": 0,
        "user_apps": {},
        "lsd_csstore": [],
        "icon_state_present": False,
    }

    # Search for installation cache at /mobile/Library/Caches or /private/var/mobile/Library/Caches
    cache_candidates = [
        mount_point / "mobile/Library/Caches/com.apple.mobile.installation.plist",
        mount_point / "private/var/mobile/Library/Caches/com.apple.mobile.installation.plist",
    ]
    cache_file = next((p for p in cache_candidates if p.is_file()), None)

    if cache_file:
        results["cache_present"] = True
        results["cache_path"] = str(cache_file.relative_to(mount_point))
        try:
            with cache_file.open("rb") as f:
                cache_data = plistlib.load(f)
            results["metadata"] = cache_data.get("Metadata")
            sys_apps = cache_data.get("System", {})
            user_apps = cache_data.get("User", {})
            results["system_apps_cached"] = len(sys_apps)
            results["user_apps_cached"] = len(user_apps)
            for bid, details in user_apps.items():
                results["user_apps"][bid] = {
                    "path": details.get("Path"),
                    "container": details.get("Container"),
                    "executable": details.get("CFBundleExecutable"),
                    "version": details.get("CFBundleVersion"),
                }
        except Exception as e:
            results["cache_read_error"] = str(e)

    # Check LaunchServices csstore
    lsd_dirs = [mount_point / "db/lsd", mount_point / "private/var/db/lsd"]
    for lsd_dir in lsd_dirs:
        if lsd_dir.is_dir():
            results["lsd_csstore"].extend([f.name for f in lsd_dir.glob("*.csstore")])

    # Check IconState.plist
    icon_candidates = [
        mount_point / "mobile/Library/SpringBoard/IconState.plist",
        mount_point / "private/var/mobile/Library/SpringBoard/IconState.plist",
    ]
    if any(p.is_file() for p in icon_candidates):
        results["icon_state_present"] = True

    # Check User Applications folder
    user_apps_dir = mount_point / "mobile/Applications"
    if user_apps_dir.is_dir():
        results["user_application_containers"] = [p.name for p in user_apps_dir.iterdir() if p.is_dir()]

    return results


def inspect_image(image_path: Path) -> dict[str, Any]:
    """Mount and detect image type, then return inspection details."""
    with readonly_mount(image_path) as mount:
        if (mount / "Applications").is_dir() and (mount / "usr").is_dir():
            return inspect_system_volume(mount)
        return inspect_data_volume(mount)


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect iOS 7.1.2 installation state from disk image (read-only)")
    parser.add_argument("image", type=Path, help="Path to system.raw or data.raw")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    args = parser.parse_args()

    if not args.image.exists():
        print(f"Error: image {args.image} does not exist", file=sys.stderr)
        return 1

    try:
        report = inspect_image(args.image)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            vtype = report.get("volume_type")
            print(f"Volume Type: {vtype}")
            if vtype == "system":
                apps = report.get("applications", {})
                print(f"Found {len(apps)} applications in /Applications:")
                for bid, info in sorted(apps.items())[:15]:
                    print(f"  - {bid} ({info.get('executable', '')}) [v{info.get('version', '')}]")
                if len(apps) > 15:
                    print(f"  ... and {len(apps) - 15} more")
            elif vtype == "data":
                print(f"Installation Cache Present: {report['cache_present']}")
                if report["cache_present"]:
                    print(f"  Metadata: {report.get('metadata')}")
                    print(f"  Cached System Apps: {report['system_apps_cached']}")
                    print(f"  Cached User Apps: {report['user_apps_cached']}")
                print(f"LaunchServices csstore files: {report.get('lsd_csstore', [])}")
                print(f"SpringBoard IconState Present: {report['icon_state_present']}")
                print(f"User App Containers: {report.get('user_application_containers', [])}")
        return 0
    except subprocess.CalledProcessError as e:
        print(f"hdiutil failed: {e.stderr if e.stderr else e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Inspection error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
