#!/usr/bin/env python3
"""Read actual guest probe/state evidence from a stopped diagnostic Data disk."""
import argparse
import json
import plistlib
import re
from pathlib import Path
from crashlogs import attach_disk_image, detach_disk_image


def summarize(rows, state, before_text, after_text):
    # Files survive cold boots, so only verify the latest probe invocation.
    beginnings = [i for i, row in enumerate(rows) if row.get("sample") == 0]
    current = rows[beginnings[-1]:] if beginnings else []
    valid = [r for r in current if r.get("valid") is True]
    before = [r for r in valid if r.get("after_restart") is False]
    after = [r for r in valid if r.get("after_restart") is True]
    def correct(row):
        return row.get("ActivationState") == "Activated" and row.get("BrickState") is False
    def pid(text):
        matches = re.findall(r'\bPID\s*["\']?\s*=\s*(\d+)', text)
        return int(matches[-1]) if matches else None
    before_pid, after_pid = pid(before_text), pid(after_text)
    restarted = bool(before_pid and after_pid and before_pid != after_pid)
    # The native short-circuit marks global ActivationState volatile (f468),
    # but stores the same value persistently in the native lockdown_cache domain.
    persisted_activation = state.get("-ActivationState", state.get("com.apple.mobile.lockdown_cache-ActivationState"))
    persistent = persisted_activation == "Activated" and state.get("-BrickState") is False
    api = bool(before and after and all(correct(r) for r in valid))
    return {"api_source": "guest liblockdown client", "samples": len(current),
            "valid_samples": len(valid), "before_restart_samples": len(before),
            "after_restart_samples": len(after), "api_values_pass": api,
            "lockdownd_pid_before": before_pid, "lockdownd_pid_after": after_pid,
            "service_restart_verified": restarted, "persistent_values_pass": persistent,
            "persisted_activation_value": persisted_activation,
            "activation_checks_pass": api and restarted and persistent,
            "home_screen_verified": False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    dev, mount = attach_disk_image(a.data)
    names = ["virtual-activation-probe.jsonl", "virtual-activation-before-restart.txt",
             "virtual-activation-after-restart.txt", "virtual-activation-stop.txt"]
    try:
        for name in names:
            source = mount / "root" / name
            if source.exists():
                (a.output / name).write_bytes(source.read_bytes())
        ark = mount / "root/Library/Lockdown/data_ark.plist"
        value = plistlib.loads(ark.read_bytes()) if ark.exists() else {}
        state = {k: v for k, v in value.items()
                 if k in ("-ActivationState", "-BrickState", "-ActivationStateAcknowledged",
                          "com.apple.mobile.lockdown_cache-ActivationState")}
        (a.output / "persistent-state.json").write_text(json.dumps(state, indent=2) + "\n")
    finally:
        detach_disk_image(dev)
    probe = a.output / names[0]
    rows = [json.loads(line) for line in probe.read_text().splitlines()] if probe.exists() else []
    def text(name):
        path = a.output / name
        return path.read_text(errors="replace") if path.exists() else ""
    result = summarize(rows, state, text(names[1]), text(names[2]))
    (a.output / "verification.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
