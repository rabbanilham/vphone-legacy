#!/usr/bin/env python3
"""
fw_manifest.py — Firmware catalog and metadata for vphone-legacy.
Only iPhone6,2 n53ap iOS 7.1.2 is a supported preparation target.
Other entries are historical catalog metadata, not launchable targets.
"""

import os
import zipfile
import plistlib
from typing import Dict, Any, Optional

LEGACY_FIRMWARES: Dict[str, Dict[str, Any]] = {
    "7.1.2": {
        "device": "iPhone6,2",
        "alt_device": "iPhone6,1",
        "board": "n53ap",
        "alt_board": "n51ap",
        "chip": "s5l8960x",
        "arch": "arm64",
        "build": "11D257",
        "ipsw_url": "https://secure-appldnld.apple.com/iOS7.1/031-4787.20140627.ggqmo/iPhone6,2_7.1.2_11D257_Restore.ipsw",
        "ipsw_filename": "iPhone6,2_7.1.2_11D257_Restore.ipsw",
        "rootfs_dmg": "058-4574-009.dmg",
        "rootfs_key": "ac8aaea2478f2720445addebf147e06a0e8fe3a55c2197cc01aefa44ff2f7cd754ca7291",
        "kernelcache_path": "kernelcache.release.n53",
        "kernel_iv": "4149ad9235072eadfb8cbe93e5f7d0cc",
        "kernel_key": "fcb4f741f2a09f8f07b251f1680ad04cab58ce29fd8e569912494198201e954d",
        "devicetree_path": "Firmware/all_flash/all_flash.n53ap.production/DeviceTree.n53ap.im4p",
        "devicetree_iv": "e62c188adc1266d45e0ac3f3e8bf92a4",
        "devicetree_key": "df3bea7549e4521d9f59d59aab27343df22b45fafec7ff3c83cce5ad6ee33b49",
        "iboot_path": "Firmware/all_flash/all_flash.n53ap.production/iBoot.n53ap.RELEASE.im4p",
        "iboot_iv": "e118db08c3d89b3a9b1c0d652d6c5d9e",
        "iboot_key": "39cc437454370f3162c244414f912ace984f133d1e94c2eea2bb5c9c9c477b74",
        "n51ap": {
            "device": "iPhone6,1",
            "board": "n51ap",
            "rootfs_dmg": "058-4334-002.dmg",
            "rootfs_key": "129df495240217036a461b6b55476a0d0cb4d21051515286be6ec08819034d7010f3708e",
            "kernelcache_path": "kernelcache.release.n51",
            "devicetree_path": "Firmware/all_flash/all_flash.n51ap.production/DeviceTree.n51ap.img4",
            "iboot_path": "Firmware/all_flash/all_flash.n51ap.production/iBoot.n51ap.RELEASE.img4",
        },
        "features": {
            "filesystem": "hfs+",
            "arm64": True,
            "sep_present": True,
            "ssv": False,
            "apfs": False,
            "cryptex": False
        }
    },
    "8.4.1": {
        "device": "iPhone6,1",
        "board": "n51ap",
        "chip": "s5l8960x",
        "arch": "arm64",
        "build": "12H321",
        "ipsw_url": "https://updates.cdn-apple.com/2015/patches/031-29472-20150812-7A7BC6F2-404B-11E5-8758-BDC5D5514EBF/iPhone6,1_8.4.1_12H321_Restore.ipsw",
        "ipsw_filename": "iPhone6,1_8.4.1_12H321_Restore.ipsw",
        "rootfs_dmg": "058-24367-036.dmg",
        "rootfs_key": "c3e9a5fbdd158d6978434771457df7c6bdfc5453e04a9d7b6f6323c21a481c9a6ec89304",
        "kernelcache_path": "kernelcache.release.n51",
        "devicetree_path": "Firmware/all_flash/all_flash.n51ap.production/DeviceTree.n51ap.img4",
        "iboot_path": "Firmware/all_flash/all_flash.n51ap.production/iBoot.n51ap.RELEASE.img4",
        "features": {
            "filesystem": "hfs+",
            "arm64": True,
            "sep_present": True,
            "ssv": False,
            "apfs": False,
            "cryptex": False
        }
    },
    "9.3.5": {
        "device": "iPhone6,1",
        "board": "n51ap",
        "chip": "s5l8960x",
        "arch": "arm64",
        "build": "13G36",
        "ipsw_url": "https://updates.cdn-apple.com/2016/patches/031-72990-20160825-98D4D68A-69A7-11E6-B9F2-C57849D6809E/iPhone6,1_9.3.5_13G36_Restore.ipsw",
        "ipsw_filename": "iPhone6,1_9.3.5_13G36_Restore.ipsw",
        "rootfs_dmg": "058-49168-079.dmg",
        "rootfs_key": "d748f32cebb89be50ea71a171a4f00d238122d1ca54beab6cf17f0e633d8cba9a6132711",
        "kernelcache_path": "kernelcache.release.n51",
        "devicetree_path": "Firmware/all_flash/all_flash.n51ap.production/DeviceTree.n51ap.img4",
        "iboot_path": "Firmware/all_flash/all_flash.n51ap.production/iBoot.n51ap.RELEASE.img4",
        "features": {
            "filesystem": "hfs+",
            "arm64": True,
            "sep_present": True,
            "ssv": False,
            "apfs": False,
            "cryptex": False
        }
    }
}

