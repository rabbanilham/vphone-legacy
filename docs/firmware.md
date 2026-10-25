# Firmware and launch

## Baseline bundle from a local IPSW

Only iPhone6,2 / n53ap / iOS 7.1.2 / 11D257 is accepted. Supply your own IPSW; firmware is not included. The preparation pipeline selects the erase identity from BuildManifest, validates/decrypts components, checks image structure, and creates System/Data/utility backing files. It starts at the native secure monitor rather than executing iBoot.

```sh
./vphone-legacy fw list
./vphone-legacy vm create ios712 --ios 7.1.2 --ipsw /path/to/iPhone6,2_7.1.2_11D257_Restore.ipsw
./vphone-legacy vm doctor ios712
./vphone-legacy vm launch ios712 --headless --probe-seconds 5
./vphone-legacy vm status ios712
```

Default storage is `~/.vphone-legacy/VMs`. Set `VPHONE_LEGACY_VMS_DIR="$PWD/build"` before all related commands to use local ignored storage. Existing bundles are never overwritten: select a new name to rebuild. `doctor --firmware-only` skips backend validation; it still does not certify a boot.

Interactive baseline launch uses `./vphone-legacy vm launch ios712`; `--paused` permits debugger inspection. Graceful guest shutdown is not implemented. To stop, use `./vphone-legacy vm stop ios712 --force`; this records an unclean shutdown. Keep backups of valuable guest data. `make clean` preserves VM disks.

The publication-preparation validation reproduced baseline preparation and launch locally: the guest reached launchd userspace, reported `FIPS USER Space POST: Integrity test failed!`, and began shutdown. QEMU exited zero. Treat this as a diagnosed bring-up result, not a successful UI boot.

## UI research profile: additional inputs required

The baseline above is not the configuration used for the recorded Home/Settings runs. The later path is:

1. Prepare baseline firmware/disks.
2. Supply the separately prepared keybag/SMP/nooscar boot plan, its KernelMemory.bin, runtime DeviceTree, and every auxiliary image it references.
3. Supply diagnostic System/Data images with the required seeded filesystem/keybag state.
4. Build a virtual-activated derivative and launch it with the project backend.

```sh
.venv/bin/python scripts/prepare_virtual_activation.py \
  --plan /path/to/local/keybag/boot_plan.json \
  --system /path/to/local/system-diagnostic-sep.raw \
  --data /path/to/local/data-diagnostic-systembag.raw \
  --output build/virtual-activated-rebuild
.venv/bin/python backend/live_run.py \
  --plan build/virtual-activated-rebuild/boot/boot_plan.json \
  --system build/virtual-activated-rebuild/system.raw \
  --data build/virtual-activated-rebuild/data.raw
```

These commands require existing inputs; they are not an end-to-end IPSW recipe. [Profile snapshots](../profiles/virtual-activated/README.md) retain hashes and configuration, not the missing bytes. `${REPO_ROOT}` in those snapshots is a template token and must be replaced recursively before local use. Absolute artifact paths in a real boot plan must point at your own files.

**Reproducibility blocker:** the repository does not contain a complete automated IPSW-to-keybag/SMP/nooscar preparation path. `prepare_diagnostic_disks.py` is a research helper with additional SEP firmware/keybag prerequisites, not a substitute for that missing path. Do not manufacture or distribute these inputs as part of setup. Complete that pipeline in a separate feature change before promising one-command UI boot.

`live_run.py` defaults to `build/virtual-activated-profile-v5`. It clones disposable disks and removes those clones on exit unless `--keep-images` is supplied. The validated storage/utility backing is also required; see the script's storage lookup and the original profile layout. Logs are printed with the temporary work directory. No baseline disk should be overwritten to make this layout work.

For debugger probes use `backend/display_probe.py --help`. Supply `--diagnostic-system`, `--diagnostic-data`, and `--plan` explicitly for research inputs. Hook JSON cache/index paths are resolved relative to the repository root unless absolute. Keep extracted shared caches and generated indexes in ignored local storage.
