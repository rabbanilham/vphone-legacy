"""Unit tests for bundle launch with player mode, lifecycle management, and boot stages."""
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

import sys
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from qemu.launch_player import (
    get_repo_root,
    resolve_display_mode,
    build_qemu_command,
    resolve_player_binary,
    build_player_command,
    classify_serial_fallback,
    BootStageMonitor,
    build_initial_runtime_status,
    handle_stop_request,
    cleanup_processes,
)


class TestDisplayModeResolution(unittest.TestCase):
    def test_default_display_from_manifest_profile(self):
        # Profile: ui -> player
        self.assertEqual(resolve_display_mode({"profile": "ui"}), "player")
        # Profile: baseline -> cocoa
        self.assertEqual(resolve_display_mode({"profile": "baseline"}), "cocoa")
        # Missing profile -> cocoa
        self.assertEqual(resolve_display_mode({}), "cocoa")

    def test_explicit_display_override(self):
        manifest_ui = {"profile": "ui"}
        manifest_base = {"profile": "baseline"}

        self.assertEqual(resolve_display_mode(manifest_ui, display_arg="cocoa"), "cocoa")
        self.assertEqual(resolve_display_mode(manifest_base, display_arg="player"), "player")
        self.assertEqual(resolve_display_mode(manifest_ui, display_arg="none"), "none")
        self.assertEqual(resolve_display_mode(manifest_ui, display_arg="PLAYER"), "player")

    def test_headless_flag_forces_none(self):
        self.assertEqual(resolve_display_mode({"profile": "ui"}, headless=True), "none")
        self.assertEqual(resolve_display_mode({"profile": "baseline"}, display_arg="player", headless=True), "none")

    def test_invalid_display_raises_value_error(self):
        with self.assertRaises(ValueError):
            resolve_display_mode({"profile": "ui"}, display_arg="invalid_mode")


class TestCommandBuilders(unittest.TestCase):
    def setUp(self):
        self.qemu_bin = "/path/to/qemu-system-aarch64"
        self.machine = "s5l8960x,boot-el=3"
        self.kernel = "/path/to/kernel.bin"
        self.qmp = "/tmp/vpl-test/qmp.sock"
        self.vnc = "/tmp/vpl-test/vnc.sock"
        self.serial = "/path/to/logs/serial.log"
        self.trace = "/path/to/logs/trace.log"

    def test_qemu_command_player_mode(self):
        cmd = build_qemu_command(
            backend_binary=self.qemu_bin,
            machine=self.machine,
            accelerator="tcg,thread=multi",
            cpu_count=2,
            kernel_image=self.kernel,
            display_mode="player",
            qmp_path=self.qmp,
            vnc_path=self.vnc,
            serial_log=self.serial,
            trace_log=self.trace,
        )

        # Must have display none and vnc socket
        self.assertIn("-display", cmd)
        disp_idx = cmd.index("-display")
        self.assertEqual(cmd[disp_idx + 1], "none")

        self.assertIn("-vnc", cmd)
        vnc_idx = cmd.index("-vnc")
        self.assertEqual(cmd[vnc_idx + 1], f"unix:{self.vnc}")

        # Must have qmp socket
        self.assertIn("-qmp", cmd)
        qmp_idx = cmd.index("-qmp")
        self.assertEqual(cmd[qmp_idx + 1], f"unix:{self.qmp},server=on,wait=off")

        # Must NOT have cocoa display
        self.assertNotIn("cocoa", cmd)

    def test_player_mode_passes_qemu_data_dir_for_vnc_keymaps(self):
        with tempfile.TemporaryDirectory() as bios:
            cmd = build_qemu_command(
                backend_binary=self.qemu_bin, machine=self.machine, accelerator="tcg,thread=multi",
                cpu_count=2, kernel_image=self.kernel, display_mode="player", qmp_path=self.qmp,
                vnc_path=self.vnc, serial_log=self.serial, trace_log=self.trace, data_dir=bios)
            self.assertEqual(cmd[cmd.index("-L") + 1], bios)
            cocoa = build_qemu_command(
                backend_binary=self.qemu_bin, machine=self.machine, accelerator="tcg,thread=multi",
                cpu_count=2, kernel_image=self.kernel, display_mode="cocoa", qmp_path=self.qmp,
                serial_log=self.serial, trace_log=self.trace, data_dir=bios)
            self.assertNotIn("-L", cocoa)

    def test_qemu_command_player_mode_requires_vnc_path(self):
        with self.assertRaises(ValueError):
            build_qemu_command(
                backend_binary=self.qemu_bin,
                machine=self.machine,
                accelerator="tcg,thread=multi",
                cpu_count=2,
                kernel_image=self.kernel,
                display_mode="player",
                qmp_path=self.qmp,
                vnc_path=None,
                serial_log=self.serial,
                trace_log=self.trace,
            )

    def test_qemu_command_cocoa_mode(self):
        cmd = build_qemu_command(
            backend_binary=self.qemu_bin,
            machine=self.machine,
            accelerator="tcg,thread=multi",
            cpu_count=2,
            kernel_image=self.kernel,
            display_mode="cocoa",
            qmp_path=self.qmp,
            serial_log=self.serial,
            trace_log=self.trace,
        )
        self.assertIn("-display", cmd)
        disp_idx = cmd.index("-display")
        self.assertEqual(cmd[disp_idx + 1], "cocoa")
        self.assertNotIn("-vnc", cmd)

    def test_qemu_command_none_mode(self):
        cmd = build_qemu_command(
            backend_binary=self.qemu_bin,
            machine=self.machine,
            accelerator="tcg,thread=multi",
            cpu_count=2,
            kernel_image=self.kernel,
            display_mode="none",
            qmp_path=self.qmp,
            serial_log=self.serial,
            trace_log=self.trace,
        )
        self.assertIn("-display", cmd)
        disp_idx = cmd.index("-display")
        self.assertEqual(cmd[disp_idx + 1], "none")
        self.assertNotIn("-vnc", cmd)

    def test_player_command_builder(self):
        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td) / "vphone-player.app"
            macos_dir = app_dir / "Contents" / "MacOS"
            macos_dir.mkdir(parents=True)
            bin_path = macos_dir / "vphone-player"
            bin_path.write_text("#!/bin/sh\n")

            # Resolves from .app directory
            cmd = build_player_command(app_dir, self.vnc, self.qmp)
            self.assertEqual(cmd, [str(bin_path.resolve()), str(self.vnc), str(self.qmp)])

            # Direct binary path
            cmd_direct = build_player_command(bin_path, self.vnc, self.qmp)
            self.assertEqual(cmd_direct, [str(bin_path.resolve()), str(self.vnc), str(self.qmp)])


