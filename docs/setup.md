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


## Interactive player (local research profile)

With the prepared `build/virtual-activated-profile-v5` artifacts available:

```sh
.venv/bin/python scripts/qemu/build_backend.py
.venv/bin/python -u backend/live_run.py
```

The launcher builds the native macOS player on first use and whenever its Swift
sources change (Xcode command-line tools required). QEMU runs without its Cocoa
window and serves raw framebuffer updates over a private Unix VNC socket; no TCP
listener is opened. The player preserves the guest aspect ratio when resized.
Click and drag on the guest display to use its touchscreen.

The toolbar has Home (a short hardware menu-button press), Control Center
(a paced swipe upward from the bottom edge), and Screen Capture
(a PNG of the current guest frame, saved using Save As). Captures exclude window
chrome and keep the guest's 640×1136 resolution. Closing the player stops QEMU and
removes temporary disk clones and sockets. Serial and QEMU logs remain in the
printed `/tmp/vphone-live-*` directory. Source disk images are not modified.
Guest Manager's Start VM flow is separate and unchanged.

The Home input is modeled through GPIO2 and its configured interrupt, with an
automatic release after 100 ms of guest time. This does not implement the separate
PMGR wake-status contract for an already sleeping guest.

Player checks:

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_player.py'
.venv/bin/python -m unittest discover -s tests -p 'test_home_button.py'
.venv/bin/python backend/home_button_contract_test.py
```

The native player also mirrors the `wt/touch` Cocoa gesture mappings: two-finger
trackpad scroll becomes a one-finger drag, wheel notches move 40 guest pixels and
lift after 150 ms, Option-drag pinches, Option+Shift-drag moves both fingers, and
trackpad magnify spreads two fingers around the pointer. Option shows the two
finger positions. Host momentum is ignored because iOS supplies deceleration.
These gestures use the private QMP socket and the existing A7 multitouch slots;
plain click/drag uses VNC. Losing window focus releases active contacts.

The emulator process retains App Nap protection even with `-display none`, via
`0005-keep-headless-emulator-awake.patch`. Rebuild the backend after updating this
patch; keeping only the player awake does not protect the separate QEMU process.
A session's `qemu.log` confirms activation, and `player-input.log` records pointer
press/drag/release and multitouch begin/end events for diagnosis. A new physical
click cancels a stale gesture; a QMP gesture error leaves VNC click/drag available
and lets a later gesture reconnect.
