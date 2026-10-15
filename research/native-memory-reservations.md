# Native n53ap PRAM, STRAM and ANS reservations

Evidence comes from the SHA1-verified, unmodified iBoot-1940.10.58 decrypted artifact at `research/firmware-artifacts/iBoot.n53ap.RELEASE.im4p.decrypted.bin`. The original IPSW DeviceTree contains zeroed reservation fields; iBoot fills them before handing control to XNU.

| DeviceTree node | Selector | Physical base | Size | Range end |
| --- | ---: | --- | --- | --- |
| `pram/reg` | 0 | `0x83f7f7000` | `0x4000` (16 KiB) | `0x83f7fb000` |
| `stram/reg` | 2 | `0x83f7fb000` | `0x1000` (4 KiB) | `0x83f7fc000` |
| `arm-io/ans` firmware region | 3 | `0x83f800000` | `0x800000` (8 MiB) | `0x840000000` |

These are actual constants in the one-GiB n53ap firmware. The 16-KiB interval from STRAM end to ANS base is not explained by this evidence and must not be silently folded into PRAM or STRAM. A 20-KiB PRAM region conflates two distinct reservations.

**Correction on 2026-10-13:** the earlier report called selector 3 a framebuffer without tracing its consumer. iBoot `0x13b7c` finds `arm-io/ans`; its `region-base` lookup at `0x13b8c` is followed by selector 3/base accessor and a 64-bit store at `0x13bbc`. The `region-size` lookup at `0x13bc0` is followed by selector 3/size accessor and a store at `0x13bf0`. This establishes ANS ownership. The loader now places diagnostic framebuffer RAM separately among its lower boot allocations. Actual native display allocation remains unverified.

## DeviceTree writes

- At file offsets `0x13224`/`0x13228`, iBoot finds `pram`; it looks up the `reg` property at `0x13234`–`0x13250`.
- At `0x13258`, it selects region 0 and calls physical-base accessor `0x19210`. `0x13264` stores the returned 64-bit physical address to `reg[0]`.
- At `0x13268`, it selects region 0 and calls size accessor `0x192c4`. `0x13274` stores the returned 64-bit size to `reg[1]`.
- At `0x132dc`/`0x132e0`, iBoot finds `stram` and obtains `reg` at `0x132ec`–`0x13308`.
- At `0x13310`, it selects region 2 and calls `0x19210`; `0x1331c` writes its address.
- At `0x13320`, it selects region 2 and calls `0x192c4`; `0x1332c` writes its size.

Both properties are two little-endian 64-bit values, matching the native ARM64 stores. These fields are kernel handoff data and must be filled in a derived runtime DeviceTree while preserving the original firmware bytes.

## Accessor and jump-table proof

`0x19210` calls base selector `0x19168`. For supported selectors, that routine initializes x0 to `0x83f7f7000`, then dispatches through six signed 32-bit relative entries beginning at `0x191f8`:

```text
selector 0 -> 0x19174  RET, preserving initialized PRAM base
selector 1 -> 0x1919c  bootloader heap region
selector 2 -> 0x191bc  MOV 0x800000000; MOVK 0x3f7f,LSL16; MOVK 0xb000; RET
selector 3 -> 0x191cc  MOV 0x800000000; MOVK 0x3f80,LSL16; RET
selector 4 -> 0x191d8  monitor physical base from initialized globals
selector 5 -> 0x191e8  SEP physical base from initialized globals
```

`0x192c4` calls size selector `0x19240`, which initializes x0 to `0x4000` before dispatching through the six entries at `0x192ac`:

```text
selector 0 -> 0x1924c  RET, preserving initialized 16-KiB PRAM size
selector 1 -> 0x1926c  bootloader heap size
selector 2 -> 0x1927c  MOV x0,#0x1000; RET
selector 3 -> 0x19284  MOV x0,#0x800000; RET
selector 4 -> 0x1928c  monitor-size from initialized globals
selector 5 -> 0x1929c  sep-size from initialized globals
```

Kernel usable-memory sizing additionally deducts `0x805000` at `0x1914c`. That subtraction alone does not establish the locations above; the selector constants and actual DeviceTree writes do. For native DRAM base `0x800000000`, SEP 6 MiB and monitor 2 MiB, the previously proven kernel physical base is `0x800800000`, with usable memory size `0x3effb000`. The reported usable-memory end is STRAM base, while PRAM lies inside that span and requires its own reservation handling.

## Observed failure addressed

The backend trace showed that XNU uses `stram/reg` for its sleep-token buffer. Leaving this as `(0,0)` caused a zero-size mapping whose virtual address was subsequently reused by UART; the eight-byte `RUNNMOSX` token copy then targeted the UART mapping and faulted. A speculative fixed 16-KiB STRAM allocation is unnecessary: the native iBoot reservation above is recoverable and should be used.

This report records a firmware handoff contract, not proof of userspace, storage or graphics support. No guest binary or original DeviceTree was modified during this analysis.
