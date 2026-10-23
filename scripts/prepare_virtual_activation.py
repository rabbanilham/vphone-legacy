#!/usr/bin/env python3
"""Build an isolated, opt-in iOS 7.1.2 virtual activation test profile.

Native AMFI diagnostic boot arguments relax guest code signing for this profile.
This changes the guest security policy; baseline plans/images remain untouched.
The injected guest probe only queries liblockdown and restarts its launchd job.
"""
import argparse
import hashlib
import json
import plistlib
import shutil
import struct
import subprocess
from pathlib import Path

from prepare_diagnostic_disks import detach, seed_setup_done
from patchers.patch_virtual_activation import patch
from patchers.patch_devicetree import parse_node, serialize_node
from qemu.hfs_ownership import set_catalog_owner
from patchers.sign_diagnostic_probe import sign_probe

ROOT = Path(__file__).resolve().parents[1]
CACHE_OFFSET, CACHE_SIZE = 0x989AC, 0x2760C
CACHE_SHA256 = "2ea3fdde29a7a15c61a9066b8aae127940361cb9967e644d665f5fa7889de9c6"
LABEL = "org.vphone.virtual-activation-probe"
DIAGNOSTIC_ARGS = "amfi_allow_any_signature=1 amfi_get_out_of_my_way=1 cs_enforcement_disable=1"


def attach(image):
    result = subprocess.run(["hdiutil", "attach", "-plist", "-nobrowse", "-imagekey",
                             "diskimage-class=CRawDiskImage", str(image)],
                            capture_output=True, check=True)
    entities = plistlib.loads(result.stdout)["system-entities"]
    devices = [e["dev-entry"] for e in entities if "dev-entry" in e]
    try:
        mount = next(e["mount-point"] for e in entities if "mount-point" in e)
        return devices[0], Path(mount)
    except (StopIteration, IndexError):
        if devices:
            detach(devices[0])
        raise RuntimeError("Image attached without a mounted filesystem")


def digest(path):
    with Path(path).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def clone(src, dst):
    if dst.exists():
        raise ValueError(f"Refusing to replace {dst}")
    # macOS APFS clone avoids unnecessary copies of multi-gigabyte images.
    subprocess.run(["cp", "-c", str(src), str(dst)], check=True)


def build_probe(out):
    stubs = {
        "libSystem.tbd": ("/usr/lib/libSystem.B.dylib",
                          "sleep open write close snprintf fork execv waitpid dup2 _exit getpid __error"),
        "liblockdown.tbd": ("/usr/lib/liblockdown.dylib",
                            "lockdown_connect lockdown_copy_value lockdown_disconnect"),
        "CoreFoundation.framework/CoreFoundation.tbd": (
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation",
            "CFStringCreateWithCString CFStringGetCString CFGetTypeID CFStringGetTypeID "
            "CFBooleanGetTypeID CFBooleanGetValue CFRelease"),
        "SpringBoardServices.framework/SpringBoardServices.tbd": (
            "/System/Library/PrivateFrameworks/SpringBoardServices.framework/SpringBoardServices",
            "SBSUndimScreen"),
    }
    for name, (install, symbols) in stubs.items():
        path = out / "stubs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("--- !tapi-tbd-v3\narchs: [ arm64 ]\nplatform: ios\ninstall-name: "
                        + install + "\nexports:\n  - archs: [ arm64 ]\n    symbols: [ "
                        + ", ".join("_" + s for s in symbols.split()) + " ]\n...\n")
    obj, binary = out / "probe.o", out / "virtual-activation-probe"
    subprocess.run(["clang", "-target", "arm64-apple-ios7.0", "-isysroot", "/", "-nostdinc",
                    "-ffreestanding", "-fno-stack-protector", "-O2", "-Wall", "-Wextra",
                    "-c", str(ROOT / "scripts/virtual_activation_probe.c"), "-o", str(obj)], check=True)
    # Eager binding also avoids an indirect-symbol-table assertion in ld 27.
    subprocess.run(["ld", "-arch", "arm64", "-ios_version_min", "7.0", "-bind_at_load",
                    "-L", str(out / "stubs"), "-F", str(out / "stubs"),
                    "-lSystem", "-llockdown", "-framework", "CoreFoundation",
                    "-framework", "SpringBoardServices",
                    "-e", "_main", str(obj), "-o", str(binary)], check=True)
    binary.write_bytes(sign_probe(binary.read_bytes()))
    return binary


def probe_job():
    return {"Label": LABEL, "ProgramArguments": ["/usr/libexec/vphone-activation-probe"],
            "RunAtLoad": True, "UserName": "root", "ThrottleInterval": 30,
            "StandardOutPath": "/dev/console", "StandardErrorPath": "/dev/console"}


def patch_job_cache(original):
    if hashlib.sha256(original).hexdigest() != CACHE_SHA256:
        raise ValueError("Unsupported xpcd cache section; refusing guessed offsets")
    value = plistlib.loads(original)
    value["LaunchDaemons"][f"/System/Library/LaunchDaemons/{LABEL}.plist"] = probe_job()
    # Keep the embedded Mach-O section size/offset and every following byte intact.
    value["VirtualActivationPadding"] = b""
    for _ in range(8):
        result = plistlib.dumps(value, fmt=plistlib.FMT_BINARY, sort_keys=False)
        delta = len(original) - len(result)
        if delta == 0:
            return result
        size = len(value["VirtualActivationPadding"]) + delta
        if size < 0:
            raise ValueError("Diagnostic job exceeds cache section capacity")
        value["VirtualActivationPadding"] = bytes(size)
    raise ValueError("Cannot preserve xpcd cache section size")


