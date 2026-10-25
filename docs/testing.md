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

## Optional integration

`VPHONE_TEST_BUNDLE=/absolute/path/to/bundle` enables lifecycle integration in `test_backend_lifecycle.py`. It launches QEMU and force-stops the guest, modifying runtime/session state: use a disposable prepared bundle. `VPHONE_REAL_FIRMWARE_TEST=1` enables real-firmware tests. `VPHONE_TEST_IPSW` and `VPHONE_TEST_ANS_SOURCE` override the local IPSW and extracted ANS file paths respectively; absent overrides use the historical checkout layout. Inspect `test_firmware_gate.py` and `test_ans_image.py` before enabling it. These tests are not part of firmware-free CI.

See [publication audit](publication-audit.md) for checks actually performed during repository preparation and their limitations.
