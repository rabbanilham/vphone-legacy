# T25: detached USB complex, OTG handshakes and HSIC aperture fault

2026-10-22, `wt/tristar-next`, following merged T23/T24 and the corrected deviceless EHCI schedule contract. No QEMU build or execution, and no guest software, kernel, DT or disk changes. Host validation is recorded below. **The maintainer's no-panic / no-busy-leaves boot acceptance is still unverified.**

## Probe148 fault: HSIC port reg[0], not OTG+0x1c060

All instruction addresses below are unslid kernel VAs with prefix `0xffffff8000000000` omitted. Read the supplied validated kernel with file offset = VA − `0xffffff8000a00000` + `0x1000000`, using the legacy venv's Capstone. Read the runtime DT from the legacy checkout; this worktree has no local copy of that build artifact.

`11078d0` is the register OR helper of **AppleS5L8960XUSBHSICPort**, whose class-name string is at `1107df0`. It is not an AppleSynopsysOTGDevice method. Probe148 captures x1=`ffffff80a6ff2060`, x2=0, x3=3:

```
11078d0 ldr w8, [x1, w2, uxtw]
11078d4 orr w8, w8, w3
11078d8 str w8, [x1, w2, uxtw]
```

The caller at `11076e0..6f8` loads vtable slot 0x6e8, loads x1 from object+0x220, and passes offset zero / mask 3. Its return PC is the captured LR `11076fc`. Slot 0x6e8 in the HSIC class's vtable at `1108100` resolves to `11078d0`. At `1107400..42c`, the same class maps its **own DT memory index 0**, saves the map at +0x228 and its VA at +0x220. The subclass init/start body is `1107280..75e4`, and the power callbacks are `1107678..78ac`.

The HSIC WLAN child's reg[0] is complex +0x60/0x10 -> **PA 0x20c000060**, consistent with the FAR's page offset 0x60. The baseband child is +0x50/0x10. Thus the evidence identifies the WLAN HSIC control word, with the page-offset correspondence supplying the physical-child inference; no captured page table or runtime map dump independently measures that PA. Both exact resources are now covered. The OTG device's printed VA (`ffffff80a6fd6000`) is a different mapping in a different object. Subtracting it from this FAR produces 0x1c060 but does not identify a core register.

## Complete DT resource inventory and responders

Translation: arm-io child 0 -> PA 0x200000000, USB complex child 0 -> arm-io +0xc000000. The complex's `ranges` length 0x600000 describes translation, **not** an MMIO responder. It has no own `reg`. All descendants were recursively enumerated, including grandchildren under EHCI. There is only one OHCI node in this DT.

| DT resource | Physical base / size | Driver and responder |
|---|---|---|
| usb-device reg[0] | 0x20c100000 / 0x10000 | AppleSynopsysOTGDevice; new detached device-mode core, full reservation with stored unknown words |
| usb-device reg[1] | 0x20c000010 / 0x10 | Platform USB control map, new independent zero-reset storage |
| usb-ehci0 reg[0] | 0x20c200000 / 0x10000 | AppleS5L8960XUSBEHCI -> AppleSynopsysUSBEHCI -> AppleUSBEHCIARM -> AppleUSBEHCI; existing engine + T24 lower-priority reservation backing |
| usb-ehci0 reg[1] | 0x20c000000 / 8 | Existing control/config storage, first half of 16-byte region |
| usb-ehci0/hsic-baseband reg[0] | 0x20c000050 / 0x10 | AppleS5L8960XUSBHSICPort -> AppleUSBHSICPort; new independent storage |
| usb-ohci0 reg[0] | 0x20c300000 / 0x10000 | AppleS5L8960XUSBOHCI -> AppleUSBOHCI family; QEMU sysbus OHCI, one disconnected port, IRQ164; lower-priority storage for reservation holes/tail |
| usb-ehci1 reg[0] | 0x20c400000 / 0x10000 | Existing EHCI engine and T24 backing |
| usb-ehci1 reg[1] | 0x20c000008 / 8 | Existing control/config storage, second half |
| usb-ehci1/wlan reg[0] | 0x20c000060 / 0x10 | Same HSIC port class; new independent storage, probe148 fault |
| arm-io/otgphyctrl reg[0] | 0x20c000020 / 0x20 | AppleS5L8960XUSBPhy -> AppleUSBPhy; existing board peripheral storage |
| arm-io/otgphyctrl reg[1] | 0x20e031000 / 0x1000 | Same PHY driver; **already inside PMGR** 0x20e000000/0x40000, with ordinary register storage at PMGR+0x31000 |