def resolve_firmware(version: str, ipsw_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Resolve only the explicitly supported target, never by filename heuristics."""
    if version != "7.1.2":
        return None
    meta = dict(LEGACY_FIRMWARES[version])
    if ipsw_path:
        with zipfile.ZipFile(ipsw_path) as archive:
            manifest = plistlib.loads(archive.read("BuildManifest.plist"))
        identity = select_identity(manifest, meta)
        for field, component in (("kernelcache_path", "KernelCache"),
                                 ("devicetree_path", "DeviceTree"), ("rootfs_dmg", "OS"),
                                 ("iboot_path", "iBoot")):
            path = identity["Manifest"][component]["Info"]["Path"]
            if path != meta[field]:
                raise ValueError(f"Unexpected {component} path for supported firmware: {path}")
        meta["identity"] = identity["Info"]
    return meta


def select_identity(manifest: Dict[str, Any], meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    meta = meta or LEGACY_FIRMWARES["7.1.2"]
    if manifest.get("ProductVersion") != "7.1.2" or manifest.get("ProductBuildVersion") != "11D257":
        raise ValueError("Only iOS 7.1.2 build 11D257 is supported")
    if manifest.get("SupportedProductTypes") != ["iPhone6,2"]:
        raise ValueError("Only iPhone6,2 (n53ap) IPSW is supported")
    matches = [x for x in manifest.get("BuildIdentities", [])
               if x.get("Info", {}).get("DeviceClass", "").lower() == "n53ap"
               and x.get("Info", {}).get("RestoreBehavior") == "Erase"]
    if len(matches) != 1:
        raise ValueError("Expected exactly one n53ap erase BuildIdentity")
    identity = matches[0]
    if identity["Info"].get("BuildNumber") != "11D257":
        raise ValueError("BuildIdentity build mismatch")
    for component in ("KernelCache", "DeviceTree", "OS", "iBoot"):
        path = identity.get("Manifest", {}).get(component, {}).get("Info", {}).get("Path")
        if not isinstance(path, str) or path.startswith("/") or ".." in path.split("/"):
            raise ValueError(f"Missing or unsafe {component} path")
    return identity


def get_firmware_info(version: str) -> Optional[Dict[str, Any]]:
    meta = LEGACY_FIRMWARES.get(version)
    return dict(meta, supported=version == "7.1.2") if meta else None


def list_supported_versions() -> list[str]:
    return ["7.1.2"]


if __name__ == "__main__":
    for version in LEGACY_FIRMWARES:
        info = get_firmware_info(version)
        print(f"iOS {version}: {'supported preparation target' if info['supported'] else 'unsupported'}")
