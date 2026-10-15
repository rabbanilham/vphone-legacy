"""Offline VM validation. A prepared bundle does not establish a booted guest."""
import hashlib
import json
from pathlib import Path
import subprocess

from macho import inspect_macho
from patchers.patch_devicetree import parse_node

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_MANIFEST = PROJECT_ROOT / "backend" / "backend.json"


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def resolve_artifact(bundle, record):
    if not isinstance(record, dict) or not isinstance(record.get("path"), str):
        raise ValueError("Missing artifact path")
    relative = Path(record["path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Artifact path must be relative to the VM bundle")
    result = (Path(bundle) / relative).resolve()
    if not result.is_relative_to(Path(bundle).resolve()) or not result.is_file():
        raise ValueError(f"Missing or escaping artifact: {relative}")
    return result


def validate_bundle(bundle, check_backend=True):
    bundle = Path(bundle).resolve()
    report = dict(bundle=str(bundle), checks=[], errors=[], limitations=[], prepared=False, bootable=False)

    def check(name, function):
        try:
            result = function()
            report["checks"].append(dict(name=name, ok=True, detail=result))
            return result
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            report["checks"].append(dict(name=name, ok=False, detail=str(exc)))
            report["errors"].append(f"{name}: {exc}")
            return None

    def load_manifest():
        value = json.loads((bundle / "vm_manifest.json").read_text())
        if not isinstance(value, dict):
            raise ValueError("Manifest must be a JSON object")
        if value.get("schema_version") != 2:
            raise ValueError("Legacy/unvalidated bundle; rebuild with vm create --ipsw. Existing files are preserved.")
        if tuple(value.get(k) for k in ("version", "build", "device", "board", "chip")) != ("7.1.2", "11D257", "iPhone6,2", "n53ap", "s5l8960x"):
            raise ValueError("Only n53ap iPhone6,2 / iOS 7.1.2 11D257 is validated")
        if not all(isinstance(value.get(key), dict) for key in ("backend", "artifacts", "storage", "firmware")):
            raise ValueError("Manifest backend, artifacts, storage and firmware must be objects")
        if value.get("memory_mb") != 1024 or value["backend"].get("id") != "s5l8960x":
            raise ValueError("Bundle requires the 1GiB s5l8960x machine")
        return value

    manifest = check("manifest", load_manifest)
    if manifest is None:
        return report
    paths = {}
    names = ("kernel", "monitor", "ans_firmware", "devicetree", "system_disk", "data_disk")
    if "system_runtime_disk" in manifest["artifacts"]:
        names += ("system_runtime_disk",)
    if "utility_disk" in manifest["artifacts"]:
        names += ("utility_disk",)
    for name in names:
        def validate_record(name=name):
            record = manifest["artifacts"][name]
            path = resolve_artifact(bundle, record)
            if path.stat().st_size != record["size"]:
                raise ValueError("Artifact size changed")
            if name in ("kernel", "monitor", "ans_firmware", "devicetree", "system_disk") and record.get("mutable"):
                raise ValueError("Immutable firmware artifact marked mutable")
            if record.get("mutable") is not True and digest(path) != record["sha256"]:
                raise ValueError("Artifact checksum mismatch")
            if name == "system_runtime_disk":
                source = manifest["artifacts"]["system_disk"]
                if (record.get("mutable") is not True or record.get("source_artifact") != "system_disk" or
                        record.get("source_sha256") != source["sha256"] or record["size"] != source["size"] or
                        path.samefile(resolve_artifact(bundle, source))):
                    raise ValueError("Writable system disk provenance is inconsistent with immutable source")
            paths[name] = path
            return {"path": str(path), "mutable": record.get("mutable", False)}
        check(name + " integrity", validate_record)
    if "kernel" in paths:
        check("ARM64 kernel", lambda: inspect_macho(paths["kernel"].read_bytes()))
    if "monitor" in paths:
        def monitor():
            info = inspect_macho(paths["monitor"].read_bytes())
            if info["text_vmaddr"] != 0x4100000000:
                raise ValueError("Unexpected native monitor link address")
            return info
        check("ARM64 native monitor", monitor)
    if "ans_firmware" in paths:
        def ans_firmware():
            from patchers.extract_ans import ANS_SHA256
            if paths["ans_firmware"].stat().st_size != 0x39000 or digest(paths["ans_firmware"]) != ANS_SHA256:
                raise ValueError("Unexpected native ANS firmware; rebuild from the verified iBoot component")
            return "Native AppleStorageProcessor-378.5.46; execution unverified"
        check("native ANS firmware", ans_firmware)
    if "devicetree" in paths:
        def validate_tree():
            data = paths["devicetree"].read_bytes()
            root, end = parse_node(data, 0)
            if root.get_prop("model") != b"iPhone6,2\0" or b"N53AP" not in (root.get_prop("compatible") or b"").split(b"\0"):
                raise ValueError("DeviceTree model/board mismatch")
            from fw_prepare import monitor_boot_contract
            contract = monitor_boot_contract(root)
            if manifest.get("firmware", {}).get("monitor_boot_contract") != contract:
                raise ValueError("Native monitor handoff contract missing or inconsistent; rebuild firmware")
            if any(data[end:]):
                raise ValueError("Unexpected trailing DeviceTree bytes")
            return "n53ap / iPhone6,2"
        check("Apple DeviceTree", validate_tree)
    for name in ("system_disk", "data_disk", "system_runtime_disk"):
        if name in paths:
            def hfs(name=name):
                from patchers.patch_rootfs import validate_raw_hfs
                return validate_raw_hfs(paths[name])
            check(name + " HFS geometry", hfs)
    if "utility_disk" in paths:
        def utility():
            from qemu.utility import validate_utility_image
            from patchers.extract_ans import ANS_SHA256
            record = manifest["artifacts"]["utility_disk"]
            if (record.get("mutable") is not True or record.get("source_ans_sha256") != ANS_SHA256 or
                    record.get("source_system_sha256") != manifest["artifacts"]["system_disk"]["sha256"]):
                raise ValueError("Logical utility artifact source/provenance mismatch")
            state = validate_utility_image(paths["utility_disk"],
                manifest["artifacts"]["system_disk"]["size"] // 4096,
                manifest["artifacts"]["data_disk"]["size"] // 4096)
            if state["format_epoch"] != record.get("format_epoch") or state["profile"] != record.get("profile"):
                raise ValueError("Logical utility epoch/profile differs from provisioning record")
            return state
        check("logical ASP utility namespaces", utility)
    report["prepared"] = not report["errors"]
    if check_backend:
        def backend():
            metadata = json.loads(BACKEND_MANIFEST.read_text())
            if not isinstance(metadata, dict) or not isinstance(metadata.get("capabilities"), dict):
                raise ValueError("Invalid backend manifest")
            if metadata.get("id") != "s5l8960x":
                raise ValueError("Backend id mismatch")
            if 3 not in metadata.get("boot_levels", []) or metadata.get("timebase_frequency") != 24000000:
                raise ValueError("Backend lacks the native EL3 monitor / 24MHz clock contract; rebuild")
            lock = json.loads((PROJECT_ROOT / "backend/source.lock.json").read_text())
            if metadata.get("source_commit") != lock["commit"] or metadata.get("source_tag") != lock["tag"]:
                raise ValueError("Backend differs from pinned QEMU source; rebuild")
            if metadata.get("machine_source_sha256") != digest(PROJECT_ROOT / "backend/s5l8960x.c"):
                raise ValueError("Backend machine source changed; rebuild")
            binary = (PROJECT_ROOT / metadata["binary"]).resolve()
            if not binary.is_relative_to((PROJECT_ROOT / "backend/bin").resolve()):
                raise ValueError("Backend binary must be in the project's backend/bin directory")
            if not binary.is_file() or digest(binary) != metadata["sha256"]:
                raise ValueError("Backend binary missing or checksum mismatch; run make backend")
            machines = subprocess.check_output([str(binary), "-machine", "help"], text=True, timeout=10)
            if not any(line.split() and line.split()[0] == "s5l8960x" for line in machines.splitlines()):
                raise ValueError("Backend has no s5l8960x machine")
            report["backend"] = metadata | {"binary": str(binary)}
            return {"binary": str(binary), "source_commit": metadata.get("source_commit")}
        check("project QEMU backend", backend)
    caps = report.get("backend", {}).get("capabilities", {})
    for capability in ("storage", "userspace", "graphics", "input", "guest_shutdown"):
        if caps.get(capability) is not True:
            report["limitations"].append(f"{capability}: not implemented/verified")
    if not manifest.get("storage", {}).get("data_provisioned"):
        report["limitations"].append("Data volume has not been provisioned by an iOS restore")
    # Never infer bootability from static file existence or backend flags.
    return report
