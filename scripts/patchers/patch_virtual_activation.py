#!/usr/bin/env python3
"""Opt-in, exact-build lockdownd patch for virtual activation testing only.

11D257 dealwith_activation already has a native short-circuit path which sets
ActivationState (global and cache) to Activated, acknowledges it, sets
BrickState=false, and posts com.apple.mobile.lockdown.brick_state. Select that
path by removing its predicate branch; do not replace API getters or storage.
This is NOT Apple activation. Code-signing/loading is a separate prerequisite.
"""
import argparse
import hashlib
import json
from pathlib import Path

ORIGINAL_SHA256 = "d1421e2fc37a76732717e23da1c276c80b51d0e2da1599765b0afbbe585d426e"
OFFSET = 0x12E5C
ORIGINAL = bytes.fromhex("c0070034")  # cbz w0, 0x100012f54
REPLACEMENT = bytes.fromhex("1f2003d5")  # nop; native fallthrough 0x100012e60
CONTEXT_OFFSET = 0x12E58
CONTEXT = bytes.fromhex("2d550094c007003400008052e102009021142391")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def patch(data):
    if len(data) <= OFFSET + 4:
        raise ValueError("Truncated lockdownd")
    # Recognize our output explicitly, without accepting arbitrary prepatched data.
    restored = data[:OFFSET] + ORIGINAL + data[OFFSET + 4:]
    if data[OFFSET:OFFSET + 4] == REPLACEMENT and sha256(restored) == ORIGINAL_SHA256:
        raise ValueError("lockdownd is already patched")
    if sha256(data) != ORIGINAL_SHA256:
        raise ValueError("Unsupported lockdownd SHA-256; only n53ap 11D257 is accepted")
    if data[CONTEXT_OFFSET:CONTEXT_OFFSET + len(CONTEXT)] != CONTEXT:
        raise ValueError("Activation branch context mismatch")
    result = data[:OFFSET] + REPLACEMENT + data[OFFSET + 4:]
    return result, {
        "profile": "virtual-activated",
        "classification": "virtual activation for testing; not Apple activation",
        "build": "11D257", "device": "iPhone6,2/n53ap",
        "original_sha256": ORIGINAL_SHA256, "patched_sha256": sha256(result),
        "patches": [{"file_offset": OFFSET, "unslid_va": "0x100012e5c",
                     "before": ORIGINAL.hex(), "after": REPLACEMENT.hex(),
                     "reason": "Select the existing native activation short-circuit"}],
        "code_signature": "Original signature retained but invalidated by patch; loading must be verified",
        "runtime_verified": False,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--manifest", type=Path, required=True)
    a = p.parse_args()
    if a.output.exists() or a.manifest.exists():
        p.error("Output and manifest must not already exist")
    result, manifest = patch(a.input.read_bytes())
    with a.output.open("xb") as f:
        f.write(result)
    a.output.chmod(0o755)
    with a.manifest.open("x") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")


if __name__ == "__main__":
    main()
