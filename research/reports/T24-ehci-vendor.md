# T24: EHCI vendor aperture holes and USB DMA wiring

2026-10-22, `wt/tristar-next`, following merged T23. Host suite: **151 tests pass, 4 skipped**. No backend build or emulator execution was performed. The maintainer's 120-second TriStar boot / 45-second snapshot acceptance remains outstanding. Guest kernel, DT, software and disks were only read, never changed. `VPHONE_TRISTAR` behavior is unchanged.

## Fault identification

Disassembly used the supplied validated `KernelMemory.bin`, file offset = VA − `0xffffff8000a00000` + `0x1000000`, and the legacy venv's Capstone. All abbreviated instruction addresses below have prefix `0xffffff8000000000`.

The panic is in the Synopsys **power-transition method at `110c738`**, invoked during start, rather than directly in `AppleSynopsysUSBEHCI::start` (`110c568`). At `110c7d0` it loads the saved **reg[0] IOMemoryMap at object+0x3a8**, calls its `getVirtualAddress` slot 0x70, then:

```
110c7d0  ldr x0, [x19, #0x3a8]
110c7d4  ldr x8, [x0]
110c7d8  ldr x8, [x8, #0x70]
110c7dc  blr x8
110c7e0  ldr w8, [x19, #0x2784]
110c7e4  str w8, [x0, #0x2c0]
```

`probe147/serial.log` captures x0=`0xffffff80a7c28000`, w8=4 and FAR=x0+0x2c0. The map's origin is proven by `eefac8..eefae0`: provider `mapDeviceMemoryWithIndex(0,0)`, saved at +0x3a8. This is **not** the index-1 control map (saved at +0x2788; its VA at +0x2790).

The value is particularly diagnostic. `110c638..110c670` reads each child's port-type; types 3 and 4 enter `110c5d8..110c5ec`, which ORs `1 << (port-1)` into +0x2784. Thus +0x2c0 is a **Synopsys HSIC/alternate-port selection mask**, not a recovered burst or packet-buffer threshold. The mask 4 identifies EHCI0's HSIC baseband on port 3; EHCI1's WLAN on port 1 would contribute 1. With the supplied DT translations, the target is consequently **PA `0x20c2002c0`**, EHCI0 reg[0]+0x2c0. This identification uses driver dataflow and DT topology; the logs do not contain page tables that independently measure the dynamic VA→PA mapping. Both controllers receive the fix.

The offset is **inside 0x1000**, contrary to the proposed outside-aperture explanation. QEMU's `EHCIState.mem` is a *container*, not an opaque 0x1000 MMIO implementation. `usb_ehci_realize` adds only capability, operational and port subregions. There is no responder at +0x2c0, so even the inner part of the DT reservation has holes. Extending only a tail above +0x1000 would leave this exact abort intact.

## Static access inventory

Scanned the executable paths in `110abfc..110b3dc` (ARM controller start, child initialization and register helpers), `110c568..110c998` (Synopsys start/power methods/helpers), `1111928..1111e00` (S5L start and power/init), `122bbd8..122c92c` (S5L PHY start/power/config/helpers), generic PHY start at `e61ee4` and its child publication paths, and the common EHCI controller code `ee0000..ef7b00`. Data/header bytes in these broader intervals are not treated as instructions proving MMIO. Register bases were tracked from saved maps, rather than classifying every object-field load as MMIO.

| Base / mapping | Offsets used | Sites and meaning |
|---|---|---|
| EHCI reg[0] | 0,4,8,0xc | `eefb10..20`: four capability words. CAPLENGTH selects the operational base. |
| EHCI reg[0] | **0x2c0** | `110c7e4`: HSIC port mask, written during power-up. No read/poll of this register in these paths. No other direct Synopsys vendor offset was found. |
| EHCI operational base = reg[0]+CAPLENGTH | 0,4,8,0xc,0x10,0x14,0x18,0x40 | Command, status, interrupt enable, frame index, segment, periodic base, async base, configure flag. Representative power/setup at `eeb1bc..eeb28c`; reset at `ef0300`; schedule controls at `ef04cc` and `ef0640` onward. Standard QEMU engine handles these. |
| EHCI operational base | 0x44+4*(port−1) | `110b04c..110b060` forms each child's PORTSC map; common EHCI root-port methods also index PORTSC. DT ports 1/3 (EHCI0) and 1 (EHCI1) fit existing 3/1-port engine topology. |
| S5L EHCI reg[1] | **0,4 only** | `1111b90..1111ba0` sets clock bit 1, `1111c0c..1111c1c` clears it; `1111cbc..1111ccc` ORs the low-nibble `cfg-incr` at +4. No poll. |
| S5L PHY reg[0], VA saved at +0xd0 | 0,4,8,0xc | `122c324..122c3b4`: reset/control RMW and config words; `122c628/67c` uses +4 bit1; `122c76c/78c` and restore paths use +8/+0xc. |
| S5L PHY reg[1], VA saved at +0xd8 | 0 only | `122c044..05c`, `122c308..318`, `122c4d4`, `122c748/7f8`: control RMW. PHY maps are independent provider maps, not EHCI reg[0]. Existing PHY/complex backing is preserved. |