The two EHCI0/OHCI0 `*-port1` children have no reg. No other USB-complex child has reg. `usb_ctl` is a phandle, not a register address. The baseband/WLAN nested resources use the USB-complex address domain (their parents do not introduce another translated range). AppleUSBHSICDevice and later AppleBCMWLAN/AppleUSBHSIC USB clients consume port/provider services; their USB endpoint traffic does not introduce an additional board MMIO reservation. The matching personalities and class/superclass code were inspected, rather than treating every occurrence of USB in the kernel as another physical controller.

The added AUX resources total three 16-byte control regions and one 64-KiB OHCI reservation. They are distinct from OTG's 64-KiB region. No added responder covers complex gaps such as +0x40..4f or +0x70 onward. The existing PHY and PMGR mappings are reused. Existing non-USB board regions are unchanged.

OHCI uses the pinned QEMU engine, explicitly selected with `USB_OHCI_SYSBUS` in the backend build script. It supplies HcRevision=0x10, controller reset, HCR self-clear, interrupt/W1C behavior, frame operation and a one-port disconnected root hub (reset descriptor A 0x201 and port status 0x100). Like both EHCIs, its `ohci.as` points to the private USB DRAM AddressSpace from T24, with no system-wide low RAM alias. The full DT tail responds below the actual engine. No external device is attached.

## Static start/init and poll inventory

Inspected executable portions of OTG `e6a07c..e6adfc` (init/start/endpoint discovery), `e6b3dc..e6c940` (interrupt/init), `e6cde0..e6d23c` (NAK/FIFO flush), `e6e4fc..e6e824` (global flush), `e7035c..e70ddc` (reset/FIFO programming/endpoint stop), HSIC subclass `1107280..1107c34`, common HSIC `efe000..f05110`, S5L OHCI `1114ccc..1114e60`, common OHCI register helpers around `10f2000` onward, and PHY `122bbd8..122c92c`. T24's EHCI inventory continues to apply. Data/vtables in broader intervals were not counted as instructions or MMIO.

