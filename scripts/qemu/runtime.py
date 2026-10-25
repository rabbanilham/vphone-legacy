#!/usr/bin/env python3
"""Own the QEMU process, QMP connection, serial logs and explicit runtime status."""
import argparse
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bundle import validate_bundle, resolve_artifact
from qemu.boot_image import build_image
from qemu.storage import prepare_system_runtime, snapshot_storage
from qemu.utility import prepare_utility_image


def write_json(path, value):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=path.name + ".", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(json.dumps(value, indent=2) + "\n")
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class PanicMonitor:
    """Read new UART bytes; retain failure even after a long panic backtrace."""
    header = re.compile(rb"(?m)^[\r ]*panic\(cpu [0-9]+ caller 0x[0-9a-fA-F]+\):")
    # Some native driver diagnostics omit a newline. XNU's panic header can
    # follow them on the same line; require its immediate debugger marker to
    # distinguish this from an ordinary diagnostic quoting a panic header.
    inline = re.compile(rb"panic\(cpu [0-9]+ caller 0x[0-9a-fA-F]+\):[^\n]*\n[\r ]*Debugger message: panic")

    def __init__(self):
        self.offset = 0
        self.tail = b""
        self.failed = False

    def poll(self, path):
        path = Path(path)
        if self.failed or not path.is_file():
            return self.failed
        if path.stat().st_size < self.offset:
            self.offset, self.tail = 0, b""
        with path.open("rb") as stream:
            stream.seek(self.offset)
            while chunk := stream.read(65536):
                data = self.tail + chunk
                self.offset = stream.tell()
                if self.header.search(data) or self.inline.search(data):
                    self.failed = True
                    return True
                # Retain both the header line and a partial following marker.
                # Truncation must not promote a quoted header to line start.
                self.tail = data if len(data) <= 4096 else b"x" + data[-4095:]
        return False


def kernel_panic(path):
    """Recognize a complete XNU panic header as failure, never as readiness."""
    return PanicMonitor().poll(path)


class QMP:
    def __init__(self, path, timeout=3):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        try:
            self.sock.connect(str(path))
            self.stream = self.sock.makefile("rwb", buffering=0)
            greeting = self.receive()
            if "QMP" not in greeting:
                raise ValueError("Invalid QMP greeting")
            self.execute("qmp_capabilities")
        except Exception:
            self.sock.close()
            raise

    def receive(self):
        line = self.stream.readline(1024 * 1024)
        if not line or len(line) >= 1024 * 1024:
            raise ValueError("QMP connection closed or response too large")
        return json.loads(line)

    def execute(self, command, arguments=None):
        ident = str(uuid.uuid4())
        request = {"execute": command, "id": ident}
        if arguments is not None:
            request["arguments"] = arguments
        self.stream.write(json.dumps(request).encode() + b"\n")
        while True:
            result = self.receive()
            if result.get("id") == ident:
                if "error" in result:
                    raise ValueError(result["error"].get("desc", "QMP error"))
                return result.get("return")

    def close(self):
        self.stream.close()
        self.sock.close()


def stop(bundle, force=False):
    bundle = Path(bundle)
    status = json.loads((bundle / "runtime.json").read_text())
    if status["process"]["phase"] not in ("running", "starting", "stopping"):
        raise ValueError("VM is not running")
    if not force and status.get("capabilities", {}).get("guest_shutdown") is not True:
        raise ValueError("Guest shutdown is unavailable in this experimental backend. Use vm stop NAME --force to terminate explicitly.")
    # Only the owning launcher connects to QMP: a socket chardev accepts one client.
    with (bundle / "runtime.lock").open("r") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            write_json(bundle / "stop-request.json", {"session_id": status["session_id"], "force": force})
            return
    raise ValueError("Runtime status is stale; no active launcher owns this VM")


