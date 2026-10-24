# T21: finish the hardware transactions behind four busy services

2026-10-22, worktree `${WORKTREES_ROOT}/vphone-wt-busy-devices`, branch `wt/busy-devices`.

Implemented CBTL1610/TriStar and LM3534 I2C registers, SPI2 SmartIO DMA completion, and per-hit probe instruction mismatch rejection. **INFERRED / awaiting maintainer boot:** these changes let the four captured matching workers finish, making InterfaceNamer observe quiet before its timer. No QEMU build or execution was performed. Host tests establish the implemented bus contracts, not an early-quiet guest result. No guest software, device tree, RAM plan, firmware image or disk image was changed. Both baseband DT nodes remain present.

## Inputs and address convention

Read CLAUDE_HANDOVER.md §20 and T19/T20. Evidence is the supplied scratchpad `probe137/iokit-state-045s.json`, `serial.log`, and `probe-error.txt`. Registry `complete=true`, `errors=[]`. Times in probe filenames are snapshot labels; the probe clock measures host-running elapsed time, not the ARM virtual counter.

Kernel `${REPO_ROOT}/build/ios712-validated/boot/KernelMemory.bin`, SHA256 `bba9a4bace9b505b80c05bc15648b46122c8948f86ac4f0d0625f7804d78d7d8`; runtime DT SHA256 `b75e0054fb52aedc461ecaf88475e93f5b9f6d370c0e0cc040273b4b224d810b`. Used the supplied legacy venv's Capstone. All kernel addresses below are unslid VAs; `fileoff = VA - 0xffffff8000a00000 + 0x1000000`.

The requested `C1-mesa.md`, `C2-iap.md`, and `C3-backlight.md` were absent when checked during implementation and at report preparation. The analyses below were performed directly against the kernel/DT/capture; no claims are attributed to missing reports.

## VERIFIED: captured owners and matching-worker frames

Busy ancestors propagate transitions; they are not independent failures (T20). The four deepest busy nodes are:

| Busy service | Object | Matching worker / captured return PCs |
|---|---|---|
| uart6/AppleSamsungSerial/iap, AppleSimpleUARTSync | `0xffffff8081f19000` | TID137: `0xffffff8000892650`, `0xffffff800088ea18`, `0xffffff800088d4e8`, `0xffffff80008914c0` |
| spi2/AppleSamsungSPIController/mesa, AppleARMSPIDevice | `0xffffff8081fe0b00` | TID245: `0xffffff8000b66830`, `0xffffff800080ef6c`, `0xffffff800080f980`, `0xffffff8000d5d8b8`, `0xffffff8000d5da6c`, `0xffffff8000d5f250`, `0xffffff8000d5e6f4` |
| /baseband, IOPlatformDevice | `0xffffff8081e90700` | TID26: `0xffffff80007ff340`, `0xffffff80007ff224`, `0xffffff8000f0da38`, `0xffffff8000f0e614`, `0xffffff8000f0f1c8`, `0xffffff8000f0fb94` |
| /backlight, IOPlatformDevice | `0xffffff8081e90600` | TID27: `0xffffff80007ff340`, `0xffffff80007ff224`, `0xffffff800081655c` |

The three platform-function workers also have `0xffffff80002ae80c` / `0xffffff80002ae1bc` in their captured chains. These are saved-context frame observations with T20's stated running-thread/stale-context limitations. Their call sites independently agree with the static dependencies below.

## Baseband: a TriStar platform-function dependency, earlier than HSIC

**VERIFIED:** the captured N41 start chain reaches N92 start `0xffffff8000f0d9c0`. At `0xffffff8000f0da28..34`, it requests `function-bb_usb_mux` (string `0xffffff8000f109c2`). Its caller's saved LR is exactly `0xffffff8000f0da38`. The earlier `function-bb_ldo` lookup at `0xffffff8000f0d9ec..fc` has already returned. Thus the observed wait is not a modem-ready GPIO poll, PMU conversion, or HSIC enumeration.