| Map / register | Native sites and behavior | Model |
|---|---|---|
| OTG reg[0] map / saved VA | `e6a768..794`, provider map index 0 -> object+0x1d8 / +0x1e0 | Full DT range |
| GAHBCFG +8, GUSBCFG +0xc | `e7052c..548` sets DMA/burst and USB config | Stored controls, global interrupt enable gates IRQ162 |
| GRSTCTL +0x10 CSftRst bit0 | `e703cc..e70448`, up to five timeout-bounded reset attempts | Write completes immediately, bit0 clears |
| GRSTCTL AHBIdle bit31 | `e704b0..e70504`, `e6e688..6dc` | Always set on this idle, no-DMA device bus |
| GRSTCTL RX/TX flush bits4/5 and TXFNUM[10:6] | `e6d120..190`, `e6e6dc..72c`; also reset helpers `e703cc` | Command bits self-clear, AHBIdle remains set; no fabricated traffic |
| GINTSTS +0x14 / GINTMSK +0x18 | `e6b420` onward, `e6c1bc`; later global NAK polling | Device-mode bit0=0; no session/reset/enumeration/RX/endpoint-transfer events; NPTXFEMP bit5=1; status levels cannot be cleared by W1C |
| DCTL +0x804 global IN NAK set/clear bits7/8, OUT bits9/10 | `e6ce00..6ceac`, `e6e584..824`, `e70920..9ec` | Commands self-clear; DCTL status bits2/3 and GINTSTS effective bits6/7 track requests and clearing |
| GRXFSIZ +0x24, GNPTXFSIZ +0x28, DIEPTXF1..6 +0x104..118 | `e7054c..e706f8` writes and optional debug readbacks | Writable FIFO partitions; reset partitions fit 0x820 words |
| GSNPSID +0x40 | `e70704..710`, low16 cached at object+0x208 | RO 0x4f54280a (modeled supported revision) |
| GHWCFG1..4 +0x44..50 | Capabilities supplied for normal DWC register consumers; native endpoint count/directions actually come from DT | RO direction bitmap 0x264; 7 endpoint numbers plus EP0, internal DMA/UTMI/dynamic FIFO, total 2080 words, dedicated FIFO capability |
| DCFG +0x800, DSTS +0x808 | `e70700`, `e6e974`, speed/status at `e6d5e0`, `e6e3e4` | DCFG stored; DSTS RO suspended=1, no enumerated frame progression |
| DIEPMSK +0x810, DOEPMSK +0x814, DAINT +0x818, DAINTMSK +0x81c | `e6c890..8a4`, `e6b514`, `e6c3bc` | DAINT derived from masked endpoint causes; IN/OUT aggregate levels feed GINTSTS and IRQ162 |
| IN EPn +0x900+0x20n / OUT +0xb00+0x20n | Control +0, interrupt +8, transfer size +0x10, DMA +0x14; diagnostics `e6fffc..e70064` | All 16 register banks respond, transfer size/DMA retain writes, EPINT W1C |
| Endpoint disable bit30 / enable bit31 / SNAK bit27 / CNAK bit26 | OUT poll `e70a20..af0`, IN stop/NAK helpers near `e6cf00`; EPINT bit1 polled | Disable consumes enable, sets EPDISBLD bit1; NAK status bit17 follows command; no transfer-complete bit invented |
| IN DTXFSTS +0x918+0x20n | FIFO-space consumers / endpoint diagnostics | Empty reset FIFO space (256 words EP0, 32 other FIFOs) |
| GOTGCTL +0 / GOTGINT +4 / RX status +0x1c,+0x20 | Normal DWC global map | B connector / invalid sessions 0x10000; no OTG event or RX packet; GOTGINT W1C |
| Vendor/debug offsets e.g. +0x440, any unlisted word | `e6ddb8..de04`; driver uses additional revision/debug paths | Storage within the exact reservation; no MMIO holes |
| HSIC reg[0]+0 and +8 | `11076e0..7758`: OR3 at 0, optional cfg write at +8, clear reset/control masks with fixed 20/1000-unit delays | Independent retained control words; not a forced connected/readiness status |
| HSIC link resume / PORTSC | `1107a34..7c18`, poll bit6 via inherited status accessor | Actual EHCI PORTSC engine; no resume/connect event manufactured for an absent port |
| PHY maps 0 / 1 | `122bf54..bf78`, `122bfcc..bff4`; control RMW at +0/+4/+8/+0xc on first map and +0 on second | Existing zero-reset storage and fixed driver delays, no additional busy poll found |

The core is deliberately a detached **device-mode** implementation; the pinned upstream DWC2 engine is host-only and cannot supply these device registers. Reset capability numbers are explicit modeled hardware choices, not a claim to know A7 OTP/reset dumps. Unknown register storage prevents reserved-aperture holes from causing aborts, without asserting undocumented functions complete. Writes to host registers cannot attach a host or cable. There is no device packet/DMA engine because no cable is present.

## Subsequent waits: VBUS and no baseband link

D2045 already responds on I2C0 address 0x74 with a native 16-bit register prefix. Its event registers 0x50..5b start clear, status block 0x60..6b starts clear, and events are masked at 0x70..7b. No VBUS insertion interrupt is fabricated. TriStar continues to report detached status bit2=1, accessory bit7=0 and no events.

