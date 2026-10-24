# Installation and backend build

## Host requirements

Use an Apple Silicon Mac with macOS, Xcode or Command Line Tools (`xcode-select --install`), Homebrew, Python 3.11+ (3.13 preferred), and enough free storage for QEMU sources/builds and several copies of a multi-gigabyte VM. `clang`, `xcrun`, `hdiutil`, and `diskutil` are used by the tooling. Disk preparation may mount guest filesystems locally.

```sh
brew install python@3.13 keystone glib pixman openssl@3 qemu
make setup
make test
```

`make setup` creates `.venv` in this checkout and builds a Keystone dylib against the selected macOS SDK. Run commands from the repository root. Override Python with `VPHONE_PYTHON=/path/to/python3` and the Keystone installation with `VPHONE_KEYSTONE_PREFIX=/path/to/keystone-prefix`. The latter must contain `lib/libkeystone.a`. No global Python packages are installed. Direct dependency versions in `requirements.txt` are pinned to the validated environment. Transitive dependencies and Homebrew packages are not locked; cross-version reproducibility remains a limitation.

## Project QEMU

```sh
.venv/bin/python scripts/qemu/build_backend.py --fetch --install-build-deps
.venv/bin/python backend/smoke_test.py
```

The builder verifies the exact commit in `backend/source.lock.json`, overlays the machine source and patches, and generates `backend/bin/qemu-system-aarch64` plus `backend/backend.json`. Python build tools live in `backend/.venv`. Subsequent builds can use `make backend`. Global QEMU is used only for `qemu-img`, not guest execution.

If the linker cannot read the selected SDK, choose a matching installed SDK explicitly:

```sh
.venv/bin/python scripts/qemu/build_backend.py --sdk /path/to/MacOSX.sdk
```

`backend/macos-sdk` is an ignored local alias. Do not commit SDKs or binaries. An Apple Silicon host and Cocoa display are the supported configuration; the builder disables HVF and uses TCG.

## Optional GUI

`make gui` builds and opens the SwiftUI manager using the selected Xcode tools. The manager wraps VM management; it does not materialize the later research UI profile. The CLI and backend are the primary documented development workflow.

Continue with [firmware preparation](firmware.md) or [troubleshooting](troubleshooting.md).
