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

## Experimental UI preparation from a local IPSW

After `vm create`, the offline UI pipeline can extract n53 SEP firmware directly
from your IPSW, prepare diagnostic disk copies, seed migration/Setup Assistant
preferences, patch virtual activation, and regenerate an SMP boot plan. Firmware
and prepared images remain local; none are distributed with the project.

```sh
python3 scripts/ui_profile.py prepare-ui /path/to/VMs/ios712 \
  --ipsw /path/to/iPhone6,2_7.1.2_11D257_Restore.ipsw --progress-json
python3 scripts/ui_profile.py import-profile /path/to/VMs/ios712 \
  --from /path/to/virtual-activated-profile-v5 --progress-json
```

`--ipsw` is optional when `firmware.ipsw_path` or `firmware.ipsw_filename` resolves
to an existing local file, or the bundle already contains the extracted SEP.
The IPSW hash must match the baseline manifest when recorded. A differing local
research SEP reference produces a warning, rather than replacing the user's input.
`--force` creates a new generation from the untouched baseline disks. Existing UI
generations are retained for recovery; they can consume disk space. A stopped VM
is required (`runtime.lock` is held throughout preparation). Failed work directories
are removed; completed profiles and the manifest switch are published atomically.
An interrupted process can leave `.ui-preparing-*` directories, which may be removed
only while holding the same lock and after confirming no preparation is running.

The resulting manifest retains schema 2 and immutable firmware/System records.
`artifacts.system_runtime_disk` and `artifacts.data_disk` point to persistent UI
disks beneath `ui/<generation>/`; `plan` points to its self-contained boot plan.
The existing `validate_bundle` provenance/size rules already accept this layout.
Relaxed AMFI boot arguments and `chosen/debug-enabled` apply to this UI profile.

For CLI integration, import `prepare_ui(bundle, progress=None, force=False,
ipsw=None)` or `import_profile(bundle, source_profile_dir, progress=None,
force=False)` from `scripts/ui_profile.py`. The callback accepts one schema-1 stage
object per call, matching [the GUI contract](gui-contract.md). The API returns the
new manifest or raises; the CLI wrapper owns the single final `result` event.
The standalone commands above emit that result themselves. No contract or fixture
schema was changed. Runtime integration must consume manifest `plan` (including
its auxiliary images), or pass `debug_enabled=True` when rebuilding UI plans;
rebuilding with the baseline defaults drops the required debug property.

### Evidence gates and Home limitations

Offline preparation deliberately emits `boot-check: skipped` and records
`home_screen_verified: false`. It does not boot a VM. `SpringBoard` or `lockdownd`
serial text proves userspace only. `qemu.boot_check.classify_boot(serial,
screendump)` accepts P6 QMP framebuffer bytes; a conservative icon-grid/dock
reference match identifies Home, while a clock/slide/wallpaper heuristic identifies
lockscreen. The latter is heuristic, with no captured lockscreen ground truth;
visual changes, overlays, or different wallpaper can cause false negatives.
The compact Home reference is derived from the archived public evidence, not a
new boot. Synthetic lockscreen tests do not prove reliable real-device detection.

The regenerated boot plan is **not identical to v5**. The offline oracle report
and assessment are in [profiles/ui](../profiles/ui/README.md). In particular the
new plan omits a persisted SEP ART NVRAM variable present in v5; functional
equivalence and IPSW-to-Home remain unverified. Importing an existing prepared
profile preserves its actual runtime DeviceTree/NVRAM and materializes all named
auxiliary images, failing if required local bytes are missing.

An orchestrator may run the opt-in acceptance gate (this command boots VMs):

```sh
python3 scripts/e2e_ui_boot.py --name ui-e2e --bundle /path/to/VMs/ui-e2e \
  --ipsw /path/to/iPhone6,2_7.1.2_11D257_Restore.ipsw --seconds 180
```

It creates a fresh bundle, prepares UI, and requires `home` from three separate
cold launches with serial/framebuffer evidence. Use a fresh name: it does not
replace an existing bundle. Runtime/CLI integration must be in place first. Forced
bounded stops are unclean; this gate does not establish clean guest shutdown.