class TestStopRequest(unittest.TestCase):
    def test_no_stop_request(self):
        with tempfile.TemporaryDirectory() as td:
            stop_req, reason, is_stopping = handle_stop_request(Path(td), "session-123")
            self.assertFalse(stop_req)
            self.assertIsNone(reason)
            self.assertFalse(is_stopping)

    def test_mismatched_session_id_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            req_file = Path(td) / "stop-request.json"
            req_file.write_text(json.dumps({"session_id": "other-session", "force": True}))

            stop_req, reason, is_stopping = handle_stop_request(Path(td), "session-123")
            self.assertFalse(stop_req)
            self.assertIsNone(reason)
            self.assertTrue(req_file.exists())

    def test_force_stop_request(self):
        with tempfile.TemporaryDirectory() as td:
            req_file = Path(td) / "stop-request.json"
            req_file.write_text(json.dumps({"session_id": "session-123", "force": True}))

            stop_req, reason, is_stopping = handle_stop_request(Path(td), "session-123")
            self.assertTrue(stop_req)
            self.assertEqual(reason, "explicit forced stop")
            self.assertFalse(is_stopping)
            self.assertFalse(req_file.exists())

    def test_clean_stop_with_guest_shutdown_capability(self):
        with tempfile.TemporaryDirectory() as td:
            req_file = Path(td) / "stop-request.json"
            req_file.write_text(json.dumps({"session_id": "session-123", "force": False}))

            mock_qmp = MagicMock()
            stop_req, reason, is_stopping = handle_stop_request(
                Path(td), "session-123", qmp=mock_qmp, guest_shutdown_capable=True
            )
            self.assertFalse(stop_req)
            self.assertIsNone(reason)
            self.assertTrue(is_stopping)
            self.assertFalse(req_file.exists())
            mock_qmp.execute.assert_called_once_with("system_powerdown")


class TestProcessCleanup(unittest.TestCase):
    def test_cleanup_sequence_order(self):
        mock_qmp = MagicMock()
        mock_player = MagicMock()
        mock_player.poll.side_effect = [None, 0]  # Initially running, then exited after terminate
        mock_qemu = MagicMock()
        mock_qemu.poll.side_effect = [None, 0]

        cleanup_processes(qemu_process=mock_qemu, player_process=mock_player, qmp=mock_qmp)

        mock_qmp.close.assert_called_once()
        mock_player.terminate.assert_called_once()
        mock_player.wait.assert_called_once()
        mock_qemu.terminate.assert_called_once()
        mock_qemu.wait.assert_called_once()

    def test_cleanup_escalates_to_kill_on_timeout(self):
        mock_player = MagicMock()
        mock_player.poll.return_value = None
        mock_player.wait.side_effect = subprocess.TimeoutExpired(cmd="player", timeout=1)

        mock_qemu = MagicMock()
        mock_qemu.poll.return_value = None
        mock_qemu.wait.side_effect = subprocess.TimeoutExpired(cmd="qemu", timeout=1)

        cleanup_processes(qemu_process=mock_qemu, player_process=mock_player, timeout_term=0.01, timeout_kill=0.01)

        mock_player.terminate.assert_called_once()
        mock_player.kill.assert_called_once()
        mock_qemu.terminate.assert_called_once()
        mock_qemu.kill.assert_called_once()


