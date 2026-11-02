# Guest Application Sideloading and Toolchain

This guide details the guest toolchain and packaging pipeline for sideloading third-party and custom applications into the iOS 7.1.2 (iPhone 5s / A7 / `iPhone6,2`) emulator.

## Overview

The sideloading roadmap follows an offline-first strategy: injecting an application bundle directly into disk images while the VM is stopped, before introducing live installation over the `vphoned` guest agent. The initial milestone is a minimal self-contained Objective-C UIKit application (`Hello.app` / `Hello.ipa`) located at `sources/vphone-guest/`.

## Guest Application: Hello

The Hello app serves as a verification target for runtime execution, UI rendering, and disk persistence.

- **Bundle ID**: `com.vphone.hello`
- **Source Directory**: `sources/vphone-guest/`
  - `src/main.m`: Standard application entrypoint.
  - `src/AppDelegate.h`, `src/AppDelegate.m`: Sets up the main window and records execution evidence:
    - Writes an ISO timestamp, PID, and main bundle path to `Documents/hello.log`.
    - Emits a diagnostic message to `NSLog`.
  - `src/ViewController.h`, `src/ViewController.m`: Displays a centered `UILabel` with `"Hello from vphone"` on a high-contrast background with rounded corners.
  - `Info.plist`: Valid iOS 7 bundle configuration (`MinimumOSVersion` 7.0, `UIDeviceFamily` [1], `UIRequiredDeviceCapabilities` [arm64], orientations, status bar style, and icon definitions).
  - `entitlements.plist`: Declares `application-identifier` (`com.vphone.hello`).
  - `generate_icons.py`: PIL script generating non-copyrighted 120x120 (`AppIcon60x60@2x.png` / `Icon-60@2x.png`) and 57x57 (`AppIcon57x57.png` / `Icon.png`) icons.

## Toolchain & Compiler Findings

Targeting iOS 7.1.2 on modern macOS host toolchains (e.g. Apple Clang with Xcode 15+ / iOS 17+ SDKs) presents specific constraints:

### ARC vs MRC (`-fno-objc-arc`)

Modern Xcode toolchains dropped `libarclite_iphoneos.a`. Compiling Objective-C code with `-fobjc-arc` targeting `arm64-apple-ios7.0` fails during linking:
```
clang: error: SDK does not contain 'libarclite' at the path '.../usr/lib/arc/libarclite_iphoneos.a'; try increasing the minimum deployment target
```
**Resolution**: The guest app uses Manual Retain/Release (MRC, `-fno-objc-arc`). This compiles cleanly, introduces no runtime dependency on missing static runtime libraries, and prevents unresolved symbol lookups in the iOS 7.1.2 dyld shared cache.

### Load Commands and Deployment Target

When compiling with:
```bash
xcrun clang -target arm64-apple-ios7.0 -isysroot "$(xcrun --sdk iphoneos --show-sdk-path)" -fno-objc-arc ...
```
Apple Clang generates a Mach-O binary containing:
- Mach-O 64-bit architecture slice: `arm64` (`0x100000c`).
- Legacy load command: `LC_VERSION_MIN_IPHONEOS` with `version 7.0` (rather than modern `LC_BUILD_VERSION`). This is directly compatible with the iOS 7.1.2 dynamic linker (`dyld`).
- Framework links restricted to system libraries available on iOS 7.1.2:
  - `/System/Library/Frameworks/UIKit.framework/UIKit`
  - `/System/Library/Frameworks/Foundation.framework/Foundation`
  - `/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics`
  - `/System/Library/Frameworks/QuartzCore.framework/QuartzCore`
  - `/usr/lib/libSystem.B.dylib`
  - `/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation`
  - `/usr/lib/libobjc.A.dylib`

Linker flag `-Wl,-ld_classic` is deprecated and ignored by modern `ld-prime`; explicit target flag `-target arm64-apple-ios7.0` is sufficient.

## Ad-hoc Code Signing Comparison

Guest execution under diagnostic profiles uses relaxed AMFI boot-args (`amfi_allow_any_signature=1 amfi_get_out_of_my_way=1 cs_enforcement_disable=1`), but binaries must still contain a valid `LC_CODE_SIGNATURE` structure.

| Feature | `ldid -S` | `scripts/patchers/sign_diagnostic_probe.py` |
| :--- | :--- | :--- |
| **Tool Type** | External CLI binary (`/opt/homebrew/bin/ldid`) | In-tree pure Python script |
| **Entitlements** | Custom XML file via `-S<file>` | Hardcoded (`com.apple.private.lockdown.finegrained-get`) |
| **Identifier** | Dynamically derived or specified | Hardcoded (`org.vphone.virtual-activation-probe`) |
| **Mach-O Support** | Fat and thin Mach-O binaries; shifts headers dynamically | Thin ARM64 Mach-O only; requires spare header space |
| **Usage** | Default signer for `build_hello_ipa.sh` and `make hello` | Diagnostic probe signer for virtual activation |

## Building the IPA

To build `Hello.ipa`:
```bash
# Build via root Makefile
make hello

# Or directly via build script
./scripts/build_hello_ipa.sh

# Or via sources/vphone-guest Makefile
make -C sources/vphone-guest
```

The resulting package is written to `build/sideload/Hello.ipa`. The archive structure is:
```
Payload/
└── Hello.app/
    ├── AppIcon57x57.png
    ├── AppIcon60x60@2x.png
    ├── Hello               (arm64 executable, ad-hoc signed, minos 7.0)
    ├── Icon-60@2x.png
    ├── Icon.png
    ├── Info.plist          (CFBundleIdentifier com.vphone.hello)
    └── PkgInfo             (APPL????)
```
All macOS extended attributes (`com.apple.quarantine`, resource forks) are stripped prior to archiving to prevent corrupting guest HFS+ filesystem structures.

## Verification

Run the dedicated test suite:
```bash
.venv/bin/python -m unittest tests/test_guest_hello_build.py -v
```
This verifies:
1. Static bundle metadata in `Info.plist` and `entitlements.plist` (always runs, no toolchain required).
2. End-to-end compilation, Mach-O ARM64 header structure, `LC_VERSION_MIN_IPHONEOS 7.0`, allowed iOS 7 dylib dependencies, code signatures, and IPA packaging (skipped if iOS SDK or `ldid` is absent).
3. Alternative ad-hoc signing with `sign_diagnostic_probe.py`.

See also: [Architecture](architecture.md), [Current Status](status.md), and [Testing](testing.md).
