"""Helper functions and monitoring for bundle launch with player or cocoa displays."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys


def get_repo_root() -> Path:
    """Return the repository root, honoring VPHONE_LEGACY_ROOT if set."""
    env_root = os.environ.get("VPHONE_LEGACY_ROOT")
    if env_root:
        return Path(env_root).resolve()
    return Path(__file__).resolve().parents[2]


def resolve_display_mode(manifest: dict, display_arg: str | None = None, headless: bool = False) -> str:
    """Resolve display mode.

    If headless is True, returns 'none'.
    If display_arg is explicitly given, returns the validated mode ('player', 'cocoa', 'none').
    Otherwise, defaults to 'player' for 'profile: ui' bundles and 'cocoa' for baseline.
    """
    if headless:
        return "none"
    if display_arg is not None:
        mode = display_arg.lower()
        if mode not in ("player", "cocoa", "none"):
            raise ValueError(f"Invalid display mode: {display_arg}. Must be one of: player, cocoa, none")
        return mode
    profile = manifest.get("profile", "baseline")
    if profile == "ui":
        return "player"
    return "cocoa"


def build_qemu_command(
    backend_binary: str | Path,
    machine: str,
    accelerator: str,
    cpu_count: int,
    kernel_image: str | Path,
    display_mode: str,
    qmp_path: str | Path,
    serial_log: str | Path,
    trace_log: str | Path,
    trace_mask: str = "guest_errors",
    vnc_path: str | Path | None = None,
    auxiliary_images: list[dict] | None = None,
    paused: bool = False,
    probe_seconds: float | None = None,
    data_dir: str | Path | None = None,
) -> list[str]:
    """Pure function constructing QEMU command line arguments."""
    cmd = [
        str(backend_binary),
        "-machine", machine,
        "-accel", accelerator,
        "-smp", str(cpu_count),
        "-m", "1024M",
        "-kernel", str(kernel_image),
    ]
    if display_mode == "player":
        if not vnc_path:
            raise ValueError("vnc_path is required for player display mode")
        cmd += ["-display", "none", "-vnc", f"unix:{vnc_path}"]
        # VNC needs QEMU's keymap files (pc-bios/keymaps); without -L it exits at startup.
        bios = Path(data_dir) if data_dir else get_repo_root() / "backend/upstream/pc-bios"
        if bios.is_dir():
            cmd += ["-L", str(bios)]
    elif display_mode == "cocoa":
        cmd += ["-display", "cocoa"]
    elif display_mode == "none":
        cmd += ["-display", "none"]
    else:
        raise ValueError(f"Unknown display mode: {display_mode}")

    cmd += [
        "-qmp", f"unix:{qmp_path},server=on,wait=off",
        "-monitor", "none",
        "-chardev", f"stdio,id=console,signal=off,logfile={serial_log}",
        "-serial", "chardev:console",
        "-d", trace_mask,
        "-D", str(trace_log),
    ]
    for image in (auxiliary_images or []):
        cmd += ["-device", f"loader,file={image['path']},addr={image['address']:#x},force-raw=on"]
    if probe_seconds is not None or paused:
        cmd.append("-S")
    return cmd


def resolve_player_binary(player_path: str | Path) -> Path:
    """Resolve path to executable inside .app bundle if necessary."""
    p = Path(player_path).resolve()
    if p.is_dir() and (p / "Contents/MacOS/vphone-player").is_file():
        return p / "Contents/MacOS/vphone-player"
    return p


def build_player_command(player_binary: str | Path, vnc_path: str | Path, qmp_path: str | Path) -> list[str]:
    """Pure function constructing vphone-player execution command."""
    binary = resolve_player_binary(player_binary)
    return [str(binary), str(vnc_path), str(qmp_path)]


def classify_serial_fallback(text: str) -> str:
    """Classify boot stage from guest serial log using built-in heuristics."""
    if not text:
        return "none"

    # Panic header patterns matching PanicMonitor
    if re.search(r"(?m)^[\r ]*panic\(cpu [0-9]+ caller 0x[0-9a-fA-F]+\):", text) or \
       re.search(r"panic\(cpu [0-9]+ caller 0x[0-9a-fA-F]+\):[^\n]*\n[\r ]*Debugger message: panic", text):
        return "panic"

    lower = text.lower()
    # Check progressive stages from latest to earliest
    if "homescreen" in lower or "sbhomescreen" in lower or "home screen" in lower:
        return "home"
    if ("lockscreen" in lower or "sblockscreen" in lower or
        "assistantpresented" in lower or "_sbupdatebuddymode" in lower or
        "springboard" in lower):
        return "lockscreen"
    if ("launchd" in lower or "systembag loaded" in lower or
        "/sbin/launchd" in lower or "entering subsystem: launchd" in lower):
        return "userspace"
    if ("darwin kernel version" in lower or "xnu-" in lower or
        "kernel_bootstrap" in lower or "rooting via boot-uuid" in lower or
        "releasetype:" in lower):
        return "kernel"

    return "none"


class BootStageMonitor:
    """Lightweight monitor tracking boot stage progression from serial.log."""
    STAGE_ORDER = {"none": 0, "kernel": 1, "userspace": 2, "lockscreen": 3, "home": 4, "panic": 5}

    def __init__(self, classifier=None):
        self.stage = "none"
        self._classifier = classifier
        if self._classifier is None:
            try:
                from qemu.boot_check import classify_serial
                self._classifier = classify_serial
            except ImportError:
                try:
                    from boot_check import classify_serial
                    self._classifier = classify_serial
                except ImportError:
                    self._classifier = None

    def poll(self, serial_log_path: Path) -> str:
        path = Path(serial_log_path)
        if not path.is_file():
            return self.stage

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return self.stage

        detected = None
        if self._classifier is not None:
            try:
                detected = self._classifier(text)
            except Exception:
                detected = None

        if not detected or detected == "none":
            detected = classify_serial_fallback(text)

        if detected == "panic":
            self.stage = "panic"
        elif self.STAGE_ORDER.get(detected, 0) > self.STAGE_ORDER.get(self.stage, 0):
            self.stage = detected

        return self.stage


def build_initial_runtime_status(session_id: str, logs_dir: Path, display_mode: str, capabilities: dict | None = None) -> dict:
    """Pure function generating initial runtime.json dictionary."""
    stages = {name: dict(phase="unverified", evidence=None) for name in ("kernel", "userspace", "agent")}
    return {
        "schema_version": 1,
        "session_id": session_id,
        "process": {"phase": "starting", "pid": None, "exit_code": None},
        "shutdown": {"clean": None, "reason": None},
        "capabilities": capabilities or {},
        "log_directory": str(logs_dir),
        "boot_stage": "none",
        "display": display_mode,
        **stages,
    }


def handle_stop_request(bundle_dir: Path, session_id: str, qmp=None, guest_shutdown_capable: bool = False) -> tuple[bool, str | None, bool]:
    """Check and process stop-request.json in bundle.

    Returns:
        (stop_requested: bool, reason: str | None, is_stopping: bool)
    """
    request_path = Path(bundle_dir) / "stop-request.json"
    if not request_path.is_file():
        return False, None, False

    try:
        request = json.loads(request_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False, None, False

    if request.get("session_id") != session_id:
        return False, None, False

    request_path.unlink(missing_ok=True)
    if request.get("force"):
        return True, "explicit forced stop", False

    if guest_shutdown_capable and qmp is not None:
        try:
            qmp.execute("system_powerdown")
        except Exception:
            pass
        return False, None, True

    return True, "guest shutdown requested; completion unverified", False


def cleanup_processes(qemu_process=None, player_process=None, qmp=None, timeout_term: float = 3.0, timeout_kill: float = 2.0):
    """Safely terminate QEMU and player processes in strict order, leaving no orphans."""
    if qmp is not None:
        try:
            qmp.close()
        except Exception:
            pass

    # 1. Player process cleanup
    if player_process is not None and player_process.poll() is None:
        try:
            player_process.terminate()
            player_process.wait(timeout=timeout_term)
        except (subprocess.TimeoutExpired, OSError):
            try:
                player_process.kill()
                player_process.wait(timeout=timeout_kill)
            except Exception:
                pass

    # 2. QEMU process cleanup
    if qemu_process is not None and qemu_process.poll() is None:
        try:
            if hasattr(os, "killpg") and hasattr(qemu_process, "pid"):
                try:
                    os.killpg(qemu_process.pid, signal.SIGTERM)
                except OSError:
                    qemu_process.terminate()
            else:
                qemu_process.terminate()
            qemu_process.wait(timeout=timeout_term)
        except (subprocess.TimeoutExpired, OSError):
            try:
                if hasattr(os, "killpg") and hasattr(qemu_process, "pid"):
                    try:
                        os.killpg(qemu_process.pid, signal.SIGKILL)
                    except OSError:
                        qemu_process.kill()
                else:
                    qemu_process.kill()
                qemu_process.wait(timeout=timeout_kill)
            except Exception:
                pass
