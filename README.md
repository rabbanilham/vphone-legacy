# vphone-legacy

An experimental iPhone 5s emulator for **iPhone6,2 / n53ap / iOS 7.1.2 (11D257)** on Apple Silicon Macs. It combines a pinned QEMU backend, firmware preparation tools, and a software model of selected A7 peripherals and AGX rendering commands.

**Research software:** recorded local runs reach the original lock screen, Home, and Settings with guest touch input. A fresh IPSW preparation does **not** yet reproduce that configuration: the latest UI profile depends on additional local boot/keybag artifacts. See [current status](docs/status.md) and [firmware preparation](docs/firmware.md) before attempting a boot.

| Home | Settings |
| --- | --- |
| ![Original iOS Home after clock rendering fixes](research/evidence/public-readme-20261024/home.png) | ![Original iOS Settings after UISwitch and navigation-bar fixes](research/evidence/public-readme-20261024/settings.png) |

*Latest available normal-speed live captures from `merge-u2-live` (2026-10-24), after the clock, blur, and UISwitch fixes. [Capture provenance](research/evidence/public-readme-20261024/README.md). These are recorded research-profile results, not screenshots produced by the quick start below.*

## Quick start: setup and tests

Requires macOS on Apple Silicon, Xcode Command Line Tools, Homebrew, Python 3.11+ (3.13 preferred), and a C compiler. No firmware is needed for the default tests. Intel Macs and other operating systems are not validated.

```sh
git clone https://github.com/rabbanilham/vphone-legacy.git
cd vphone-legacy
brew install python@3.13 keystone glib pixman openssl@3 qemu
make setup
make test
./vphone-legacy --help
```

The Homebrew QEMU package supplies `qemu-img` for disk conversion. Guest execution uses the separately built, pinned project backend:

```sh
.venv/bin/python scripts/qemu/build_backend.py --fetch --install-build-deps
.venv/bin/python backend/smoke_test.py
```

See [installation](docs/setup.md) for prerequisites, SDK selection, and overrides. Building QEMU requires network access, substantial disk space, and time. No firmware, prepared VM, or emulator binary is included.

## Using your own firmware

```sh
./vphone-legacy vm create ios712 --ios 7.1.2 --ipsw /path/to/iPhone6,2_7.1.2_11D257_Restore.ipsw
./vphone-legacy vm doctor ios712
./vphone-legacy vm launch ios712 --headless --probe-seconds 5
```

This prepares and probes the **baseline bring-up bundle**, not the UI profile pictured above. A successful `doctor` checks artifacts, not a completed iOS boot. Read [firmware and launch](docs/firmware.md) for the distinction and current blockers.

## Documentation

- [Setup and backend build](docs/setup.md)
- [Firmware, VM lifecycle, and research profile](docs/firmware.md)
- [Status and known limitations](docs/status.md)
- [Architecture](docs/architecture.md) and [backend contracts](backend/README.md)
- [Testing](docs/testing.md) and [troubleshooting](docs/troubleshooting.md)
- [Contributing](CONTRIBUTING.md)
- [Research archive](research/README.md)
- [Third-party notices and license status](THIRD_PARTY_NOTICES.md)
- [Publication audit](docs/publication-audit.md)

## License status

The QEMU machine overlay declares **GPL-2.0-or-later**. A repository-wide license has not yet been established for the remaining code. See [third-party notices](THIRD_PARTY_NOTICES.md) for attribution and the unresolved licensing work before redistribution. Apple firmware and artwork are not project code.