def launch(bundle, headless=False, probe_seconds=None, paused=False):
    bundle = Path(bundle).resolve()
    report = validate_bundle(bundle)
    if report["errors"]:
        raise ValueError("\n".join(report["errors"]))
    manifest = json.loads((bundle / "vm_manifest.json").read_text())
    backend = report["backend"]
    with (bundle / "runtime.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("VM already has an active launcher") from None
        storage = backend.get("ans_storage_model", {})
        storage_paths = None
        if storage.get("explicit_image_properties") is True:
            record = prepare_system_runtime(bundle, manifest)
            if manifest["artifacts"].get("system_runtime_disk") is None:
                manifest["artifacts"]["system_runtime_disk"] = record
                write_json(bundle / "vm_manifest.json", manifest)
            storage_paths = {
                "system": str(resolve_artifact(bundle, record)),
                "data": str(resolve_artifact(bundle, manifest["artifacts"]["data_disk"])),
            }
            if storage.get("utility_image_property") is True:
                utility = prepare_utility_image(bundle, manifest)
                if manifest["artifacts"].get("utility_disk") is None:
                    manifest["artifacts"]["utility_disk"] = utility
                    write_json(bundle / "vm_manifest.json", manifest)
                storage_paths["utility"] = str(resolve_artifact(bundle, utility))
        logs = bundle / "logs" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
        logs.mkdir(parents=True, exist_ok=True)
        snapshots = snapshot_storage(storage_paths, logs / "storage-before") if storage_paths else None
        if snapshots:
            write_json(logs / "storage-before.json", snapshots)
        # Keep generated RAM/DT/ANS inputs beside their evidence: a later launch
        # must not overwrite the bytes named by this session's command/plan.
        plan = build_image(resolve_artifact(bundle, manifest["artifacts"]["kernel"]),
                           resolve_artifact(bundle, manifest["artifacts"]["devicetree"]),
                           logs / "boot", manifest.get("boot_args", "-v serial=3 debug=0x14e"),
                           monitor_path=resolve_artifact(bundle, manifest["artifacts"]["monitor"]),
                           ans_path=resolve_artifact(bundle, manifest["artifacts"]["ans_firmware"]))
        write_json(logs / "boot-plan.json", plan)
        write_json(logs / "build-identity.json", {
            "backend": {key: backend[key] for key in ("source_commit", "sha256", "machine_source_sha256")},
            "loader_sha256": hashlib.sha256((Path(__file__).parent / "boot_image.py").read_bytes()).hexdigest(),
            "loader_sources": {
                name: hashlib.sha256((Path(__file__).parents[1] / name).read_bytes()).hexdigest()
                for name in ("qemu/runtime.py", "qemu/boot_image.py", "qemu/ans_image.py",
                             "qemu/storage.py", "qemu/utility.py", "patchers/extract_ans.py", "patchers/patch_devicetree.py")
            },
            "firmware": {key: manifest[key] for key in ("device", "board", "version", "build")},
            "artifacts": manifest["artifacts"],
            "storage_images": storage_paths,
            "storage_before": snapshots,
        })
        stages = {name: dict(phase="unverified", evidence=None) for name in ("kernel", "userspace", "display", "agent")}
        status = dict(schema_version=1, session_id=str(uuid.uuid4()),
                      process=dict(phase="starting", pid=None, exit_code=None),
                      shutdown=dict(clean=None, reason=None), capabilities=backend.get("capabilities", {}),
                      log_directory=str(logs), **stages)
        # macOS Unix socket paths are limited to 104 bytes; tempfile root is short.
        with tempfile.TemporaryDirectory(prefix="vpl-", dir="/tmp") as control:
            qmp_path = str(Path(control) / "qmp.sock")
            status["control"] = {"qmp": qmp_path}
            machine = f"s5l8960x,boot-el=3,boot-entry={plan['initial_entry_pa']:#x},boot-args={plan['initial_boot_args_pa']:#x},framebuffer={plan['framebuffer_pa']:#x}"
            if storage_paths:
                # QemuOpts accepts doubled commas in string property values.
                system = storage_paths["system"].replace(",", ",,")
                data = storage_paths["data"].replace(",", ",,")
                machine += f",system-image={system},data-image={data},system-writable=1"
                if "utility" in storage_paths:
                    machine += ",utility-image=" + storage_paths["utility"].replace(",", ",,")
            # MTTCG by default, as live_run.py and merge-live.sh boot to Home.
            accelerator = "tcg,thread=" + os.environ.get("VPHONE_TCG_THREAD", "multi")
            # -d int logs every exception and timer FIQ (~100 MB per boot) and
            # unimp logs every modeled MMIO access; both slow the guest a lot.
            trace_mask = os.environ.get("VPHONE_QEMU_DEBUG", "guest_errors")
            command = [backend["binary"], "-machine", machine, "-accel", accelerator,
                       "-smp", str(plan["cpu_count"]), "-m", "1024M",
                       "-kernel", plan["image"], "-display", "none" if headless else "cocoa",
                       "-qmp", f"unix:{qmp_path},server=on,wait=off", "-monitor", "none",
                       "-chardev", f"stdio,id=console,signal=off,logfile={logs / 'serial.log'}",
                       "-serial", "chardev:console", "-d", trace_mask, "-D", str(logs / "qemu-trace.log")]
            for image in plan["auxiliary_images"]:
                command += ["-device", f"loader,file={image['path']},addr={image['address']:#x},force-raw=on"]
            if probe_seconds is not None or paused:
                command.append("-S")
            # No virtio disks/network: legacy guest drivers do not support them.
            # ANS serves explicit raw partition files; no generic virtio disk.
            write_json(logs / "command.json", command)
            write_json(bundle / "runtime.json", status)
            print("Launching original secure monitor and XNU on experimental A7; no boot/UI readiness claimed.", flush=True)
            for limitation in report["limitations"]:
                print("  " + limitation, flush=True)
            interrupted = False
            old_handlers = {}

            def interrupt(signum, frame):
                nonlocal interrupted
                interrupted = True

            for sig in (signal.SIGINT, signal.SIGTERM):
                old_handlers[sig] = signal.signal(sig, interrupt)
            process = None
            qmp = None
            reason = None
            launch_failed = False
            verbose_trace = "int" in trace_mask.split(",")
            panic_monitor = PanicMonitor()
            try:
                process = subprocess.Popen(command, start_new_session=True)
                status["process"].update(phase="running", pid=process.pid)
                write_json(bundle / "runtime.json", status)
                deadline = time.monotonic() + 10
                while process.poll() is None and time.monotonic() < deadline:
                    if Path(qmp_path).exists():
                        qmp = QMP(qmp_path)
                        break
                    if interrupted:
                        break
                    time.sleep(0.05)
                if process.poll() is not None:
                    raise ValueError(f"QEMU exited during startup (code {process.returncode})")
                if qmp is None:
                    raise ValueError("QMP startup timed out")
                write_json(logs / "qmp-start.json", qmp.execute("query-status"))
                if probe_seconds is not None:
                    before = qmp.execute("human-monitor-command", {"command-line": "info registers"})
                    (logs / "registers-before.txt").write_text(before)
                    qmp.execute("cont")
                started = time.monotonic()
                while process.poll() is None:
                    request_path = bundle / "stop-request.json"
                    if request_path.exists():
                        request = json.loads(request_path.read_text())
                        if request.get("session_id") == status["session_id"]:
                            request_path.unlink()
                            if request.get("force"):
                                reason = "explicit forced stop"
                                break
                            if backend.get("capabilities", {}).get("guest_shutdown") is True:
                                qmp.execute("system_powerdown")
                                status["process"]["phase"] = "stopping"
                                write_json(bundle / "runtime.json", status)
                    if panic_monitor.poll(logs / "serial.log"):
                        reason = "XNU panic observed on guest UART; boot failed"
                        launch_failed = True
                        status["kernel"] = dict(phase="failed", evidence=str(logs / "serial.log"), reason=reason)
                        break
                    trace = logs / "qemu-trace.log"
                    if verbose_trace and trace.exists() and trace.stat().st_size > 16 * 1024 * 1024:
                        # Normal timer FIQs and demand-page faults also produce
                        # int traces. A byte limit is not evidence of failure.
                        qmp.execute("human-monitor-command", {"command-line": "log " + ",".join(
                            m for m in trace_mask.split(",") if m != "int")})
                        verbose_trace = False
                        status["diagnostics"] = dict(interrupt_trace_limited=True, trace=str(trace),
                                                     reason="16MiB interrupt trace budget reached; device-error tracing continues")
                        write_json(bundle / "runtime.json", status)
                        write_json(logs / "trace-budget.json", status["diagnostics"])
                        print("Interrupt trace budget reached; device-error tracing continues.", flush=True)
                    if interrupted:
                        reason = "launcher interrupted; guest shutdown unverified"
                        break
                    if probe_seconds is not None and time.monotonic() - started >= probe_seconds:
                        qmp.execute("stop")
                        after = qmp.execute("human-monitor-command", {"command-line": "info registers"})
                        (logs / "registers-after.txt").write_text(after)
                        write_json(logs / "qmp-end.json", qmp.execute("query-status"))
                        qmp.execute("screendump", {"filename": str(logs / "framebuffer.ppm")})
                        reason = "bounded kernel probe; guest shutdown unverified"
                        break
                    time.sleep(0.1)
                if reason:
                    if launch_failed:
                        qmp.execute("stop")
                        (logs / "registers-after.txt").write_text(qmp.execute("human-monitor-command", {"command-line": "info registers"}))
                        write_json(logs / "qmp-end.json", qmp.execute("query-status"))
                        qmp.execute("screendump", {"filename": str(logs / "framebuffer.ppm")})
                    status["process"]["phase"] = "stopping"
                    write_json(bundle / "runtime.json", status)
                    try:
                        qmp.execute("quit")
                    except (OSError, ValueError):
                        pass
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait()
                code = process.wait()
                request_path = bundle / "stop-request.json"
                request = json.loads(request_path.read_text()) if request_path.exists() else {}
                if request.get("session_id") == status["session_id"]:
                    reason = "explicit forced stop" if request["force"] else "guest shutdown requested; completion unverified"
                    request_path.unlink()
                status["process"].update(phase="failed" if launch_failed or code != 0 else "stopped", pid=None, exit_code=code)
                status["shutdown"] = dict(clean=False, reason=reason or "QEMU exited; guest filesystem shutdown not verified")
                write_json(bundle / "runtime.json", status)
                print(f"QEMU exited ({code}). " + (f"Launch failed: {reason}. " if launch_failed else "") + f"Evidence: {logs}", flush=True)
                return code or (1 if launch_failed else 0)
            except Exception as exc:
                status["process"].update(phase="failed", exit_code=process.poll() if process else None)
                status["shutdown"] = dict(clean=False, reason=str(exc))
                write_json(bundle / "runtime.json", status)
                raise
            finally:
                if qmp:
                    qmp.close()
                if process and process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                if status["process"]["phase"] == "failed":
                    status["process"].update(pid=None, exit_code=process.returncode if process else None)
                    write_json(bundle / "runtime.json", status)
                write_json(logs / "runtime-final.json", status)
                for sig, handler in old_handlers.items():
                    signal.signal(sig, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--probe-seconds", type=float)
    parser.add_argument("--paused", action="store_true")
    args = parser.parse_args()
    if args.probe_seconds is not None and not 0 < args.probe_seconds <= 30:
        parser.error("probe-seconds must be greater than 0 and at most 30")
    try:
        sys.exit(launch(args.bundle, args.headless, args.probe_seconds, args.paused))
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Launch error: {exc}\n")


if __name__ == "__main__":
    main()