def prepare_boot(source, out):
    plan = json.loads(source.read_text())
    image_src = Path(plan["image"])
    if digest(image_src) != plan["image_sha256"]:
        raise ValueError("Source boot image hash does not match its plan")
    image = bytearray(image_src.read_bytes())
    root, _ = parse_node((source.parent / "DeviceTree.runtime").read_bytes(), 0)
    chosen = root.find_node("chosen")
    old_tree = serialize_node(root)
    args = plan["boot_args"] + " " + DIAGNOSTIC_ARGS
    raw_args = args.encode("ascii")
    if len(raw_args) >= 608:
        raise ValueError("Diagnostic boot arguments exceed native capacity")
    chosen.set_prop("debug-enabled", struct.pack("<I", 1))
    chosen.set_prop("boot-args", raw_args + b"\0")
    # Preserve all NVRAM partitions/variables except the existing boot-args value.
    nvram = chosen.get_prop("nvram-proxy-data")
    if nvram and b"boot-args=" in nvram:
        start = nvram.index(b"boot-args=")
        end = nvram.index(b"\0", start)
        replacement = b"boot-args=" + raw_args + b"\0"
        old = nvram[start:end + 1]
        # Common CHRP partition is 4096 bytes, followed by two unchanged partitions.
        common = nvram[:start] + replacement + nvram[end + 1:4096]
        growth = len(replacement) - len(old)
        if growth > 0 and any(common[4096:]):
            raise ValueError("NVRAM common partition has no spare boot-arg space")
        chosen.set_prop("nvram-proxy-data", common[:4096].ljust(4096, b"\0") + nvram[4096:])
    memory_map = chosen.find_node("memory-map")
    tree = serialize_node(root)
    if memory_map is not None:
        memory_map.set_prop("DeviceTree", struct.pack("<QQ", plan["devicetree_pa"], len(tree)))
        tree = serialize_node(root)
    dt = plan["devicetree_pa"] - plan["dram_base"]
    if len(tree) > ((len(old_tree) + 0x3fff) & ~0x3fff):
        raise ValueError("Diagnostic tree exceeds original reserved pages")
    if any(image[dt + len(old_tree):dt + len(tree)]):
        raise ValueError("DeviceTree growth would overwrite populated boot image bytes")
    image[dt:dt + len(tree)] = tree
    ba = plan["boot_args_pa"] - plan["dram_base"]
    image[ba + 108:ba + 716] = raw_args.ljust(608, b"\0")
    struct.pack_into("<I", image, ba + 104, len(tree))
    out.mkdir()
    (out / "KernelMemory.bin").write_bytes(image)
    (out / "DeviceTree.runtime").write_bytes(tree)
    (out / "boot_args.bin").write_bytes(image[ba:ba + 736])
    plan.update(image=str((out / "KernelMemory.bin").resolve()),
                image_sha256=hashlib.sha256(image).hexdigest(), boot_args=args,
                validation="virtual-activated diagnostic profile; runtime verification pending")
    (out / "boot_plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    return {"source_plan": str(source), "source_plan_sha256": digest(source),
            "source_image_sha256": digest(image_src), "image_sha256": plan["image_sha256"],
            "boot_args": args, "chosen_debug_enabled": True, "kernel_instruction_patches": []}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--system", type=Path, default=ROOT / "build/diagnostic/system-diagnostic-sep.raw")
    p.add_argument("--data", type=Path, default=ROOT / "build/diagnostic/data-diagnostic-systembag.raw")
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=True)
    manifest_path = a.output / "profile.json"
    if manifest_path.exists() or any((a.output / n).exists() for n in ("system.raw", "data.raw", "boot")):
        p.error("Output already contains a profile; choose a new directory")
    manifest = {"profile": "virtual-activated", "virtual_activation_only": True,
                "runtime_verified": False, "inputs": {str(x): digest(x) for x in (a.system, a.data)}}
    probe = build_probe(a.output)
    manifest["probe_sha256"] = digest(probe)
    manifest["boot"] = prepare_boot(a.plan.resolve(), a.output / "boot")
    system, data = a.output / "system.raw", a.output / "data.raw"
    clone(a.system, system)
    clone(a.data, data)
    dev, mount = attach(system)
    try:
        binary = mount / "usr/libexec/lockdownd"
        patched, manifest["lockdownd"] = patch(binary.read_bytes())
        binary.write_bytes(patched)  # Existing inode/guest ownership retained.
        dest = mount / "usr/libexec/vphone-activation-probe"
        shutil.copyfile(probe, dest)
        dest.chmod(0o755)
        probe_id = dest.stat().st_ino
        job = mount / f"System/Library/LaunchDaemons/{LABEL}.plist"
        job.write_bytes(plistlib.dumps(probe_job(), fmt=plistlib.FMT_BINARY))
        job.chmod(0o644)
        job_id = job.stat().st_ino
        cache = mount / "System/Library/Caches/com.apple.dyld/dyld_shared_cache_arm64"
        with cache.open("r+b") as f:
            f.seek(CACHE_OFFSET)
            patched_cache = patch_job_cache(f.read(CACHE_SIZE))
            f.seek(CACHE_OFFSET)
            f.write(patched_cache)
        manifest["job_cache"] = {"offset": CACHE_OFFSET, "size": CACHE_SIZE,
                                 "before_sha256": CACHE_SHA256,
                                 "after_sha256": hashlib.sha256(patched_cache).hexdigest()}
    finally:
        detach(dev)
    manifest["guest_ownership"] = [set_catalog_owner(system, file_id) for file_id in (probe_id, job_id)]
    dev, mount = attach(data)
    try:
        seed_setup_done(mount)
    finally:
        detach(dev)
    manifest["outputs"] = {str(x): digest(x) for x in (system, data)}
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(manifest_path, flush=True)


if __name__ == "__main__":
    main()
