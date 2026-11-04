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

# Honor VPHONE_LEGACY_ROOT if set in the environment
env_root = os.environ.get("VPHONE_LEGACY_ROOT")
REPO_ROOT = Path(env_root).resolve() if env_root else Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_DIR = REPO_ROOT / "backend"

for d in (str(SCRIPTS_DIR), str(BACKEND_DIR)):
    if d not in sys.path:
        sys.path.insert(0, d)

from bundle import validate_bundle, resolve_artifact
from qemu.boot_image import build_image
from qemu.storage import prepare_system_runtime, snapshot_storage
from qemu.utility import prepare_utility_image
from qemu.launch_player import (
    boot_image_options,
    get_repo_root,
    resolve_display_mode,
    build_qemu_command,
    build_player_command,
    BootStageMonitor,
    build_initial_runtime_status,
    handle_stop_request,
    cleanup_processes,
)


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


def host_launch_command(command):
    """Opt in to clearing inherited Darwin background policy before exec.

    Thread QoS is set in QEMU; taskpolicy -B only clears Darwin background
    policy and does not grant a QoS class. Keep argv structured (no shell).
    """
    policy = os.environ.get("VPHONE_TASKPOLICY", "inherit")
    if policy not in ("inherit", "foreground"):
        raise ValueError("VPHONE_TASKPOLICY must be inherit or foreground")
    if policy == "foreground":
        if sys.platform != "darwin":
            raise ValueError("VPHONE_TASKPOLICY=foreground requires macOS")
        return ["/usr/bin/taskpolicy", "-B", *command]
    return command


