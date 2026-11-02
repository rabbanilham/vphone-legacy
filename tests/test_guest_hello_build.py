#!/usr/bin/env python3
"""
test_guest_hello_build.py — Tests for iOS 7 Hello guest application metadata,
toolchain build, Mach-O architecture/load commands, and IPA packaging.
"""

import os
import plistlib
import shutil
import struct
import subprocess
import unittest
import zipfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUEST_DIR = os.path.join(PROJECT_ROOT, "sources", "vphone-guest")
BUILD_DIR = os.path.join(PROJECT_ROOT, "build", "sideload")


def is_ios_toolchain_available() -> bool:
    """Check if xcrun iphoneos SDK and ldid are present on the host."""
    try:
        sdk = subprocess.check_output(
            ["xcrun", "--sdk", "iphoneos", "--show-sdk-path"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        has_ldid = bool(shutil.which("ldid") or os.path.exists("/opt/homebrew/bin/ldid"))
        return bool(sdk and os.path.isdir(sdk) and has_ldid)
    except Exception:
        return False


def parse_macho_headers(data: bytes):
    """Parse thin Mach-O header and basic load commands."""
    if len(data) < 32:
        raise ValueError("Data too short for Mach-O")

    magic, cputype, cpusubtype, filetype, ncmds, sizeofcmds, flags, reserved = struct.unpack_from("<8I", data, 0)
    cursor = 32
    commands = []
    dylibs = []
    minos = None
    has_codesig = False

    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from("<II", data, cursor)
        commands.append((cmd, cmdsize))
        if cmd == 0x25:  # LC_VERSION_MIN_IPHONEOS
            v, sdk = struct.unpack_from("<II", data, cursor + 8)
            minos = f"{(v >> 16)}.{(v >> 8) & 0xff}.{v & 0xff}"
        elif cmd == 0x32:  # LC_BUILD_VERSION
            platform, minos_v, sdk_v, ntools = struct.unpack_from("<IIII", data, cursor + 8)
            minos = f"{(minos_v >> 16)}.{(minos_v >> 8) & 0xff}.{minos_v & 0xff}"
        elif cmd == 0x1d:  # LC_CODE_SIGNATURE
            has_codesig = True
        elif cmd == 0x0c:  # LC_LOAD_DYLIB
            stroff = struct.unpack_from("<I", data, cursor + 8)[0]
            name = data[cursor + stroff:cursor + cmdsize].split(b"\0")[0].decode("utf-8")
            dylibs.append(name)
        cursor += cmdsize

    return {
        "magic": magic,
        "cputype": cputype,
        "cpusubtype": cpusubtype,
        "ncmds": ncmds,
        "minos": minos,
        "has_codesig": has_codesig,
        "dylibs": dylibs,
    }


class TestHelloStaticMetadata(unittest.TestCase):
    """Verify static metadata and source files without requiring iOS toolchain."""

    def test_sources_and_scripts_exist(self):
        expected_files = [
            os.path.join(GUEST_DIR, "Info.plist"),
            os.path.join(GUEST_DIR, "entitlements.plist"),
            os.path.join(GUEST_DIR, "generate_icons.py"),
            os.path.join(GUEST_DIR, "Makefile"),
            os.path.join(GUEST_DIR, "src", "main.m"),
            os.path.join(GUEST_DIR, "src", "AppDelegate.h"),
            os.path.join(GUEST_DIR, "src", "AppDelegate.m"),
            os.path.join(GUEST_DIR, "src", "ViewController.h"),
            os.path.join(GUEST_DIR, "src", "ViewController.m"),
            os.path.join(PROJECT_ROOT, "scripts", "build_hello_ipa.sh"),
        ]
        for f in expected_files:
            self.assertTrue(os.path.isfile(f), f"Expected file missing: {f}")

    def test_info_plist_validity(self):
        plist_path = os.path.join(GUEST_DIR, "Info.plist")
        with open(plist_path, "rb") as fp:
            plist = plistlib.load(fp)

        self.assertEqual(plist.get("CFBundleIdentifier"), "com.vphone.hello")
        self.assertEqual(plist.get("CFBundleExecutable"), "Hello")
        self.assertIn("CFBundleVersion", plist)
        self.assertIn("CFBundleShortVersionString", plist)
        self.assertEqual(plist.get("UIDeviceFamily"), [1])
        self.assertEqual(plist.get("MinimumOSVersion"), "7.0")
        self.assertIn("arm64", plist.get("UIRequiredDeviceCapabilities", []))
        self.assertTrue(plist.get("UISupportedInterfaceOrientations"))
        self.assertIn("UIInterfaceOrientationPortrait", plist.get("UISupportedInterfaceOrientations"))
        self.assertIn("UIStatusBarStyle", plist)

    def test_entitlements_validity(self):
        ent_path = os.path.join(GUEST_DIR, "entitlements.plist")
        with open(ent_path, "rb") as fp:
            ent = plistlib.load(fp)

        self.assertEqual(ent.get("application-identifier"), "com.vphone.hello")


class TestHelloToolchainAndPackaging(unittest.TestCase):
    """Verify compilation, Mach-O slices, signature, and IPA packaging."""

    @unittest.skipUnless(is_ios_toolchain_available(), "xcrun iphoneos SDK and ldid required")
    def test_build_and_validate_ipa(self):
        # Run build script
        script_path = os.path.join(PROJECT_ROOT, "scripts", "build_hello_ipa.sh")
        proc = subprocess.run([script_path], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"build_hello_ipa.sh failed: {proc.stderr}")

        ipa_path = os.path.join(BUILD_DIR, "Hello.ipa")
        self.assertTrue(os.path.isfile(ipa_path), f"Missing IPA at {ipa_path}")

        # Check IPA archive contents
        with zipfile.ZipFile(ipa_path, "r") as zf:
            names = zf.namelist()
            self.assertIn("Payload/Hello.app/Hello", names)
            self.assertIn("Payload/Hello.app/Info.plist", names)
            self.assertIn("Payload/Hello.app/AppIcon60x60@2x.png", names)
            self.assertIn("Payload/Hello.app/AppIcon57x57.png", names)

            # Check binary Mach-O structure
            binary_data = zf.read("Payload/Hello.app/Hello")
            info = parse_macho_headers(binary_data)

            # 0xfeedfacf is MH_MAGIC_64, 0x100000c is CPU_TYPE_ARM64
            self.assertEqual(info["magic"], 0xfeedfacf, "Expected 64-bit Mach-O magic")
            self.assertEqual(info["cputype"], 0x100000c, "Expected ARM64 cputype")
            self.assertTrue(
                info["minos"] and info["minos"].startswith("7.0"),
                f"Expected deployment target 7.0, got {info['minos']}",
            )
            self.assertTrue(info["has_codesig"], "Expected LC_CODE_SIGNATURE in binary")

            # Check linked dylibs — must only target frameworks available on iOS 7
            allowed_prefixes = (
                "/System/Library/Frameworks/UIKit.framework/",
                "/System/Library/Frameworks/Foundation.framework/",
                "/System/Library/Frameworks/CoreGraphics.framework/",
                "/System/Library/Frameworks/QuartzCore.framework/",
                "/System/Library/Frameworks/CoreFoundation.framework/",
                "/usr/lib/libSystem",
                "/usr/lib/libobjc",
            )
            for dylib in info["dylibs"]:
                self.assertTrue(
                    any(dylib.startswith(prefix) for prefix in allowed_prefixes),
                    f"Unexpected modern/unsupported dylib linked: {dylib}",
                )

    @unittest.skipUnless(is_ios_toolchain_available(), "xcrun iphoneos SDK required")
    def test_alternative_signing_with_diagnostic_probe_signer(self):
        """Verify sign_diagnostic_probe.py can sign the thin ARM64 binary as an alternative."""
        import sys
        sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))
        from patchers.sign_diagnostic_probe import sign_probe

        sdk = subprocess.check_output(
            ["xcrun", "--sdk", "iphoneos", "--show-sdk-path"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()

        # Compile unsigned arm64 binary
        tmp_bin = os.path.join(BUILD_DIR, "Hello_unsigned")
        os.makedirs(BUILD_DIR, exist_ok=True)
        cmd = [
            "xcrun", "clang",
            "-target", "arm64-apple-ios7.0",
            "-isysroot", sdk,
            "-fno-objc-arc",
            "-Wall", "-O2",
            "-framework", "UIKit",
            "-framework", "Foundation",
            "-framework", "CoreGraphics",
            "-framework", "QuartzCore",
            "-o", tmp_bin,
            os.path.join(GUEST_DIR, "src", "main.m"),
            os.path.join(GUEST_DIR, "src", "AppDelegate.m"),
            os.path.join(GUEST_DIR, "src", "ViewController.m"),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Compilation failed: {res.stderr}")

        with open(tmp_bin, "rb") as fp:
            unsigned_data = fp.read()

        # Verify unsigned before sign_probe
        info_before = parse_macho_headers(unsigned_data)
        self.assertFalse(info_before["has_codesig"])

        # Sign using sign_probe
        signed_data = sign_probe(unsigned_data)
        info_after = parse_macho_headers(signed_data)
        self.assertTrue(info_after["has_codesig"])
        self.assertEqual(info_after["cputype"], 0x100000c)


if __name__ == "__main__":
    unittest.main()