**New correction:** PMU ADC channel **9** now returns zero rather than the generic 2048 sample. The kernel's channel/name dispatch at `105460c..648` maps channel9 to string `vbus` (`105f76c`); `1053d90..da8` explicitly requests channel9, and `105354c..59c` decodes its conversion. The ordinary conversion still completes through the existing timer (0x500 bit7 clears; event0x5b bit1 is raised) and the native event handler at `105152c` wakes the conversion wait. The synchronous 0x541/542/543 path also returns zero and clears busy. This avoids a false analog VBUS indication while retaining the real conversion-completion contract. Other battery/thermal samples are unchanged. PMU charger/status reads including 0x4c0 remain zero unless explicitly configured; no charger/source is fabricated.

The native baseband start's formerly missing TriStar mux function remains available with `VPHONE_TRISTAR=1`; there is no new host-ready/device-ready GPIO feedback. DT device-ready pins164 (baseband),167 (WLAN) and baseband HSIC-ready17 have no external modeled assertion. EHCI ports retain CCS=0, so USB enumeration is not started for a nonexistent modem. HSIC subclass control map access no longer aborts; it can run its fixed-delay power sequence and publish its native service. Common HSIC start/event handling is asynchronous. Its explicit transition wait at `f03cec..e84` has a computed deadline and a 500-unit timeout diagnostic/cleanup, rather than a register requiring a fabricated ready bit. The subclass resume wait `1107aac..ad4` also compares a deadline and falls through to cleanup.

The known `AppleBasebandN92::findHSICPort` (`f0eaf8`, matching-service call `f0eb54` with timeout -1) remains an **indefinite service dependency** if the platform port fails to publish. A memory responder cannot prove that registry publication or global IOKit quiet succeeds. The implementation removes the statically identified MMIO aborts and polling failures while presenting disconnected hardware; it does not claim to emulate a modem or prove all asynchronous driver matching is quiet. The requested 45-second registry snapshot is still the deciding evidence for no busy leaves, and should distinguish a missing service from a physical-link wait if it fails.

## Validation and maintainer procedure

Host tests compile the actual OTG callbacks and board resource construction against bounded API stubs; they do not build or run QEMU. The callback test uses AddressSanitizer/UndefinedBehaviorSanitizer and exercises all 16 IN/OUT banks, reset/flush pulses, RO values, W1C, global NAK effective set/clear, endpoint disable, interrupt aggregation/masking and partial accesses. A separate compiled wiring test verifies the exact control/HSIC/OHCI reservations, priorities, one-port OHCI IRQ164 and its private DMA address space. The PMU I2C test checks VBUS=0 in both conversion paths, conversion completion, event read-clear and detached digital status. Protected comment `/* The TA command whose parameter buffer` remains present.

`backend/usb_otg_contract_test.py` shares its transactions with the host callback test and builds only a synthetic RAM payload for the maintainer. It covers reset values, native polled bits, endpoint0/1/15 banks, W1C, reset reproducibility, every additional control/HSIC range, existing PHY/PMGR ranges, reservation end words, and OHCI reset/disconnected root-hub readback. It reuses the EHCI QMP harness via an optional fixture/message parameter; the existing EHCI invocation is unchanged. The payload is checked against its result-buffer boundary. **No native contract was executed here.**

After rebuilding, run:

```
python3 backend/ehci_contract_test.py
python3 backend/usb_otg_contract_test.py
```

Then run the original unmodified guest plan with **VPHONE_TRISTAR=1**, boot to **120 seconds**, **--snapshot-at 45**. Require no kernel panic and an error-free `iokit-state-045s.json` with **no busy leaves**. Those runtime results are not established by the host suite.

Final host validation: `python3 -m unittest discover -s tests -p 'test_*.py'`: **156 tests OK, 4 skipped**. `git diff --check`: passes. Maintainer native contracts and guest boot were not run.