def launch(bundle, headless=False, probe_seconds=None, paused=False, wifi=False, display=None):
    bundle = Path(bundle).resolve()
    report = validate_bundle(bundle)
    if report["errors"]:
        raise ValueError("\n".join(report["errors"]))
    manifest = json.loads((bundle / "vm_manifest.json").read_text())
    backend = report["backend"]
    display_mode = resolve_display_mode(manifest, display_arg=display, headless=headless)
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
                           ans_path=resolve_artifact(bundle, manifest["artifacts"]["ans_firmware"]),
                           **boot_image_options(manifest))
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
        status = build_initial_runtime_status(str(uuid.uuid4()), logs, display_mode, backend.get("capabilities", {}))
        # macOS Unix socket paths are limited to 104 bytes; tempfile root is short.
        with tempfile.TemporaryDirectory(prefix="vpl-", dir="/tmp") as control:
            qmp_path = str(Path(control) / "qmp.sock")
            vnc_path = str(Path(control) / "vnc.sock") if display_mode == "player" else None
            control_qmp_path = str(Path(control) / "ctl.sock") if display_mode == "player" else None
            status["control"] = {"qmp": qmp_path}
            if control_qmp_path:
                status["control"]["launcher_qmp"] = control_qmp_path
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
            command = build_qemu_command(
                backend_binary=backend["binary"],
                machine=machine,
                accelerator=accelerator,
                cpu_count=plan["cpu_count"],
                kernel_image=plan["image"],
                display_mode=display_mode,
                qmp_path=qmp_path,
                serial_log=logs / "serial.log",
                trace_log=logs / "qemu-trace.log",
                trace_mask=trace_mask,
                vnc_path=vnc_path,
                control_qmp_path=control_qmp_path,
                auxiliary_images=plan.get("auxiliary_images", []),
                paused=paused,
                probe_seconds=probe_seconds,
            )
            # No virtio disks/network: legacy guest drivers do not support them.
            # ANS serves explicit raw partition files; no generic virtio disk.
            from host_wifi import configure_wifi
            child_env = configure_wifi(command, wifi)
            command = host_launch_command(command)
            write_json(logs / "command.json", command)
            write_json(bundle / "runtime.json", status)

            player_binary = None
            if display_mode == "player":
                from live_run import build_player
                player_binary = build_player(root=REPO_ROOT)

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
            player_process = None
            qmp = None
            reason = None
            launch_failed = False
            verbose_trace = "int" in trace_mask.split(",")
            panic_monitor = PanicMonitor()
            boot_monitor = BootStageMonitor()
            try:
                # QEMU's device models print diagnostics to stderr from vCPU threads that
                # hold the global lock. A pipe nobody drains would stall the whole VM
                # (and its QMP sockets), so stdio always goes to a file.
                qemu_output = open(logs / "qemu-output.log", "ab")
                try:
                    process = subprocess.Popen(command, start_new_session=True, env=child_env,
                                               stdin=subprocess.DEVNULL, stdout=qemu_output,
                                               stderr=subprocess.STDOUT)
                finally:
                    qemu_output.close()
                status["process"].update(phase="running", pid=process.pid)
                write_json(bundle / "runtime.json", status)
                deadline = time.monotonic() + 10
                while process.poll() is None and time.monotonic() < deadline:
                    launcher_qmp = control_qmp_path or qmp_path
                    if Path(launcher_qmp).exists() and (vnc_path is None or Path(vnc_path).exists()):
                        qmp = QMP(launcher_qmp)
                        break
                    if interrupted:
                        break
                    time.sleep(0.05)
                if process.poll() is not None:
                    raise ValueError(f"QEMU exited during startup (code {process.returncode})")
                if qmp is None:
                    raise ValueError("QMP startup timed out")
                write_json(logs / "qmp-start.json", qmp.execute("query-status"))

                if display_mode == "player" and not interrupted and process.poll() is None:
                    player_cmd = build_player_command(player_binary, vnc_path, qmp_path)
                    with open(logs / "player-output.log", "ab") as player_output:
                        player_process = subprocess.Popen(player_cmd, stdin=subprocess.DEVNULL,
                                                          stdout=player_output, stderr=subprocess.STDOUT)

                if probe_seconds is not None:
                    before = qmp.execute("human-monitor-command", {"command-line": "info registers"})
                    (logs / "registers-before.txt").write_text(before)
                    qmp.execute("cont")
                started = time.monotonic()
                while process.poll() is None:
                    if player_process is not None and player_process.poll() is not None:
                        reason = "player window closed"
                        break

                    stop_req, stop_reason, is_stopping = handle_stop_request(
                        bundle, status["session_id"], qmp=qmp,
                        guest_shutdown_capable=backend.get("capabilities", {}).get("guest_shutdown", False)
                    )
                    if stop_req:
                        reason = stop_reason
                        break
                    if is_stopping:
                        status["process"]["phase"] = "stopping"
                        write_json(bundle / "runtime.json", status)

                    stage = boot_monitor.poll(logs / "serial.log")
                    if stage != status.get("boot_stage"):
                        status["boot_stage"] = stage
                        write_json(bundle / "runtime.json", status)
                    if stage == "panic" or panic_monitor.poll(logs / "serial.log"):
                        reason = "XNU panic observed on guest UART; boot failed"
                        launch_failed = True
                        status["kernel"] = dict(phase="failed", evidence=str(logs / "serial.log"), reason=reason)
                        status["boot_stage"] = "panic"
                        write_json(bundle / "runtime.json", status)
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

                if process.poll() is not None and not reason:
                    reason = f"QEMU exited unexpectedly (code {process.returncode})"
                    if process.returncode != 0:
                        launch_failed = True

                if reason:
                    if launch_failed and qmp is not None:
                        try:
                            qmp.execute("stop")
                            (logs / "registers-after.txt").write_text(qmp.execute("human-monitor-command", {"command-line": "info registers"}))
                            write_json(logs / "qmp-end.json", qmp.execute("query-status"))
                            qmp.execute("screendump", {"filename": str(logs / "framebuffer.ppm")})
                        except Exception:
                            pass
                    status["process"]["phase"] = "stopping"
                    write_json(bundle / "runtime.json", status)
                    if qmp is not None:
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

                if player_process is not None and player_process.poll() is None:
                    player_process.terminate()
                    try:
                        player_process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        player_process.kill()
                        player_process.wait()

                code = process.wait() if process.poll() is None else process.returncode
                is_clean_stop = (reason == "player window closed" or (isinstance(reason, str) and "forced stop" in reason))
                status["process"].update(
                    phase="failed" if (launch_failed or (code != 0 and not is_clean_stop)) else "stopped",
                    pid=None,
                    exit_code=0 if is_clean_stop and code in (0, -15) else code,
                )
                status["shutdown"] = dict(clean=False, reason=reason or "QEMU exited; guest filesystem shutdown not verified")
                write_json(bundle / "runtime.json", status)
                print(f"QEMU exited ({code}). " + (f"Launch failed: {reason}. " if launch_failed else "") + f"Evidence: {logs}", flush=True)
                return (0 if is_clean_stop else code) or (1 if launch_failed else 0)
            except Exception as exc:
                status["process"].update(phase="failed", exit_code=process.poll() if process else None)
                status["shutdown"] = dict(clean=False, reason=str(exc))
                write_json(bundle / "runtime.json", status)
                raise
            finally:
                cleanup_processes(qemu_process=process, player_process=player_process, qmp=qmp)
                if status["process"]["phase"] == "failed":
                    status["process"].update(pid=None, exit_code=process.returncode if process else None)
                    write_json(bundle / "runtime.json", status)
                write_json(logs / "runtime-final.json", status)
                for sig, handler in old_handlers.items():
                    signal.signal(sig, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle")
    parser.add_argument("--display", choices=["player", "cocoa", "none"], default=None,
                        help="Display mode: player (native window), cocoa (QEMU window), or none (headless)")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--probe-seconds", type=float)
    parser.add_argument("--paused", action="store_true")
    parser.add_argument("--wifi", action="store_true", help="Expose host-mirrored Wi-Fi scan list backed by user-mode NAT")
    args = parser.parse_args()
    if args.probe_seconds is not None and not 0 < args.probe_seconds <= 30:
        parser.error("probe-seconds must be greater than 0 and at most 30")
    try:
        sys.exit(launch(args.bundle, args.headless, args.probe_seconds, args.paused, args.wifi, display=args.display))
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Launch error: {exc}\n")


if __name__ == "__main__":
    main()