**VERIFIED:** DT `/baseband` remains `baseband,n41`. `function-bb_usb_mux` and `function-bb_uart_mux` refer to dock phandle `0x77`, whereas `bb_on`, `pmu_exton`, and `bb_ldo` refer to PMU phandle `0x47`. `bb_rst`, `radio_on`, `reset_det`, and `hsic_ready` refer to GPIOIC phandle `0x1a`. Baseband's AppleBasebandN41 child exists but the provider retains busy=1: a driver child / user client is not proof that the subclass start has returned.

**VERIFIED:** platform-function lookup `0xffffff80007ff250` reads the first property word, formats `IOFunctionParent%08lX` (`0xffffff80008223e7`) at `0xffffff80007ff2f0..308`, and waits at `0xffffff80007ff33c`. Publication helper `0xffffff80007ff068` builds the corresponding provider property (`0xffffff80007ff0f4..140`). Treat the phandle as raw property bytes for this private name; do not assume host numeric endianness. TriStar's built-in path publishes against its dock and I2C providers at `0xffffff8000e9cae4` / `0xffffff8000e9caf0`, through stub `0xffffff8000e9fa48` to `0xffffff80007ff068`.

**VERIFIED:** serial says `AppleCBTL1610::start: could not read REV`, then `AppleTriStar2::start: could not initialize AppleCBTL1610`. The registry has a tristar I2C nub with no driver child. Prior board code NACKs address `0x1a` on I2C0. AppleCBTL1610 start reads byte register `0x0f` at `0xffffff8000e9e8c8..e4`; a failed transfer returns false at `0xffffff8000e9e914`. Its parent checks IC start at `0xffffff8000e9bb2c..40` and follows the initialization failure path. This is the missing hardware transaction behind the observed mux-provider wait.

**VERIFIED / implementation:** I2C0 `0x20a110000`, slave `0x1a` (wire `0x34/0x35`) now implements byte-prefix reads/writes. Mux registers 1/2 retain the driver's writes (`0xffffff8000e9eff8..f010`, `0xffffff8000e9f1b0..c8`). Revision reads return synthetic `0x20` (the driver publishes the byte; this value is not a recovered chip identity). Status `0x0c` is 4: cable-present inversion at `0xffffff8000e9eba8..b0` yields absent, and identity-ready bit7 is clear. Event register `0x0d` stays zero, mask `0x0e` is stored (`0xffffff8000e9e98c..cc`). GPIO146 reads pulled high for an idle active-low IRQ. No modem/USB/accessory event is invented.

**VERIFIED additional startup requirement:** common accessory-port start calls getSystemID at `0xffffff800088a548..5c`. CBTL's implementation `0xffffff8000e9ea10..68` reads three bytes from `0x20` and requires byte0=`0x74`, decoding `(byte1 << 2) | byte2`. This separate read window returns `74 00 00`. **INFERRED emulation choice:** system ID 0 is a synthetic descriptor, not recovered OTP, an accessory serial, modem IMEI, or an assertion about real n53 hardware identity. Merely answering REV while continuing to NACK this read can still fail parent start: the common start's no-override failure is `0xffffff800088a5c0..604`. No `acc_system_id` boot argument is added. Unsupported register windows still NACK.

**VERIFIED:** PMU and its native child services are published and idle in this snapshot. The first PMU function lookup has returned in the baseband worker. Existing D2045 GPIO/regulator storage and IRQ/ADC behavior are unchanged; there is no evidence for forcing PMU power state, driving modem-ready pins, or manufacturing a PMU interrupt to resolve this wait.

