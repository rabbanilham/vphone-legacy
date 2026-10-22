#!/usr/bin/env python3
"""CPU PMU realization contract. User-run after rebuilding: python3 backend/arm_pmu_contract_test.py."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time

from smoke_test import BINARY, BASE, command, fixture


def query_cpu_pmu(env_extra=None):
    with tempfile.TemporaryDirectory(prefix="pmu-contract-") as d:
        root = Path(d)
        img = root / "boot.bin"
        img.write_bytes(fixture())
        qmp = root / "qmp.sock"

        env = dict(os.environ)
        if env_extra:
            env.update(env_extra)
        else:
            env.pop("VPHONE_PMU", None)

        proc = subprocess.Popen([
            str(BINARY), "-machine",
            f"s5l8960x,boot-entry={hex(BASE)},boot-args={hex(BASE + 0x30000)}",
            "-kernel", str(img),
            "-display", "none", "-serial", "none",
            "-S", "-qmp", f"unix:{qmp},server=on,wait=off"
        ], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        try:
            deadline = time.monotonic() + 10
            while not qmp.exists():
                assert proc.poll() is None, proc.stderr.read().decode()
                assert time.monotonic() < deadline, "QMP startup timed out"
                time.sleep(0.01)

            sock = socket.socket(socket.AF_UNIX)
            sock.settimeout(5)
            sock.connect(str(qmp))
            with sock, sock.makefile("rwb") as fp:
                json.loads(fp.readline())
                command(fp, "qmp_capabilities")

                # Locate CPU path in QOM
                resp = command(fp, "qom-list", {"path": "/machine/unattached"})
                devices = resp.get("return", [])
                cpu_path = None
                for dev in devices:
                    name = dev.get("name")
                    if dev.get("type", "").startswith("child<"):
                        try:
                            t = command(fp, "qom-get", {"path": f"/machine/unattached/{name}", "property": "type"})
                            if "cortex-a57" in str(t.get("return", "")):
                                cpu_path = f"/machine/unattached/{name}"
                                break
                        except Exception:
                            pass

                if not cpu_path:
                    # fallback to checking device[0]
                    cpu_path = "/machine/unattached/device[0]"

                pmu_val = command(fp, "qom-get", {"path": cpu_path, "property": "pmu"})
                command(fp, "quit")
                return pmu_val.get("return")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


def main():
    assert BINARY.exists(), "Build backend first with scripts/qemu/build_backend.py"
    default_pmu = query_cpu_pmu()
    assert default_pmu is False, f"Default PMU expected False, got {default_pmu}"

    forced_pmu = query_cpu_pmu({"VPHONE_PMU": "1"})
    assert forced_pmu is True, f"VPHONE_PMU=1 expected True, got {forced_pmu}"

    print("PASS: CPU PMU property default=False, VPHONE_PMU=1 opt-out=True verified via QOM.")


if __name__ == "__main__":
    main()
