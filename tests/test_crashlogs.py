"""Unit tests for backend.crashlogs module.

Tests hdiutil output parsing (including partition maps), crashlog index building,
subprocess mocking, and ensuring hdiutil detach is called on failure.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from crashlogs import (
    attach_disk_image,
    build_crashlog_index,
    collect_crashlogs,
    detach_disk_image,
    extract_root_device,
    parse_hdiutil_attach_output,
    parse_process_name,
)


class TestHdiutilOutputParsing(unittest.TestCase):
    def test_parse_apple_partition_map(self):
        stdout = (
            "/dev/disk4          \tApple_partition_scheme         \t\n"
            "/dev/disk4s1        \tApple_partition_map            \t\n"
            "/dev/disk4s2        \tApple_HFS                      \t/Volumes/Data\n"
        )
        dev, mount = parse_hdiutil_attach_output(stdout)
        self.assertEqual(dev, "/dev/disk4")
        self.assertEqual(mount, Path("/Volumes/Data"))

    def test_parse_guid_partition_scheme(self):
        stdout = (
            "/dev/disk5          \tGUID_partition_scheme          \t\n"
            "/dev/disk5s1        \tApple_HFS                      \t/Volumes/TestVolume\n"
        )
        dev, mount = parse_hdiutil_attach_output(stdout)
        self.assertEqual(dev, "/dev/disk5")
        self.assertEqual(mount, Path("/Volumes/TestVolume"))

    def test_parse_flat_hfs_without_partition_map(self):
        stdout = "/dev/disk6          \tApple_HFS                      \t/Volumes/Untitled\n"
        dev, mount = parse_hdiutil_attach_output(stdout)
        self.assertEqual(dev, "/dev/disk6")
        self.assertEqual(mount, Path("/Volumes/Untitled"))

    def test_parse_spaces_in_volume_name(self):
        stdout = (
            "/dev/disk4          \tApple_partition_scheme         \t\n"
            "/dev/disk4s1        \tApple_partition_map            \t\n"
            "/dev/disk4s2        \tApple_HFS                      \t/Volumes/iOS Data Disk\n"
        )
        dev, mount = parse_hdiutil_attach_output(stdout)
        self.assertEqual(dev, "/dev/disk4")
        self.assertEqual(mount, Path("/Volumes/iOS Data Disk"))

    def test_parse_with_deprecation_warning_lines(self):
        stdout = (
            "hdiutil: WARNING: 'hdiutil attach ...' is deprecated. Please use diskutil instead.\n"
            "/dev/disk7          \tApple_partition_scheme         \t\n"
            "/dev/disk7s1        \tApple_partition_map            \t\n"
            "/dev/disk7s2        \tApple_HFS                      \t/Volumes/Data\n"
        )
        dev, mount = parse_hdiutil_attach_output(stdout)
        self.assertEqual(dev, "/dev/disk7")
        self.assertEqual(mount, Path("/Volumes/Data"))

    def test_parse_missing_hfs_volume_raises(self):
        stdout = (
            "/dev/disk4          \tApple_partition_scheme         \t\n"
            "/dev/disk4s1        \tApple_partition_map            \t\n"
        )
        with self.assertRaises(RuntimeError):
            parse_hdiutil_attach_output(stdout)

    def test_extract_root_device(self):
        stdout = (
            "warning line\n"
            "/dev/disk12s1       \tApple_HFS                      \t/Volumes/Test\n"
        )
        self.assertEqual(extract_root_device(stdout), "/dev/disk12")


class TestIndexBuilding(unittest.TestCase):
    def test_parse_process_name_json_header(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "SpringBoard_2106-02-06-222629_localhost.ips"
            p.write_text('{"bundleID":"com.apple.springboard","app_name":"SpringBoard","bug_type":"109"}\nIncident Identifier: ...\n')
            name = parse_process_name(p)
            self.assertEqual(name, "SpringBoard")

    def test_parse_process_name_text_header(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "crash_report.crash"
            p.write_text("Incident Identifier: 12345\nProcess:             backboardd [17]\nPath: /usr/libexec/backboardd\n")
            name = parse_process_name(p)
            self.assertEqual(name, "backboardd")

    def test_parse_process_name_filename_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "backboardd_2106-02-06-222643_localhost.ips"
            p.write_bytes(b"\x00\x01\x02")  # Not valid text header
            name = parse_process_name(p)
            self.assertEqual(name, "backboardd")

    def test_parse_process_name_latestcrash_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "backboardd_2106-02-06-222643_localhost.ips"
            target.write_text('{"app_name":"backboardd","bug_type":"109"}\n')
            link = Path(td) / "LatestCrash-backboardd.ips"
            link.symlink_to(target.name)
            name = parse_process_name(link)
            self.assertEqual(name, "backboardd")

    def test_build_crashlog_index_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f1 = root / "backboardd_2106-02-06-222643_localhost.ips"
            f1.write_text('{"app_name":"backboardd","bug_type":"109"}\n...')
            
            diag = root / "DiagnosticLogs"
            diag.mkdir()
            f2 = diag / "security.log.21060207T062627Z"
            f2.write_text("security log content")

            # Dummy existing index.json to ensure it is excluded
            (root / "index.json").write_text("[]")

            index = build_crashlog_index(root)
            self.assertEqual(len(index), 2)
            
            self.assertEqual(index[0]["file"], "DiagnosticLogs/security.log.21060207T062627Z")
            self.assertEqual(index[0]["process"], "security")
            self.assertEqual(index[0]["process_name"], "security")
            self.assertGreater(index[0]["size"], 0)
            self.assertIsInstance(index[0]["mtime"], float)

            self.assertEqual(index[1]["file"], "backboardd_2106-02-06-222643_localhost.ips")
            self.assertEqual(index[1]["process"], "backboardd")
            self.assertEqual(index[1]["process_name"], "backboardd")
            self.assertGreater(index[1]["size"], 0)
            self.assertIsInstance(index[1]["mtime"], float)


class TestDetachOnFailure(unittest.TestCase):
    @patch("subprocess.run")
    def test_detach_always_called_when_copy_fails(self, mock_run):
        # Set up mock attach response
        mock_attach = MagicMock(returncode=0)
        mock_attach.stdout = (
            "/dev/disk4          \tApple_partition_scheme         \t\n"
            "/dev/disk4s1        \tApple_partition_map            \t\n"
            "/dev/disk4s2        \tApple_HFS                      \t/Volumes/MockData\n"
        )
        mock_detach = MagicMock(returncode=0, stdout='"disk4" ejected.')
        mock_run.side_effect = [mock_attach, mock_detach]

        fake_path = Path("/tmp/fake_data.raw")
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "crashlogs"
            # Patch build_crashlog_index or cause an exception during collect_crashlogs
            with patch("crashlogs.build_crashlog_index", side_effect=RuntimeError("Index failed")):
                with self.assertRaises(RuntimeError):
                    collect_crashlogs(fake_path, out_dir)

        # Check detach was called on /dev/disk4
        mock_run.assert_has_calls([
            call(
                ["hdiutil", "attach", "-readonly", "-nobrowse", "-noautoopen",
                 "-imagekey", "diskimage-class=CRawDiskImage", str(fake_path.resolve())],
                capture_output=True, text=True, check=True
            ),
            call(["hdiutil", "detach", "/dev/disk4"], capture_output=True, text=True),
        ])

    @patch("subprocess.run")
    def test_detach_called_when_attach_parsing_fails(self, mock_run):
        mock_attach = MagicMock(returncode=0)
        mock_attach.stdout = (
            "/dev/disk9          \tApple_partition_scheme         \t\n"
            "/dev/disk9s1        \tApple_partition_map            \t\n"
        )  # No HFS mountpoint
        mock_detach = MagicMock(returncode=0)
        mock_run.side_effect = [mock_attach, mock_detach]

        with self.assertRaises(RuntimeError):
            attach_disk_image("/tmp/fake_data.raw")

        # Detach should have been attempted on /dev/disk9
        mock_run.assert_has_calls([
            call(
                ["hdiutil", "attach", "-readonly", "-nobrowse", "-noautoopen",
                 "-imagekey", "diskimage-class=CRawDiskImage", "/tmp/fake_data.raw"],
                capture_output=True, text=True, check=True
            ),
            call(["hdiutil", "detach", "/dev/disk9"], capture_output=True, text=True),
        ])

    @patch("subprocess.run")
    def test_detach_retries_with_force_if_normal_detach_fails(self, mock_run):
        mock_fail = MagicMock(returncode=1, stderr="Resource busy")
        mock_force = MagicMock(returncode=0, stdout='"disk4" ejected.')
        mock_run.side_effect = [mock_fail, mock_force]

        detach_disk_image("/dev/disk4")

        self.assertEqual(mock_run.call_count, 2)
        mock_run.assert_has_calls([
            call(["hdiutil", "detach", "/dev/disk4"], capture_output=True, text=True),
            call(["hdiutil", "detach", "-force", "/dev/disk4"], capture_output=True, text=True, check=True),
        ])


class TestCollectCrashlogsEndToEnd(unittest.TestCase):
    @patch("subprocess.run")
    def test_collect_crashlogs_copies_selected_files_and_writes_index(self, mock_run):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            fake_mount = tmp / "mock_mount"
            crash_src = fake_mount / "mobile/Library/Logs/CrashReporter"
            crash_src.mkdir(parents=True)

            # Files that SHOULD be copied
            f_ips = crash_src / "backboardd_2106-02-06-222643_localhost.ips"
            f_ips.write_text('{"app_name":"backboardd","bug_type":"109"}\n')

            f_crash = crash_src / "SpringBoard_2106-02-06-222629_localhost.crash"
            f_crash.write_text("Process: SpringBoard [47]\n")

            f_plist = crash_src / "sys_test.plist"
            f_plist.write_text("<plist><dict></dict></plist>")

            diag = crash_src / "DiagnosticLogs"
            diag.mkdir()
            (diag / "security.log.21060207T062627Z").write_text("log data")

            # Files that should NOT be copied
            (crash_src / "com.apple.itunesstored").mkdir()
            (crash_src / "com.apple.itunesstored/itunesstored.log").write_text("ignored")
            (crash_src / "unrelated.txt").write_text("ignored")

            # Mock attach output pointing to fake_mount
            mock_attach = MagicMock(returncode=0)
            mock_attach.stdout = (
                f"/dev/disk4          \tApple_partition_scheme         \t\n"
                f"/dev/disk4s1        \tApple_partition_map            \t\n"
                f"/dev/disk4s2        \tApple_HFS                      \t{fake_mount}\n"
            )
            mock_detach = MagicMock(returncode=0, stdout='"disk4" ejected.')
            mock_run.side_effect = [mock_attach, mock_detach]

            out_dir = tmp / "crashlogs"
            fake_data_raw = tmp / "data.raw"
            fake_data_raw.touch()

            index = collect_crashlogs(fake_data_raw, out_dir)

            # Assert attach command used readonly and rawdiskimage class
            mock_run.assert_has_calls([
                call(
                    ["hdiutil", "attach", "-readonly", "-nobrowse", "-noautoopen",
                     "-imagekey", "diskimage-class=CRawDiskImage", str(fake_data_raw.resolve())],
                    capture_output=True, text=True, check=True
                ),
                call(["hdiutil", "detach", "/dev/disk4"], capture_output=True, text=True),
            ])

            # Check copied files
            self.assertTrue((out_dir / "backboardd_2106-02-06-222643_localhost.ips").exists())
            self.assertTrue((out_dir / "SpringBoard_2106-02-06-222629_localhost.crash").exists())
            self.assertTrue((out_dir / "sys_test.plist").exists())
            self.assertTrue((out_dir / "DiagnosticLogs/security.log.21060207T062627Z").exists())

            # Check omitted files
            self.assertFalse((out_dir / "com.apple.itunesstored").exists())
            self.assertFalse((out_dir / "unrelated.txt").exists())

            # Check index.json
            index_path = out_dir / "index.json"
            self.assertTrue(index_path.exists())
            loaded_index = json.loads(index_path.read_text())
            self.assertEqual(len(loaded_index), 4)

            files_indexed = [e["file"] for e in loaded_index]
            self.assertIn("backboardd_2106-02-06-222643_localhost.ips", files_indexed)
            self.assertIn("SpringBoard_2106-02-06-222629_localhost.crash", files_indexed)
            self.assertIn("sys_test.plist", files_indexed)
            self.assertIn("DiagnosticLogs/security.log.21060207T062627Z", files_indexed)


class TestCliOption(unittest.TestCase):
    def test_display_probe_cli_accepts_collect_crashlogs(self):
        import argparse
        # Verify display_probe parser
        from display_probe import main
        # We inspect display_probe module source or invoke -h with parser
        import backend.display_probe as dp
        # Check that --collect-crashlogs is in the code
        self.assertIn("--collect-crashlogs", dp.__file__ and Path(dp.__file__).read_text())

    def test_live_run_cli_accepts_collect_crashlogs(self):
        import backend.live_run as lr
        self.assertIn("--collect-crashlogs", lr.__file__ and Path(lr.__file__).read_text())


if __name__ == "__main__":
    unittest.main()
