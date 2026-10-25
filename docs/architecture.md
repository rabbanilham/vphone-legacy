# Architecture

- `sources/`: Python CLI and SwiftUI manager. The CLI locates VM bundles, validates them and delegates to firmware/runtime tools.
- `scripts/`: manifest selection, component decoding, disk preparation and diagnostic patches. `scripts/qemu/` owns the pinned backend builder, loader and runtime lifecycle.
- `backend/`: the GPL-2.0-or-later QEMU machine overlay, peripheral contracts, bounded probes, and AGX analysis/replay tools.
- `profiles/`: normalized metadata snapshots for known local research configurations.
- `tests/`: synthetic unit/host-C tests plus explicit optional firmware/backend integration tests.
- `research/`: dated technical reports and selected evidence; historical paths and commands can require unavailable artifacts.

## Data flow

A local IPSW is checked against BuildManifest, decoded into original kernel/monitor/DeviceTree components and System disk sectors, then packaged with metadata and storage. The loader builds guest RAM and boot arguments. The runtime validates the project backend manifest and starts QEMU with separate serial and QMP channels. The native secure monitor hands off to XNU.

`vm_manifest.json` records prepared input identity/checksums. `runtime.json` records process/session state; it is not proof of userspace or UI acceptance. Immutable `system.raw` is separated from writable runtime derivatives. Data and utility backing persist; probe tools generally clone them. Guest shutdown/flush support is incomplete.

## Rendering and research tools

The QEMU machine models selected native display, touch, storage and AGX contracts. The guest supplies rendering work; the AGX software renderer implements a subset of submitted commands. META addresses are translated through the modeled GPU mappings (the decoder uses GPU VA `0xe1c0000000 + (address & 0x3fffffff)`). This is not an arbitrary iOS-like host interface.

`backend/live_run.py` launches the prepared UI research profile. `backend/display_probe.py` adds bounded debugger/QMP capture. `scripts/agx_corpus.py` captures optional dump scenes and builds a chained offline renderer report. Dumps and upstream QEMU sources are local ignored artifacts. See [backend contracts](../backend/README.md), [testing](testing.md), and [current status](status.md).
