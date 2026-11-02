# GUI / CLI contract

The SwiftUI app (`sources/vphone-legacy-gui`) never reimplements firmware, disk or launch logic. It runs the `vphone-legacy` CLI and reads the machine-readable output below. Every command keeps its human-readable text on **stderr** when a JSON flag is given; JSON goes to **stdout** only. Fixtures in `tests/fixtures/gui/` are the shared examples for both the Python producers and the Swift decoders. Schema changes bump `schema` and update the fixtures in the same commit.

## Progress stream (`--progress-json`)

Long operations (`vm create`, `vm prepare-ui`, `setup install`, `app install`) print one JSON object per line (NDJSON):

```json
{"schema":1,"event":"stage","id":"decrypt-rootfs","status":"progress","fraction":0.4,"message":"Decrypting RootFS","hint":null}
```

- `event`: `stage` (progress), `log` (free text line, `message` only), `result` (final, exactly once, `ok` boolean plus command-specific `data`), or `error`.
- `status` (for `stage`): `start`, `progress`, `done`, `skipped`, `error`.
- `fraction`: 0.0-1.0 within the stage, or `null` when unknown.
- `hint`: short user-facing fix suggestion on `error`, otherwise `null`.
- `id` values for `vm create`: `preflight`, `extract`, `verify`, `decode-kernel`, `decode-ibot`, `decode-devicetree`, `decrypt-rootfs`, `convert-rootfs`, `create-data`, `manifest`. For `vm prepare-ui`: `sep-extract`, `diagnostic-disks`, `virtual-activation`, `boot-plan`, `ui-manifest`, `boot-check`. For `setup install`: the prerequisite id. For `app install`: `inspect`, `sign`, `copy`, `ownership`, `cache`.
- Exit status is non-zero iff a final `result` with `"ok":false` (or an `error` event) was emitted. Consumers must not parse stderr.

## `vm list --json`

```json
{"schema":1,"root":"/Users/me/.vphone-legacy/VMs","vms":[{"name":"ios712","path":"...","ios":"7.1.2","build":"11D257","device":"iPhone6,2","profile":"baseline|ui","memory_mb":1024,"disk_bytes":7300000000,"phase":"stopped|starting|running|stopping|failed","pid":null,"boot_stage":"none","apps":[{"bundle_id":"com.vphone.hello","path":"/Applications/Hello.app"}]}]}
```

## `vm status NAME --json`

`runtime.json` fields (`session_id`, `process{phase,pid,exit_code}`, `shutdown{clean,reason}`, `log_directory`, `control`) plus `boot_stage` (`none`, `kernel`, `userspace`, `lockscreen`, `home`, `panic`), `display` (`none|cocoa|player`) and `lock` (`free|held`).

## `setup check --json`

```json
{"schema":1,"ok":false,"items":[{"id":"homebrew","title":"Homebrew","status":"ok|missing|fixable","detail":"/opt/homebrew/bin/brew","fix":{"kind":"auto|manual","command":null,"instructions":"..."}}]}
```

Item ids: `xcode-clt`, `homebrew`, `brew-packages`, `venv`, `backend`, `player`, `vfdecrypt`, `qemu-img`, `disk-space`, `wifi-helper` (optional; never makes `ok` false). `setup install ID --progress-json` performs an `auto` fix with an explicit `PATH` (`/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin`) and streams `stage` events.

## VM management

- `vm create NAME --ios 7.1.2 --ipsw PATH [--force] [--progress-json]`: `--force` removes an existing bundle and stale `NAME.preparing-*` directories first.
- `vm prepare-ui NAME [--force] [--progress-json]`: turns a baseline bundle into the UI profile (diagnostic disks, virtual activation, boot plan, manifest `profile: ui`, persistent runtime disks) and runs `boot-check`.
- `vm delete NAME [--json]`, `vm import-profile NAME --from PATH [--progress-json]` (existing prepared profile, e.g. `build/virtual-activated-profile-v5`).
- `vm launch NAME [--display player|cocoa|none] [--wifi]`: for `profile: ui` the default display is `player`. Closing the player stops QEMU. `runtime.json` is the status channel.
- `app install NAME IPA [--enable-unsigned] [--json|--progress-json]`, `app list NAME --json`, `app remove NAME BUNDLE_ID`. The VM must be stopped (the launcher lock must be free); `--enable-unsigned` appends the relaxed-AMFI boot arguments to that VM's manifest only.

## Root resolution

The CLI and scripts honour `VPHONE_LEGACY_ROOT` (repository checkout) and `VPHONE_LEGACY_VMS_DIR`. The GUI stores the chosen checkout in `UserDefaults` (`repoRoot`) and passes it through the environment; it never derives paths from `#file`.