**VERIFIED:** T20's HSIC dependency remains real: `AppleBasebandN92::findHSICPort` at `0xffffff8000f0eaf8`, name construction `0xffffff8000f0eb3c..48`, indefinite service lookup `0xffffff8000f0eb4c..54`. USB PHY reports `hsic disabled`; there is no EHCI/HSIC transport model. N41's later port-enable routine `0xffffff8000f0fc50` calls `0xffffff8000f0e99c`, which calls findHSICPort at `0xffffff8000f0e9b8`. That is not TID26's captured start wait. No fake HSIC service, USB modem, registration, or carrier response is added. **INFERRED:** later CommCenter modem requests can still encounter that separate dependency; passing InterfaceNamer does not establish functional cellular service.

## iap: the same missing dock provider

**VERIFIED:** TID137 waits at the lookup called at `0xffffff800089264c`, whose string at `0xffffff8000894edf` is `function-dock_parent`. The enclosing start begins at `0xffffff8000892604`; failure/parent search follows at `0xffffff8000892650..6c`. It is not waiting for a UART6 receive byte in this capture. The DT's dock is the same phandle `0x77` used by baseband's mux functions.

**INFERRED:** allowing TriStar/common accessory-port start to finish should publish the real dock provider and let this worker proceed. No UART FIFO byte, accessory greeting, UART interrupt or guest registry object is fabricated. The connector remains unplugged; UART2 and all existing serial devices retain their behavior. Host contracts check the idle status/event/mask/system-ID windows and bus isolation.

## Mesa: finish SPI2 DMA, then native failed identification

**VERIFIED:** TID245 waits in the Samsung SPI controller completion loop at `0xffffff8000b66818..34`. Its parent Mesa path sends its initial register/identity request at `0xffffff8000d5f208..250`: 7 TX bytes with RX length at least 23. The SPI provider submits via `0xffffff8000d5d88c..b8`. DT SPI2 `0x20a088000` uses SmartIO endpoints **24 TX / 25 RX**. Before this change only SPI1 endpoints 22/23 have transport completion; SPI2 DMA mode cannot shift and generic endpoints stay armed until stopped. This leaves start's matching worker asleep.

**VERIFIED / implementation:** give SPI2 its own two SioDMA channel states, descriptor validation, immediate op65 submit ACK, deferred-until-clocked op68 completion with original tag and byte count, cancellation and IRQ138. The common engine preserves SPI1/HBPP behavior and channel isolation. Existing PIO's idle-high MISO (`0xff`) is also used for SPI2 DMA. GPIO79's idle IRQ sample is high. The board does not claim successful Touch ID identification, firmware, enrollment, SEP pairing, or authentication.

**VERIFIED:** the native caller at `0xffffff8000d5e6f8..718` requires reply bytes `[0]=0x80`, `[1]=0x20`, `[3]=0x10`. An all-FF idle response fails those checks. The retry count is bounded to three at `0xffffff8000d5e744..74c`, with power cycles through `0xffffff8000d5f3e4` (100 ms delay at `0xffffff8000d5f438..43c`). The exhaustion path is `0xffffff8000d5e750..7d0`. **INFERRED:** the matching worker should unwind through that native failure path once transport completes. This needs a guest check; no arbitrary sensor success payload is substituted to manufacture a working biometric device.

## Backlight: LM3534 must answer the native four-register transaction

**VERIFIED:** TID27's return PC `0xffffff800081655c` is immediately after lookup of `function-backlight_enable` at `0xffffff800081654c..558` (string `0xffffff800082488f`). DT `/backlight` points it at LM3534 phandle `0x4a`. `/arm-io/i2c0/lm3534` is `backlight,lm3534`, address **0x63**, and its registry nub has no driver child. The personality is AppleLMBacklight (kernel string `0xffffff8001282d84` onward).

**VERIFIED:** AppleLMBacklight start `0xffffff8000f208b8` chooses prefix 0 for LM3534, then reads **four bytes at register 0** at `0xffffff8000f2095c..98`. An I2C error returns false. It publishes the function provider at `0xffffff8000f2099c..a4`; function creation recognizes the `bklE` selector at `0xffffff8000f20a4c..64`. Enable/disable modifies control byte bit0 and writes the four-byte block at `0xffffff8000f20b08..94`. LP8559's alternate prefix/one-byte path is not selected by the supplied DT.

