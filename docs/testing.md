# Testing

## Firmware-free checks

```sh
make setup
make test
.venv/bin/python scripts/check_docs.py
zsh -n scripts/setup_venv.sh scripts/build_app.sh vphone-legacy
bash -n research/tasks/merge-live.sh
```

Default discovery runs Python tests and small compiled C harnesses. A host compiler is needed for those harnesses. It does not boot iOS or require a private VM. Missing optional firmware, dump corpora, or backend build tools produce explicit skips; inspect the skip summary before interpreting coverage. CI runs this path on macOS, with no firmware or private artifacts.

To run the three probe-164 dump test modules against an external corpus, set `VPHONE_AGX_DUMPS=/absolute/path/to/dumps`. Keep the original `frame-N.bin` files and sidecars together. Other historical corpus tests use their documented local `build/` or `backend/evidence/` locations. Never commit memory dumps.

## Backend checks

After building the project QEMU:

```sh
.venv/bin/python backend/smoke_test.py
.venv/bin/python backend/agx_contract_test.py
.venv/bin/python backend/touch_input_contract_test.py
.venv/bin/python backend/spi_touch_contract_test.py
.venv/bin/python backend/smp_contract_test.py
```

These execute synthetic guest contracts. UART/framebuffer success is not native iOS acceptance. QMP requires local Unix sockets. Native boot acceptance must independently record source/backend/input hashes, serial output, screenshots, timing, and panic/exit status.

## Offline AGX corpus replay and fast iteration

`scripts/agx_corpus.py` compiles the renderer core of `backend/s5l8960x.c` into a standalone host harness (`backend/agx_replay_host.c`) to replay recorded GPU passes offline without booting QEMU.

### Common CLI workflows

```sh
# 1. Fast C-only replay on a single frame
.venv/bin/python scripts/agx_corpus.py replay \
  --corpus build/agx-corpus --scene clock --frames 5 --c-only --output /tmp/clock-f5

# 2. Iterate on a range with dependency chaining
.venv/bin/python scripts/agx_corpus.py replay \
  --corpus build/agx-corpus --scene settings-all --frames 5..10 --c-only --output /tmp/settings-f5-10

# 3. Full multi-scene audit without deleting existing reports
.venv/bin/python scripts/agx_corpus.py replay \
  --corpus build/agx-corpus --scene clock,settings --unique-output

# 4. Standard default chained audit across all corpus scenes
.venv/bin/python scripts/agx_corpus.py replay --corpus build/agx-corpus
```

### Dependency prefix semantics

In chained replay mode (the default), GPU memory is stateful: later passes frequently composite, blend, or sample from offscreen passes or earlier framebuffers. When specific frames are selected via `--frames` (e.g. `--frames 5` or `--frames 5..10`):
- All earlier passes in scene order (passes 1..4) execute as a **dependency prefix** through `agx_replay_host --chain`.
- Carried dirty memory pages are propagated forward into selected frames so rendered pixels match full-chain replays bit-for-bit.
- Unselected prerequisite passes release intermediate RGBA buffers immediately without encoding PNGs.
- If any prerequisite pass encounters an error or skips a draw, dependent selected frames are marked invalid and execution exits with code 1.

### Stage timing boundaries

Phase timings are separated and reported in stdout and `summary.json`:
- `build_s`: harness core extraction, SHA-256 hash check, and compiler invocation (cached check takes ~0.0s).
- `c_replay_s`: elapsed replay stage, including harness subprocesses and their result processing, before PNG conversion.
- `py_audit_s`: Python offline audit (`agx_replay_offline.py` / `audit_frame`); exactly 0.0s when `--c-only` is passed.
- `export_s`: image conversions (selected RGBA to PNG, live PPM to PNG) and summary metrics calculation.
- `total_s`: elapsed time from replay entry through image export. Final JSON/HTML serialization and console output are excluded from these timings.

### Report preservation and output directories

- Existing report directories are never deleted or truncated.
- If `--output <DIR>` is specified, `<DIR>` must be empty or new; existing non-empty directories are rejected with an error to prevent accidental data loss.
- If `--unique-output` is given or the default `report/` directory already exists, a timestamped directory (e.g. `report-YYYYMMDD-HHMMSS[-N]`) is created atomically under `--corpus`.

## Optional integration

`VPHONE_TEST_BUNDLE=/absolute/path/to/bundle` enables lifecycle integration in `test_backend_lifecycle.py`. It launches QEMU and force-stops the guest, modifying runtime/session state: use a disposable prepared bundle. `VPHONE_REAL_FIRMWARE_TEST=1` enables real-firmware tests. `VPHONE_TEST_IPSW` and `VPHONE_TEST_ANS_SOURCE` override the local IPSW and extracted ANS file paths respectively; absent overrides use the historical checkout layout. Inspect `test_firmware_gate.py` and `test_ans_image.py` before enabling it. These tests are not part of firmware-free CI.

See [publication audit](publication-audit.md) for checks actually performed during repository preparation and their limitations.
