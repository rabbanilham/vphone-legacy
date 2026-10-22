# Virtual activation profile — iOS 7.1.2 / 11D257

## Result and acceptance boundary

The guest's native Lockdown API now returns **ActivationState = Activated**
and **BrickState = false**, including after restarting lockdownd. The state is
also retained across sequential cold boots using the preceding session's Data
disk. This is **virtual activation for testing**, not Apple activation.

**The complete plan is not yet accepted:** the framebuffer after the normal
unlock swipe remains mostly black rather than showing usable Home icons.
Do not equate successful activation checks with successful Home rendering.

## Artifacts

The prepared profile is `build/virtual-activated-profile-v5/`:

- `system.raw`, `data.raw`: separate diagnostic images; baseline images are
  not changed. `data.raw` is the fresh prepared seed, not a resumed session.
- `profile.json`: input/output hashes, instruction patch, guest ownership
  edits, job-cache modification, diagnostic boot policy, and validation status.
- `boot/boot_plan.json`, `boot/KernelMemory.bin`, `boot/DeviceTree.runtime`:
  derived boot configuration. No guest-kernel instruction patches are needed.
- `virtual-activation-probe`: real ARM64 guest client; its ad-hoc SHA-1 signature
  verifies with host `codesign --verify`. It is not an Apple-signed executable.
- `qemu-system-aarch64`: pinned local emulator used for boots 2 and 3, SHA-256
  `3a2bf2d243d56eaf1c2754eec8e672aaf40ca1a0051c17b4104f8a0dbafbe8b8`.

Runtime evidence is in `backend/evidence/virtual-activated-20261023/`.
Each `cold-bootN/` retains its own disks, invocation, provenance, screenshots,
logs, observations and activation evidence. The latest session's retained
`images/data.raw` can be selected explicitly to continue its persisted state.

## Native state change

`scripts/patchers/patch_virtual_activation.py` accepts only lockdownd with
SHA-256 `d1421e2fc37a76732717e23da1c276c80b51d0e2da1599765b0afbbe585d426e`.
At file offset `0x12e5c` / unslid VA `0x100012e5c`, it replaces:

```text
c0 07 00 34    cbz w0, 0x100012f54
1f 20 03 d5    nop
```

This selects an existing fallthrough in `dealwith_activation`, whose native
message is “Short circuiting activation state to Activated.” The existing
instructions then:

1. Mark global ActivationState volatile through the native storage metadata
   helper (`0x10000f468`).
2. Write the existing CFString `Activated` (`0x10009a5c0`) through the native
   setter (`0x10000eb80`), for both the global and lockdown_cache domains.
3. Set ActivationStateAcknowledged=true and BrickState=false.
4. Post `com.apple.mobile.lockdown.brick_state` through the existing native
   notification wrapper (`0x100007714`).

Consequently, **absence of `-ActivationState` from data_ark is expected** on
this path: its global value is volatile, while
`com.apple.mobile.lockdown_cache-ActivationState = Activated` and
`-BrickState = false` are persistent. Live API reads, rather than a fabricated
global plist key, verify the values exposed to guest clients.

The patch preserves the existing ABI, native setter/getter implementations,
notifications and service protocol. It does not create activation records,
alter host response formatting, or change the emulated hardware's activation
answers. The original short-circuit opt-out preference is not enabled in the
fresh diagnostic seed.

Disassembly and exact input bytes are retained under `inputs/` in the evidence
directory. The patcher refuses unknown hashes, modified context and its own
already-patched output.

## Loading and guest verification

The derived plan sets the guest's `/chosen/debug-enabled` property and these
native diagnostic boot arguments:

```text
amfi_allow_any_signature=1 amfi_get_out_of_my_way=1 cs_enforcement_disable=1
```

This intentionally relaxes code-signing enforcement **inside this testing
profile**. It permits the modified lockdownd, modified job cache and diagnostic
client to execute. Serial logs explicitly confirm the native AMFI options were
accepted. Baseline plans/default launch arguments remain unchanged.

The guest client calls `lockdown_connect`, `lockdown_copy_value`, and
`lockdown_disconnect`, matching SpringBoard's native API. Native enforcement
requires `com.apple.private.lockdown.finegrained-get`, so its ad-hoc signature
grants only:

```text
NULL/ActivationState
NULL/BrickState
```

The client records typed native values to
`/private/var/root/virtual-activation-probe.jsonl` and the guest console. It
records native `launchctl list` output, stops the `com.apple.mobile.lockdown`
job once per invocation, and reconnects to trigger its normal socket activation.
The before/after PIDs and API reads are checked by
`backend/collect_virtual_activation.py` after a read-only Data attachment.

The probe also calls native `SBSUndimScreen` while sampling. This is only display
wake; the unlock attempt still uses the ordinary guest touch pipeline. The raw
`errno` field is ambient diagnostic context, not an error result for successful
API calls; `connected`, object presence, CF type IDs and `valid` distinguish the
actual query result.