**VERIFIED / implementation:** I2C0 slave 0x63 (wire `0xc6/0xc7`) now supplies four zero-reset control bytes with sequential read/write storage. No revision/brightness value or display pixels are manufactured. Controller reset clears FIFO/prefix state while preserving external peripheral registers; machine reset clears those registers. Other addresses/buses still NACK. **INFERRED:** successful native start publishes the actual backlight function and releases TID27's matching work.

## Probe crash

**VERIFIED:** supplied traceback aborts display_probe's user-expected instruction check at the boot-wait hook. Breakpoints are VA-global: another process may have different executable bytes at the same user VA. The primitive `instruction_mapped` remains strict by default; its optional mismatch callback logs and returns false for this use. display_probe records the last mismatch in `user-hook-mismatch.json` and all hits in `user-hook-mismatches.jsonl`, including hook, PC, expected/actual bytes, identity, elapsed, and `action=skip-hit`. It single-steps that hit and resumes before event interpretation or hit-cap accounting. The breakpoint/verified bytes remain installed for a later valid process hit. Empty/unmapped reads retain their previous retry behavior. No guest memory is written.

## Host verification and maintainer acceptance

- `python3 backend/busy_i2c_contract_test.py`: PASS. Compiles the actual board C with PMU/clock stubs. Separate baseband, iap, and backlight cases cover mux state, repeated START/STOP, FIFO occupancy/empty, system-ID marker, disconnected status/zero events, mask storage, read-only status, unsupported windows, boundary bursts, controller reset, PMU/Chestnut regression and I2C1 isolation.
- `tests/test_spi_dma.py`: PASS. Actual extracted C, bounded host RAM, SPI2 7-byte TX / 23-byte RX, both completion tags/counts, IRQ/W1C, delayed submit kick, cancel, MMIO descriptor rejection, and a concurrent SPI1 TX remaining armed across SPI2 RX completion. Existing SPI1 firmware/touch contracts still run.
- `python3 -m unittest discover -s tests -p 'test_*.py'`: **146 tests OK, 4 skipped**. Includes mismatch -> valid-hit regression, retaining strict mode and unmapped-read behavior.
- `git diff --check`: PASS. `/* The TA command whose parameter buffer` remains in s5l8960x.c.

Rebuild the backend by the normal maintainer procedure. Use the same no-Oscar plan and identical diagnostic disk sources, SMP/MTTCG/Cocoa environment, and hooks as probe137. No new RAM plan is needed for these MMIO-only changes. For example, with normal diagnostic clone inputs supplied as in T20:

```sh
VPHONE_TCG_THREAD=multi VPHONE_DISPLAY=cocoa \
python3 backend/display_probe.py \
  --plan backend/evidence/keybag-contract-20261015/boot-plan-smp-nooscar/boot_plan.json \
  --output /tmp/t21-busy-devices --seconds 100 --stop-condition duration \
  --user-hooks backend/boot_wait_hooks.json --max-hits boot_wait_=32 \
  --snapshot-at 45
```

Require `iokit-state-045s.json` complete=true and **no busy leaves**, rather than merely disappearance of one leaf. Check the four start chains have returned; expect Mesa identification failure/retries instead of a DMA sleep, a TriStar driver/dock function provider, and an AppleLMBacklight driver/function provider. Require configd's **QUIET** before its timer and CommCenter past InterfaceNamer (pair its boot_wait_interfaces_begin/done, ensure hooks are unsaturated); a 60-second TIMEOUT is not success. Check serial for new panics/restarts and inspect any remaining kernel matching stacks. If baseband next waits for hsic-baseband, distinguish that newly reached modem/USB dependency from the mux start wait fixed here. Runtime acceptance remains unverified until this maintainer run.
