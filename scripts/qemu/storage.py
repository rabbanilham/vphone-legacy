"""Persistent writable guest system derivative; preserve the firmware original."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from bundle import digest, resolve_artifact
from patchers.patch_rootfs import validate_raw_hfs


def copy_raw_image(source, destination, expected_sha256):
    """Atomically clone/copy to a new name, checking exact source bytes."""
    source, destination = Path(source), Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ValueError("Destination image already exists; existing contents preserved")
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix="disk-copy-", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        if sys.platform == "darwin":
            # clonefile requires an absent target. An existing empty tempfile
            # makes cp fall back to a full copy instead of an APFS clone.
            temporary.unlink()
            # macOS cp -c clones on supported filesystems and copies otherwise.
            try:
                subprocess.run(["/bin/cp", "-c", "-p", str(source), str(temporary)],
                               check=True, capture_output=True)
            except subprocess.CalledProcessError as exc:
                raise ValueError("Cannot create disk copy: " +
                                 exc.stderr.decode(errors="replace").strip()) from exc
        else:
            shutil.copy2(source, temporary)
        if digest(temporary) != expected_sha256 or os.path.samefile(temporary, source):
            raise ValueError("Disk copy differs from its recorded source")
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def snapshot_storage(paths, output_dir):
    """Retain pre-launch mutable disks so guest writes have a recoverable origin."""
    output = Path(output_dir)
    output.mkdir()
    records = {}
    for name, path in paths.items():
        path = Path(path)
        checksum = digest(path)
        snapshot = output / (name + ".raw")
        copy_raw_image(path, snapshot, checksum)
        records[name] = dict(path=str(snapshot), active_path=str(path),
                             size=path.stat().st_size, sha256=checksum)
    return records


def prepare_system_runtime(bundle, manifest):
    """Called under the VM launcher lock; return a record, never reset a disk."""
    bundle = Path(bundle).resolve()
    source_record = manifest["artifacts"]["system_disk"]
    source = resolve_artifact(bundle, source_record)
    existing = manifest["artifacts"].get("system_runtime_disk")
    if existing is not None:
        disk = resolve_artifact(bundle, existing)
        if (existing.get("mutable") is not True or existing.get("source_artifact") != "system_disk" or
                existing.get("source_sha256") != source_record["sha256"] or
                existing.get("size") != source_record["size"] or
                disk.stat().st_size != existing["size"] or
                os.path.samefile(disk, source)):
            raise ValueError("Writable system disk provenance/size is invalid; existing data preserved")
        validate_raw_hfs(disk)
        return existing
    destination = bundle / "system-runtime.raw"
    if destination.exists() or destination.is_symlink():
        raise ValueError("Unregistered system-runtime.raw exists; preserve it and resolve its provenance before launch")
    validate_raw_hfs(source)
    copy_raw_image(source, destination, source_record["sha256"])
    return dict(path=destination.name, size=source_record["size"], mutable=True,
                sha256=source_record["sha256"], source_sha256=source_record["sha256"],
                source_artifact="system_disk", purpose="Persistent guest HFS journal/system writes")