The existing launchd cache does not discover a new system job solely from its
plist. The builder therefore adds the diagnostic job to the embedded xpcd cache,
preserving every existing job and the original section's length. New files get
guest root:wheel ownership through checked catalog BSD-ID edits on the clean,
unmounted **cloned** HFS image. Host ownership mapping is not used as proof of
guest ownership. No host root password is required.

## Runtime checks

| Run | Input Data | Native API samples | lockdownd PID change | Activation checks |
| --- | --- | --- | --- | --- |
| cold-boot1 | Fresh v5 profile | 45 valid: 4 before / 41 after restart | 38 → 130 | Pass |
| cold-boot2 | cold-boot1 retained Data | 42 valid: 4 before / 38 after restart | 43 → 128 | Pass |
| cold-boot3 | cold-boot2 retained Data | 40 valid: 4 before / 36 after restart | 43 → 127 | Pass |

All three runs completed 180 guest-running seconds without kernel panic. They show
regular “slide to unlock” rather than the multilingual setup greeting. The
post-swipe captures remain dark with a small rectangle, so Home rendering and
interaction are **not** marked verified.

Boot 1 used the then-current default renderer; boot 2 explicitly disabled the
new render thread (`VPHONE_AGX_REPLAY_THREAD=0`). The dark result persisted in
both modes. Native `IconState.plist` lists the stock icon pages/dock, the
installation cache is populated, and LockBackground.cpbitmap exists. This
rules out an entirely empty persisted icon layout, but not every UI startup
or renderer failure. These observations are not a proof that rendering is the
only remaining issue.

Boot 3 also captured bounded raw GPU command/page dumps. Independent offline
replay of `render-dumps/frame-1.bin` yields
`home-offline-frame1.png`: stock Home icons, labels and dock are present, with
rendering artifacts. This is a reconstruction from guest GPU commands, **not a
live screen capture** and not proof that the window shows an interactive Home.
The corresponding live replay logged 79 draws into offscreen target
`0x10214000`; thus the complete Home scene is submitted and the replay path does
process it. The subsequent live composite/scanout discrepancy remains open.

`graphics-audit.md` contains the agy worker's analysis. Its proposed
triple-buffering cause is **a hypothesis, not an established root cause**:
`display_update()` already gathers the active display MMU mapping, and the
timed captures use QMP screendump, not the fixed-address raw-memory capture.
Do not apply a hardware/display change solely on that report's conclusion.
The active renderer source was concurrently under other work; this task did
not alter that source or install a speculative display fix.

QEMU `quit` ends these diagnostic runs: they are cold power-cycle tests, not
proof of graceful guest shutdown. Retained HFS images may replay their journal
on the next boot. No claim of uninterrupted SpringBoard liveness is based
solely on the absence of crash reports.

## Reproduction

Build a fresh profile in a new output directory:

```sh
.venv/bin/python scripts/prepare_virtual_activation.py \
  --output build/virtual-activated-rebuild \
  --plan backend/evidence/keybag-contract-20261015/boot-plan-smp-nooscar/boot_plan.json
```

The builder refuses existing profile outputs and checks the original lockdownd
and xpcd section hashes before patching. It needs macOS disk-image attachment,
Clang and the system linker; exported-symbol stubs link to real guest libraries,
not mock implementations. `-bind_at_load` avoids an ld 27 lazy-stub assertion.
No additional linker package is needed.

Launch the prepared v5 profile interactively:

```sh
VPHONE_TCG_THREAD=multi VPHONE_AGX_REPLAY_THREAD=0 \
.venv/bin/python backend/live_run.py \
  --qemu-binary build/virtual-activated-profile-v5/qemu-system-aarch64 \
  --plan build/virtual-activated-profile-v5/boot/boot_plan.json \
  --system build/virtual-activated-profile-v5/system.raw \
  --data build/virtual-activated-profile-v5/data.raw \
  --keep-images --collect-crashlogs
```

The launcher clones its inputs. To resume persistence, use the preceding run's
retained Data image with `--data`. To extract API evidence from a stopped clone:

```sh
.venv/bin/python backend/collect_virtual_activation.py \
  --data PATH_TO_STOPPED_DATA.raw --output NEW_EVIDENCE_DIRECTORY
```

`display_probe.py --framebuffer-at SECONDS` adds timed QMP captures without
changing default behavior. Both diagnostic launchers accept `--qemu-binary`
to avoid silently switching emulator builds during concurrent work.

## Tests

Eight focused profile tests pass, including the exact instruction change,
unknown/prepatched input rejection, preservation of existing launch jobs,
boot-argument/embedded-tree consistency, bounded HFS ownership edits, native
API/disk/PID agreement, and CodeDirectory/entitlement hashes.
The seven provisioning, eighteen probe-state, and eighteen crashlog tests also
pass (51 tests total).

```sh
.venv/bin/python -m unittest discover -s tests -p test_virtual_activation.py
.venv/bin/python -m unittest discover -s tests -p test_prepare_diagnostic_disks.py
.venv/bin/python -m unittest discover -s tests -p test_probe_state.py
.venv/bin/python -m unittest discover -s tests -p test_crashlogs.py
git diff --check
```
