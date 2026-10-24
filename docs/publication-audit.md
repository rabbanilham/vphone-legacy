# Publication preparation audit — 2026-10-24

Changes started from `agx-meta-hle` at `15940a9`, isolated on `chore/public-ready`, and were subsequently synchronized to `origin/main` at `d3a8020`. The original checkout and VM data were not edited. No push, merge commit, visibility change or history rewrite was performed.

## Implemented

- Replaced the outdated README with English setup, firmware, status, architecture, testing and troubleshooting guides, contribution guidance, and linked historical UI evidence.
- Moved maintained conclusions from agent handovers into the guides; removed root handovers, duplicate prompts and obsolete agent session launchers. Kept technical briefs/reports, the live acceptance helper, profiles and regression evidence.
- Normalized personal machine paths in historical text/logs; technical hashes and screenshot bytes were retained. Historical references to private artifacts are explicitly identified as unavailable.
- Made Python/Keystone/Homebrew detection portable, pinned direct Python dependencies to the validated environment, removed private test/probe paths, and added explicit external-fixture overrides.
- Added early research-profile input validation, firmware-free macOS CI, maintained-guide link checking, publication triage tooling and upstream license reference texts.

## Validation performed locally

| Check | Result |
| --- | --- |
| Isolated setup with Python 3.13 | Passed; new worktree `.venv`, no original environment reuse |
| Independent pinned QEMU build | Passed; source cloned from the existing local upstream Git repository, exact lock commit verified; build dependencies installed into this worktree |
| Default suite before backend build | 262 tests passed, 16 optional skips (before adding two preflight tests) |
| Full suite after backend build | 264 tests passed, 14 optional skips |
| Real IPSW firmware-gate module | 15 tests passed using explicit local IPSW path |
| Real ANS module | 2 tests passed using explicit local extracted-ANS path |
| Backend smoke | Passed guest execution, UART, framebuffer/QMP, stop/quit and invalid-input checks |
| AGX / touch / SPI touch / SMP contracts | All passed with the independently built backend |
| New missing-profile preflight tests | Passed; missing plan/auxiliary image rejected before disk cloning |
| CLI help and script syntax | Passed |
| Maintained-guide links and diff whitespace | Passed |
| New baseline bundle from local IPSW | Preparation and `doctor` passed, no validation errors |
| Five-second baseline headless launch | Executed guest userspace; serial recorded FIPS POST integrity failure and userspace shutdown. QEMU exited 0; this is **not** successful iOS/UI acceptance. |

The initial socket-restricted test attempt failed when binding QMP's local Unix socket; the permitted rerun passed. Setup/build downloads also required network access outside the restricted sandbox. Those were environment restrictions, not hidden test skips.

Firmware preparation and launch used a newly created disposable bundle in this worktree. Existing IPSW/extracted ANS inputs were read only. The disposable bundle was removed after recording the results to avoid retaining roughly 11 GiB of generated files. No new Home/Settings acceptance run was performed. The CI workflow is configured but has not run on GitHub because nothing was pushed.

## Content and history audit

Run `.venv/bin/python scripts/audit_publication.py` for a read-only JSON report. It reports categories and paths/blob IDs without printing matched credential values. The scanner covers tracked and unignored working files and unique blobs reachable from all local refs; metadata includes author/committer email identity counts. Results are triage, not a guarantee that no secrets exist: it uses known service-token/private-key/credential-assignment patterns, not entropy analysis, remote-ref discovery or binary OCR.

At the final local scan, 232 reachable commits and 798 unique blobs were inspected. Another local experimental branch advanced during preparation; the target baseline remained `15940a9`. Four distinct author/committer email identities exist in reachable metadata. No matched service-token, private-key or credential-assignment patterns, guest-image/capture extensions, or blobs above 10 MiB were found. Screenshots and a guest texture crop are present under research evidence and require a separate rights review.

Current working content has no matched personal macOS home paths. Historical personal paths remain in 142 unique blobs. Firmware-key fields occur in four historical blobs and current manifest/preparation/test code; the scanner's own rule definitions also match that category. These fields are firmware metadata, not identified account credentials; their origin/publication suitability still needs review. Existing third-party copyright email addresses were preserved.

## Remaining publication blockers

1. **License/provenance:** the backend declares GPL-2.0-or-later and VFDecrypt retains its original permissive grant. Remaining project code and the small unmarked helper header need ownership/ancestry review before selecting a repository-wide license. See [third-party notices](../THIRD_PARTY_NOTICES.md).
2. **History privacy:** deleting handovers from the current tree does not remove earlier paths or commit email metadata. Decide whether those historical disclosures are acceptable before changing visibility. Any history rewrite must be handled separately; none was attempted here.
3. **Guest-derived materials:** review the provenance/publication suitability of firmware keys, UI screenshots and extracted texture evidence. No complete firmware or disk images were found by the extension scan.
4. **UI reproducibility:** complete the IPSW-to-keybag/SMP/nooscar preparation pipeline before advertising the recorded Home profile as reproducible from a fresh clone. The tested baseline currently reaches FIPS userspace failure.

Direct Python dependencies are pinned; transitive dependencies, Homebrew formulae and the host SDK are not fully locked. Remote CI execution and a new native UI/persistence/stability acceptance run remain future validation work.

## Follow-up: main sync and README captures

The worktree was fast-forwarded to `origin/main` at `d3a8020`. Relative to the original baseline, the only new tracked content was the time-dilation evaluation in the internal handover. That conclusion is now retained in [status](status.md): keep k=1; k>1 failed to complete unlock. Emulator source did not change, so the earlier 264-test and backend validation results still apply; no new full-suite or UI boot run was needed for this documentation update.

The README now shows original Home/Settings captures from the latest available normal-speed integration run, `merge-u2-live`, rather than the older Home acceptance image. The PPM-to-PNG conversion was checked by decoding the PNGs and comparing every RGB pixel with the originals: both match exactly at 640×1136. Hashes and run/source provenance are included with the [screenshots](../research/evidence/public-readme-20261024/README.md). Maintained-document links, whitespace and conflict checks passed.

A refreshed history scan inspected 235 reachable commits and 801 unique blobs across local refs. It found 143 historical blobs with personal paths, four metadata email identities, and the previously documented firmware-key fields; no service-token, private-key, credential-assignment, guest-image/capture-extension or >10 MiB-blob pattern was flagged. Current working content has no matched personal macOS home paths. Publication blockers above remain unchanged.
