#!/usr/bin/env python3
"""Prepare verified firmware artifacts; preparation does not certify bootability."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import zipfile

from fw_manifest import resolve_firmware, select_identity, list_supported_versions
from patchers.patch_kernel import decode_kernel_components
from patchers.extract_ans import decode_ans_components
from patchers.patch_devicetree import decode_devicetree
from patchers.patch_rootfs import vfdecrypt_dmg, convert_udif_to_raw, create_data_volume
from macho import inspect_macho

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def ensure_vfdecrypt(project_root):
    project = Path(project_root)
    binary = project / "dist/vfdecrypt"
    if binary.is_file() and os.access(binary, os.X_OK):
        return str(binary)
    source = project / "scripts/patchers/vfdecrypt.c"
    if not source.is_file():
        raise RuntimeError("vfdecrypt source is missing")
    binary.parent.mkdir(parents=True, exist_ok=True)
    ssl_prefix = subprocess.check_output(["brew", "--prefix", "openssl@3"], text=True).strip()
    subprocess.run(["clang", "-I" + ssl_prefix + "/include", "-L" + ssl_prefix + "/lib",
                    "-Wno-deprecated-declarations", "-O2", str(source), "-lcrypto", "-o", str(binary)],
                   check=True, capture_output=True)
    return str(binary)


def sha256_file(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(data)
    return result.hexdigest()


def artifact(path, bundle, mutable=False):
    result = {"path": str(Path(path).relative_to(bundle)), "sha256": sha256_file(path),
              "size": Path(path).stat().st_size}
    if mutable:
        result["mutable"] = True
    return result


def extract_component(archive, member, output):
    if archive.namelist().count(member) != 1:
        raise ValueError(f"Missing or duplicated required IPSW member: {member}")
    with archive.open(member) as source, Path(output).open("xb") as destination:
        shutil.copyfileobj(source, destination, 1024 * 1024)
    if not Path(output).stat().st_size:
        raise ValueError(f"Empty firmware component: {member}")


def monitor_boot_contract(tree_root):
    defaults = tree_root.find_node("defaults")
    if defaults is None:
        raise ValueError("Missing monitor partition defaults")
    monitor_size, sep_size = defaults.get_prop("monitor-size"), defaults.get_prop("sep-size")
    if not monitor_size or len(monitor_size) != 4 or not sep_size or len(sep_size) != 4:
        raise ValueError("Invalid monitor or SEP partition size")
    reserved, sep_reserved = int.from_bytes(monitor_size, "little"), int.from_bytes(sep_size, "little")
    if reserved != 0x200000 or sep_reserved != 0x600000:
        raise ValueError("Unexpected native monitor partition geometry for verified n53ap target")
    return {"version": 1, "reserved_size": reserved, "native_physical_base": 0x800000000 + sep_reserved,
            "virtual_base": 0x4100000000,
            "source": "iBoot-1940.10.58 decoded loader and original DeviceTree defaults"}


def prepare_firmware(version, ipsw_path, out_dir, boot_args=None):
    output = Path(out_dir).resolve()
    if output.exists():
        raise ValueError("Output already exists; prepare a fresh bundle rather than overwriting persistent data")
    if not ipsw_path or not Path(ipsw_path).is_file():
        raise ValueError("A real local IPSW is required; missing firmware cannot create a VM")
    meta = resolve_firmware(version, str(ipsw_path))
    if not meta:
        raise ValueError("Only iPhone6,2 n53ap iOS 7.1.2 build 11D257 is supported")
    args = boot_args or "-v serial=3"
    if "\0" in args or len(args.encode()) >= 256:
        raise ValueError("Boot arguments must fit the 256-byte Apple command line")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=output.name + ".preparing-", dir=output.parent))
    try:
        original = temporary / "original"
        original.mkdir()
        with zipfile.ZipFile(ipsw_path) as archive:
            manifest_data = archive.read("BuildManifest.plist")
            identity = select_identity(plistlib.loads(manifest_data))
            (original / "BuildManifest.plist").write_bytes(manifest_data)
            for field, filename in (("kernelcache_path", "kernelcache.im4p"),
                                    ("devicetree_path", "DeviceTree.im4p"),
                                    ("iboot_path", "iBoot.im4p"),
                                    ("rootfs_dmg", "RootFS.encrypted.dmg")):
                destination = original / filename
                extract_component(archive, meta[field], destination)
                component = {"kernelcache_path": "KernelCache", "devicetree_path": "DeviceTree",
                             "rootfs_dmg": "OS", "iboot_path": "iBoot"}[field]
                digest = identity["Manifest"][component].get("Digest")
                if digest is not None:
                    if not isinstance(digest, bytes) or len(digest) != 20:
                        raise ValueError(f"Unsupported {component} BuildManifest digest")
                    if hashlib.sha1(destination.read_bytes()).digest() != digest:
                        raise ValueError(f"{component} digest differs from BuildManifest")
        kernel, monitor = decode_kernel_components((original / "kernelcache.im4p").read_bytes(), meta["kernel_iv"], meta["kernel_key"])
        kernel_info = inspect_macho(kernel)
        monitor_info = inspect_macho(monitor)
        iboot, ans, ans_info = decode_ans_components((original / "iBoot.im4p").read_bytes(),
                                                    meta["iboot_iv"], meta["iboot_key"])
        (original / "iBoot.bin").write_bytes(iboot)
        (original / "ans-firmware.bin").write_bytes(ans)
        shutil.copyfile(original / "ans-firmware.bin", temporary / "ans-firmware.bin")
        (original / "monitor.macho").write_bytes(monitor)
        shutil.copyfile(original / "monitor.macho", temporary / "monitor.macho")
        (original / "kernel.macho").write_bytes(kernel)
        shutil.copyfile(original / "kernel.macho", temporary / "kernel.macho")
        tree, tree_root = decode_devicetree((original / "DeviceTree.im4p").read_bytes(),
                                          meta["devicetree_iv"], meta["devicetree_key"])
        monitor_contract = monitor_boot_contract(tree_root)
        (original / "DeviceTree.raw").write_bytes(tree)
        shutil.copyfile(original / "DeviceTree.raw", temporary / "DeviceTree.raw")
        helper = ensure_vfdecrypt(PROJECT_ROOT)
        decrypted = original / "RootFS.dmg"
        vfdecrypt_dmg(str(original / "RootFS.encrypted.dmg"), str(decrypted), meta["rootfs_key"], helper)
        system = temporary / "system.raw"
        system_info = convert_udif_to_raw(decrypted, system)
        if system_info["filesystem"] != "hfsx":
            raise ValueError("This target expects a case-sensitive HFSX system partition")
        data_disk = temporary / "data.raw"
        data_info = create_data_volume(data_disk)
        report = {"patches": [], "verified": True,
                  "reason": "No runtime-verified kernel, DeviceTree or RootFS compatibility patches are available"}
        (temporary / "patch_report.json").write_text(json.dumps(report, indent=2) + "\n")
        manifest = {
            "schema_version": 2, "version": version, "build": meta["build"], "device": meta["device"],
            "board": meta["board"], "chip": meta["chip"], "cpu": "cyclone", "memory_mb": 1024,
            "boot_args": args, "backend": {"id": "s5l8960x", "status": "experimental"},
            "artifacts": {"kernel": artifact(temporary / "kernel.macho", temporary),
                          "monitor": artifact(temporary / "monitor.macho", temporary),
                          "ans_firmware": artifact(temporary / "ans-firmware.bin", temporary),
                          "devicetree": artifact(temporary / "DeviceTree.raw", temporary),
                          "system_disk": artifact(system, temporary),
                          "data_disk": artifact(data_disk, temporary, mutable=True)},
            "firmware": {"ipsw_filename": Path(ipsw_path).name, "ipsw_sha256": sha256_file(ipsw_path),
                         "identity": identity["Info"], "kernel_metadata": kernel_info, "monitor_metadata": monitor_info,
                         "monitor_boot_contract": monitor_contract,
                         "ans_metadata": ans_info,
                         "originals": {p.name: artifact(p, temporary) for p in original.iterdir()},
                         "key_source": "https://theapplewiki.com/wiki/Keys:Sochi_11D257_(iPhone6,2)"},
            "storage": {"layout": "separate-raw-hfs-partitions", "system": system_info, "data": data_info,
                        "data_provisioned": False, "guest_mapping_verified": False,
                        "reason": "Driver disk numbering, fstab and initial private/var require verified guest bring-up"},
            "capabilities": {"firmware_validated": True, "kernel_boot_verified": False,
                             "userspace_verified": False, "display_verified": False, "input_verified": False},
            "preparation_status": "validated-firmware-only",
        }
        (temporary / "vm_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        temporary.rename(output)
        print(f"Validated firmware bundle: {output}; iOS boot/display remain unverified")
        return manifest
    except Exception:
        shutil.rmtree(temporary)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", "-v", required=True, choices=list_supported_versions())
    parser.add_argument("--ipsw", "-i", required=True)
    parser.add_argument("--out-dir", "-o", default=str(PROJECT_ROOT / "build/vm_bundle"))
    parser.add_argument("--boot-args")
    args = parser.parse_args()
    try:
        prepare_firmware(args.version, args.ipsw, args.out_dir, args.boot_args)
    except (ValueError, RuntimeError, OSError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
        detail = (error.stderr.decode(errors="replace") if isinstance(error, subprocess.CalledProcessError) and isinstance(error.stderr, bytes) else getattr(error, "stderr", "")) or ""
        parser.exit(1, f"Firmware preparation failed: {error}\n{detail}")


if __name__ == "__main__":
    main()
