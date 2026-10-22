# Setup Assistant provisioning validation — 2026-10-23

## Result

**The Home-screen acceptance criterion is not met.** PurpleBuddy UI completion
preferences are present and survive boot, but the guest reports
`-BrickState = true` and cached `ActivationState = Unactivated`. Completion
preferences do not override SpringBoard's separate activation/buddy-mode gate.
No activation records, activation state, system binaries, or emulator hardware
were changed by this task.

## Preparation and provenance

The existing `build/diagnostic/data-diagnostic-setupdone.raw` was not a clean
output of the current helper: it contained a previously seeded
`-ActivationState = FactoryActivated` and `-BrickState = false`. Its SHA-256 was
`693c8aceb46c7885722bd53daed1701b3c2ea19ec4146b4a1afd7c629fc4c0f1`.
It was preserved as
`build/diagnostic/data-diagnostic-setupdone-pre-validation-20261023.raw`.
`boot1` below is a comparison run of that historical image, not clean evidence.

The setupdone image was then regenerated from
`build/diagnostic/data-diagnostic-systembag.raw` using:

```sh
.venv/bin/python scripts/prepare_diagnostic_disks.py --setup-done-variant
```

The regenerated setupdone SHA-256 is
`8b233e4bd378074a3a7c63c6b7e149aeaedc304430e828cc9561959f116c1716`.
Read-only inspection confirmed no `root/Library/Lockdown/data_ark.plist` in
either the source systembag image or the regenerated setupdone image. The
source images are not used as writable guest disks; each probe creates clones.

`seed_setup_done()` writes binary plists with `SetupDone`,
`SetupFinishedAllSteps`, `AssistantPresented`, and `ForceNoBuddy` set to boolean
true, `SetupVersion` set to integer 5, and `SetupState = SetupUsingAssistant`.
Both mobile and root receive `com.apple.purplebuddy.plist`; mobile also receives
the existing `com.apple.purplebuddy.notbackedup.plist` flags. The source's
11D257 migration/accountsd preferences are retained.

## Runtime evidence

Local evidence directory: `backend/evidence/setupdone-validation-20261023/`.
`preparation.json` records the historical input; `clean-preparation.json`
records the rebuilt input. `environment.json` records the environment used.
Each run has its own command, hashes/provenance, logs, disk clones, framebuffer,
observations, and collected crash-report index.

All runs use the existing `boot-plan-smp-nooscar` plan, two CPUs, MTTCG, the
diagnostic SEP system image, and this normal guest input:

```sh
VPHONE_TOUCH_SCRIPT='at=80;down 40,1036;hold 0.3;move 600,1036 dur=3 steps=180;hold 1.0;up'
```

| Run | Input | Duration | Observed result |
| --- | --- | --- | --- |
| `boot1` | Historical setupdone | 150 guest-running seconds | No kernel panic; framebuffer shows Setup's “Update Completed” and Continue button. Historical factory state is removed; postboot state is bricked/unactivated. |
| `clean-boot1` | Rebuilt setupdone | 95 guest-running seconds | No kernel panic; framebuffer shows a white screen with status bar, not Home icons. Completion preferences survive; postboot state is bricked/unactivated. |
| `clean-boot2` | Rebuilt setupdone | 150 guest-running seconds | No kernel panic; framebuffer shows Setup's “Update Completed” and Continue button. Completion preferences survive; postboot state is bricked/unactivated. |
| `clean-boot3` | Rebuilt setupdone | 150 guest-running seconds | No kernel panic; framebuffer shows a white screen with status bar, not Home icons. Completion preferences survive; postboot state is bricked/unactivated. |

The last two runs used independent clones concurrently. There were no collected
`.ips`/`.crash` reports in either run. None of the three clean cold boots met the
Home-screen criterion. The white endpoints do not establish whether Setup was
still drawing, stalled, or affected by a separate display issue; that distinction
was not diagnosed here.

For these runs, `postboot-state.json` was extracted from the stopped data clone
using a read-only attachment. Setup itself adds
`WebKitAcceleratedDrawingEnabled = false` to mobile's PurpleBuddy preferences.
The completion flags remain present. This does not establish the exact live
return value of `PSSetupAssistantNeedsToRun`, but rules out missing persisted
completion flags as an explanation for these runs.

The earlier local A1 static audit identifies a second check in SpringBoard
`_SBUpdateBuddyMode` at unslid `0x1000dedb4`/`0x1000dedb8`:
`SBLockdownManager.brickedDevice` can enable buddy mode after the preference
check returned false. This is consistent with the observed persistent state.
Its further claim that manually setting FactoryActivated would persist is
contradicted by the historical-image comparison run and must not be treated
as a validated solution.

No kernel panic and absence of a collected SpringBoard crash report alone
prove neither uninterrupted SpringBoard liveness nor successful UI startup.
Home-screen acceptance must use the actual framebuffer and guest interaction.

## Host checks

An agy CLI worker added regression coverage in
`tests/test_prepare_diagnostic_disks.py`. Seven focused tests pass:

```sh
.venv/bin/python -m unittest discover -s tests -p test_prepare_diagnostic_disks.py
git diff --check
```

The tests cover typed binary-plist payloads, the mobile/root preference paths,
preservation of migration preferences and existing Lockdown bytes, and no
creation of activation state by the setup helper. These checks verify the
provisioning contract, not Home-screen reachability.

## Reproduction and remaining prerequisite

```sh
VPHONE_DISPLAY=none VPHONE_TCG_THREAD=multi \
VPHONE_TOUCH_SCRIPT='at=80;down 40,1036;hold 0.3;move 600,1036 dur=3 steps=180;hold 1.0;up' \
.venv/bin/python backend/display_probe.py \
  --plan backend/evidence/keybag-contract-20261015/boot-plan-smp-nooscar/boot_plan.json \
  --diagnostic-system build/diagnostic/system-diagnostic-sep.raw \
  --diagnostic-data build/diagnostic/data-diagnostic-setupdone.raw \
  --seconds 150 --stop-condition duration --collect-crashlogs \
  --output backend/evidence/setupdone-validation-20261023/reproduction
```

Choose a fresh output directory. For interactive testing, pass the same plan
and image via `backend/live_run.py --plan ... --data ... --keep-images`.
The normal launcher clones its inputs and does not reuse prior-session changes
automatically; retained clones can be selected explicitly for later boots.

The remaining prerequisite is a guest activation state that SpringBoard
actually accepts. This task leaves that state to the guest, as agreed in the
plan. No successful activation path is established here. Until that separate
prerequisite is resolved, marking UI setup complete is not sufficient to
promise unlock to Home.