class TestRuntimeStatusAndContract(unittest.TestCase):
    def test_initial_runtime_status_fields(self):
        logs = Path("/tmp/logs/test")
        status = build_initial_runtime_status("test-session-id", logs, "player")

        self.assertEqual(status["schema_version"], 1)
        self.assertEqual(status["session_id"], "test-session-id")
        self.assertEqual(status["display"], "player")
        self.assertEqual(status["boot_stage"], "none")
        self.assertEqual(status["process"]["phase"], "starting")
        self.assertIsNone(status["process"]["pid"])
        self.assertIsNone(status["process"]["exit_code"])
        self.assertIsNone(status["shutdown"]["clean"])
        self.assertEqual(status["log_directory"], str(logs))
        self.assertIn("kernel", status)
        self.assertIn("userspace", status)
        self.assertIn("agent", status)

    def test_runtime_status_matches_gui_contract_fixture(self):
        fixture_path = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "gui" / "vm-status.json"
        fixture = json.loads(fixture_path.read_text())

        status = build_initial_runtime_status("7e0b", Path("logs/20261102-0001"), "player")
        status["control"] = {"qmp": "/tmp/vpl-7e0b/qmp"}
        status["boot_stage"] = "home"
        status["process"].update(phase="running", pid=4242)
        status["shutdown"].update(clean=False)

        # Check required keys from fixture
        for key in ("session_id", "process", "shutdown", "log_directory", "control", "boot_stage", "display"):
            self.assertIn(key, status)
            if isinstance(fixture[key], dict):
                for subkey in fixture[key]:
                    self.assertIn(subkey, status[key])


class TestBootStageMonitor(unittest.TestCase):
    def test_stage_progression_from_serial(self):
        with tempfile.TemporaryDirectory() as td:
            serial_log = Path(td) / "serial.log"
            monitor = BootStageMonitor()

            self.assertEqual(monitor.poll(serial_log), "none")

            serial_log.write_text("Darwin Kernel Version 14.0.0\n")
            self.assertEqual(monitor.poll(serial_log), "kernel")

            serial_log.write_text("Darwin Kernel Version 14.0.0\nRunning /sbin/launchd\n")
            self.assertEqual(monitor.poll(serial_log), "userspace")

            serial_log.write_text("Darwin Kernel Version 14.0.0\nRunning /sbin/launchd\nSpringBoard starting up\n")
            self.assertEqual(monitor.poll(serial_log), "lockscreen")

            serial_log.write_text("Darwin Kernel Version 14.0.0\nRunning /sbin/launchd\nSpringBoard starting up\nHomeScreen loaded\n")
            self.assertEqual(monitor.poll(serial_log), "home")

            # Panic overrides everything
            serial_log.write_text("panic(cpu 0 caller 0xffffff800022194c): Unexpected abort\n")
            self.assertEqual(monitor.poll(serial_log), "panic")

    def test_classifier_fallback_does_not_regress(self):
        with tempfile.TemporaryDirectory() as td:
            serial_log = Path(td) / "serial.log"
            monitor = BootStageMonitor()

            serial_log.write_text("Running /sbin/launchd\n")
            self.assertEqual(monitor.poll(serial_log), "userspace")

            # A subsequent read with fewer lines or only kernel text must not regress userspace to kernel
            serial_log.write_text("Darwin Kernel Version 14.0.0\n")
            self.assertEqual(monitor.poll(serial_log), "userspace")

    def test_custom_classifier_hook_respected(self):
        with tempfile.TemporaryDirectory() as td:
            serial_log = Path(td) / "serial.log"
            serial_log.write_text("custom boot line\n")

            mock_classifier = MagicMock(return_value="lockscreen")
            monitor = BootStageMonitor(classifier=mock_classifier)
            self.assertEqual(monitor.poll(serial_log), "lockscreen")
            mock_classifier.assert_called_once()


class TestRepoRoot(unittest.TestCase):
    def test_vphone_legacy_root_override(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"VPHONE_LEGACY_ROOT": td}):
                self.assertEqual(get_repo_root(), Path(td).resolve())


if __name__ == "__main__":
    unittest.main()
