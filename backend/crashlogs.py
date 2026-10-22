#!/usr/bin/env python3
"""Shared helper module to collect guest crash reports from disposable data clones.

Attaches the data disk image read-only using hdiutil, copies CrashReporter logs
(all .ips/.plist/.crash and DiagnosticLogs) to an output directory, builds an
index.json summary, and safely detaches the image via try/finally.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


def extract_root_device(stdout: str) -> str | None:
    """Extract the base device node (/dev/diskX) from hdiutil output."""
    for line in stdout.splitlines():
        line = line.strip()
        m = re.match(r'^(/dev/disk\d+)', line)
        if m:
            return m.group(1)
    return None


def parse_hdiutil_attach_output(stdout: str) -> tuple[str, Path]:
    """Parse hdiutil attach stdout and locate the mounted HFS volume.

    Handles images with or without partition maps (SPUD/Apple_partition_scheme,
    GUID_partition_scheme, etc.). Returns (dev_node, mount_point).
    Raises RuntimeError if no mount point or HFS volume is found.
    """
    root_dev = extract_root_device(stdout)
    candidate_mount = None
    candidate_dev = None

    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith('/dev/'):
            continue
        # Split by tab if present, else by 2 or more whitespace characters
        if '\t' in line:
            parts = [p.strip() for p in line.split('\t') if p.strip()]
        else:
            parts = [p.strip() for p in re.split(r'\s{2,}', line) if p.strip()]

        if len(parts) >= 2:
            dev = parts[0]
            mount = None
            ptype = ''
            for part in parts[1:]:
                if part.startswith('/'):
                    mount = part
                elif not ptype:
                    ptype = part

            if mount:
                mount_path = Path(mount)
                if 'HFS' in ptype or 'Apple_HFS' in ptype:
                    return (root_dev or dev, mount_path)
                if candidate_mount is None:
                    candidate_mount = mount_path
                    candidate_dev = dev

    if candidate_mount is not None:
        return (root_dev or candidate_dev or '', candidate_mount)

    raise RuntimeError(f"Could not find mounted HFS volume in hdiutil attach output:\n{stdout}")


def detach_disk_image(dev: str) -> None:
    """Detach an attached disk image given its device node or mount point."""
    if not dev:
        return
    res = subprocess.run(["hdiutil", "detach", str(dev)], capture_output=True, text=True)
    if res.returncode != 0:
        subprocess.run(["hdiutil", "detach", "-force", str(dev)], capture_output=True, text=True, check=True)


def attach_disk_image(image_path: Path | str) -> tuple[str, Path]:
    """Attach a disk image read-only using hdiutil.

    Uses 'hdiutil attach -readonly -nobrowse -noautoopen -imagekey diskimage-class=CRawDiskImage <file>'.
    Ensures that if parsing fails, the disk image is detached before raising.
    """
    cmd = [
        "hdiutil", "attach",
        "-readonly",
        "-nobrowse",
        "-noautoopen",
        "-imagekey", "diskimage-class=CRawDiskImage",
        str(image_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    root_dev = extract_root_device(res.stdout)
    try:
        dev, mount_point = parse_hdiutil_attach_output(res.stdout)
        return dev, mount_point
    except Exception:
        if root_dev:
            try:
                detach_disk_image(root_dev)
            except Exception:
                pass
        raise


def parse_process_name(path: Path) -> str:
    """Parse process name from file header (IPS json / text header) or filename."""
    # First try reading header if it's a file
    try:
        if path.is_file():
            with path.open("r", errors="ignore") as f:
                first_line = f.readline().strip()
                if first_line.startswith("{") and first_line.endswith("}"):
                    try:
                        data = json.loads(first_line)
                        name = (
                            data.get("app_name")
                            or data.get("name")
                            or data.get("Process")
                            or data.get("bundleID")
                        )
                        if name:
                            return str(name)
                    except Exception:
                        pass
                # Check next 50 lines for 'Process: <name> [<pid>]' or 'Process: <name>'
                lines = [first_line] + [f.readline() for _ in range(50)]
                for line in lines:
                    m = re.match(r"^Process:\s*([^\s\[]+)", line)
                    if m:
                        return m.group(1)
    except Exception:
        pass

    # If symlink points to another file, try parsing target
    if path.is_symlink():
        try:
            target = path.resolve()
            if target.exists() and target != path:
                target_proc = parse_process_name(target)
                if target_proc and target_proc != "LatestCrash":
                    return target_proc
        except Exception:
            pass

    # Fallback: parse from name
    stem = path.stem
    if stem.startswith("LatestCrash-"):
        return stem[len("LatestCrash-") :]

    if ".log." in path.name:
        stem = path.name.split(".log.")[0]
    elif stem.endswith(".log"):
        stem = stem[:-4]

    m = re.match(r"^([A-Za-z0-9_\-\.]+?)_\d{4}-\d{2}-\d{2}", stem)
    if m:
        return m.group(1)

    m = re.match(r"^([A-Za-z0-9_\-\.]+?)-\d{4}-\d{2}-\d{2}", stem)
    if m:
        return m.group(1)

    if "_" in stem:
        return stem.split("_")[0]

    if "." in stem and not stem.startswith("com."):
        return stem.split(".")[0]

    return stem


def build_crashlog_index(crashlogs_dir: Path | str) -> list[dict]:
    """Scan crashlogs_dir and return a list of file metadata records.

    Each record contains:
      - file: relative file path inside crashlogs_dir
      - process: parsed process name
      - process_name: alias of process
      - size: file size in bytes
      - mtime: file modification time (seconds since epoch)
    Excludes index.json itself.
    """
    crashlogs_dir = Path(crashlogs_dir)
    entries = []
    if not crashlogs_dir.exists():
        return entries

    for p in sorted(crashlogs_dir.rglob("*")):
        if p.name == "index.json":
            continue
        if p.is_file() or p.is_symlink():
            rel_path = p.relative_to(crashlogs_dir).as_posix()
            try:
                st = p.stat()
            except OSError:
                st = p.lstat()

            proc = parse_process_name(p)
            entry = {
                "file": rel_path,
                "process": proc,
                "process_name": proc,
                "size": st.st_size,
                "mtime": st.st_mtime,
            }
            entries.append(entry)

    entries.sort(key=lambda e: e["file"])
    return entries


def collect_crashlogs(image_path: Path | str, out_dir: Path | str) -> list[dict]:
    """Attach data clone read-only, copy crash reports, write index.json, and detach.

    Copies mobile/Library/Logs/CrashReporter/ (all .ips/.plist/.crash and DiagnosticLogs)
    to out_dir. Guarantees detachment of the image even on error.
    Returns the list of indexed entries.
    """
    image_path = Path(image_path).resolve()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    dev, mount_point = attach_disk_image(image_path)
    try:
        crash_src = mount_point / "mobile/Library/Logs/CrashReporter"
        if crash_src.exists():
            for item in crash_src.iterdir():
                if item.name == "DiagnosticLogs" and item.is_dir():
                    dst_diag = out_dir / "DiagnosticLogs"
                    if dst_diag.exists():
                        shutil.rmtree(dst_diag)
                    shutil.copytree(item, dst_diag, symlinks=True)
                elif item.suffix in (".ips", ".plist", ".crash"):
                    dst_file = out_dir / item.name
                    if dst_file.is_symlink() or dst_file.exists():
                        dst_file.unlink()
                    shutil.copy2(item, dst_file, follow_symlinks=False)

        index_entries = build_crashlog_index(out_dir)
        index_file = out_dir / "index.json"
        index_file.write_text(json.dumps(index_entries, indent=2) + "\n")
        return index_entries
    finally:
        detach_disk_image(dev)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path, help="Path to data.raw disk image clone")
    parser.add_argument("output", type=Path, help="Directory to save crashlogs")
    args = parser.parse_args()
    collect_crashlogs(args.image, args.output)