The DT's EHCI index-1 sizes are **8 bytes each** at `0x20c000000` and `0x20c000008`. Together the existing 16-byte backing is exactly correct. Expanding it would conceal accesses to other USB functions. PHY storage at `0x20c000020..3f` is unchanged. DT HSIC child resources at complex +0x50/+0x60 and the OHCI/device controllers are distinct resources, not the faulting EHCI vendor block; this patch does not install a USB-complex-wide catch-all or invent endpoint readiness.

The relevant register loops are standard EHCI loops: HCRESET bit1 clears (`ef0388..ef03b4`), halt status bit12 sets on stop (`ef0560..ef0578`), and schedule enable/status transitions follow the engine's frame processing. Disconnected ports retain CCS/PE clear. The PHY transitions in the scanned S5L methods use fixed delays and explicit control-bit clear/set operations, not a vendor status polling loop. No additional vendor busy-bit completion value was justified by this scan.

## Model changes

Two zero-reset, byte-addressed storage regions span exactly each DT reg[0] reservation: `0x20c200000..20c20ffff`, `0x20c400000..20c40ffff`. They are system-memory subregions at priority **−1**, below the normal EHCI engine at priority 0. This covers holes *inside* the engine container and the tail, while preserving real capability RO, command reset/run, status W1C, ports, timing, DMA and IRQ behavior. The ordinary bridge callbacks retain vendor configuration; standard registers never fall through to those bytes. Whole-machine reset clears both backings through the existing bridge reset loop. Vendor storage persistence across HCRESET is a bounded model choice: the recovered mask has no demonstrated HCRESET side effect. This is not a claim to simulate undocumented Synopsys registers throughout the reserved tail.

The control window stays 16 bytes and keeps four independent words. No invented USB device, cable event, baseband-ready GPIO or TriStar default enable was added. The protected `/* The TA command whose parameter buffer` comment remains in place.

## Next steps in the driver and DMA correction

Common EHCI start allocates the periodic frame list at `eefe2c`: a 0x1000-byte buffer, physical mask `0xfffff000`, then 1024 32-bit entries at `eeff30..eeff68`. In power-up `eeb1fc..eeb210`, it programs operational+0x18 from the async head's 32-bit address; `eeb218..eeb238` obtains the periodic buffer's physical segment and writes operational+0x14 through the **32-bit store helper** (`110c970`). Consequently the bus pointers presented to this controller contain the low 32 bits; AP DRAM is `0x800000000..0x83fffffff`, whose low 32-bit offsets are `0..0x3fffffff`.

The supplied runtime DT has **no USB iommu-parent, dma-ranges, mapper child, or USB DART**. The arm-io parent has `iommu-present`, but the explicit DART/mapper references are for JPEG, scaler and ISP; this flag alone is not evidence of a USB translation table. No USB DART table walk was invented. Native DMA values have not yet been observed in a successful boot; interpreting the truncated pointers as DRAM bus offsets is the board wiring inference to verify in that run, rather than a measured hardware bus trace.

Pinned QEMU sysbus EHCI defaults `s->as` to `address_space_memory`, but `get_dwords`, `put_dwords` and packet scatter/gather all use 32-bit bus addresses through that address space. This board has no low system RAM mapping. Thus a periodic/async fetch would otherwise hit unmapped system memory; upstream `get_dwords` even discards the DMA transaction result, risking bogus descriptor walks instead of a clean diagnostic.

Both engines now share a **private USB DMA AddressSpace**, containing a 1 GiB alias of machine RAM at bus offset 0, with a 4 GiB container. This supplies coherent descriptor reads/writeback without exposing a low RAM alias to CPUs or other devices. The rest of the bus aperture remains unmapped. QEMU's 32-bit DMA advertisement is unchanged. This bounded direct bus mapping does not implement a USB IOMMU or claim future attached endpoint operation.

## Validation and maintainer acceptance

- `python3 -m unittest discover -s tests -p 'test_*.py'`: **151 tests OK, 4 skipped**. These are host tests; none builds or boots the backend.
- `tests/test_ehci_model.py` compiles the actual board construction/storage snippets against bounded host stubs. Checks include private DMA address-space assignment on both controllers, alias bounds/target, full reservation size and lower priority, vendor/tail readback, partial writes, independent control/config words, port layout and IRQ isolation.
- `backend/ehci_contract_test.py` now checks hole +0x64, actual fault +0x2c0, +0xffc, +0x1000 and final word +0xfffc on both controllers, vendor/control persistence over HCRESET, and existing standard register contracts. It also constructs a terminated periodic frame list plus a valid async QH/IN qTD in AP RAM, programs their low bus addresses, enables both schedules and requires real qTD writeback (inactive + halted + transaction error) and USBERRINT for the absent endpoint. This detects a DMA alias regression that register readbacks cannot. **Not executed here.** Its payload is 4640 bytes, below its result buffer; hand-encoded loop/bit-test instructions were decoded with Capstone to check their targets.
- `git diff --check`: passes. No guest writes, QEMU build or emulator execution.

Maintainer: rebuild, run `python3 backend/ehci_contract_test.py`, then boot the original plan with `VPHONE_TRISTAR=1` to **120 seconds**, `--snapshot-at 45`. Require no panic and complete, error-free `iokit-state-045s.json` with no busy leaves. These runtime outcomes are **not yet established**. If DMA setup fails before scheduling, capture the allocation result/physical segment and the programmed frame/async base; if another controller starts, identify its provider map rather than widening the whole USB complex. The present fix removes the proven vendor write abort and the statically identified missing USB DMA view; it does not prove baseband/iap completion.
