# Native ANS utility formatting and a truthful virtual utility boundary

## Artifact and evidence boundary

Target: `build/ios712-validated/ans-firmware.bin`, 0x39000 bytes, SHA256 `c066bf9d1edef96e59fe618eb30933847ba00ac82bff2b8a7b3ef0b7a32c9b7e`. This is the exact unmodified ARM32 ANS image extracted from audited native n53ap iBoot, not XNU or an AP driver. Addresses below are MCU-relative byte addresses/file offsets in this image. The metadata RAM addresses exceed the source extent and identify runtime objects, not source bytes containing a saved NAND format.

REA's native provider was unavailable in the parent investigation. The retained evidence is **local Capstone 5.0.7 static Thumb disassembly**, not REA decompilation or observed IOP execution. [Bounded instruction/byte evidence](firmware-artifacts/utility-format-native-disassembly.txt) retains the source digest, exact bytes, branch tables as data, and native strings. Firmware bytes, C, loader and disk images were not changed by this investigation.

## The exact native `utilFormatted` predicate

Geometry's native internal-type 0x16 case starts at **0xe5ae**. It loads `uint32[0x65c58+8]` at **0xe5e0**, turns it into a boolean at **0xe5e6, 0xe5fc–0xe600**, and stores that boolean to the AP descriptor **+0x40** at **0xed0c**. The separate `lbaFormatted` value comes from `uint8[0x909c0+0x4dc]` at **0xe5f6**, via stack +0x2c, and reaches descriptor **+0x3c** at **0xed06–08**. Descriptor +0x34 is 0x1000 bytes/LBA at 0xecfc–0xed00.

The native predicate is therefore:

```text
utilityFormatted = (nativeUtilityState.word[+0x08] != 0)
lbaFormatted     = nativeLBAState.byte[+0x4dc]
```

It is **not** a test of utility format epoch, SysConfig contents, an effaceable key, or merely the existence of a host file.

Native state initializer **0xffc4** supplies the meaning of +8. It computes:

```text
P = uint32[0x8eb40 + 0x7b8]
reservedSlots = uint16[0x7b0a4 + 0x9a] + uint8[0x7b0a4 + 0xa1]
if P != 0:
    nativeUtilityState[+8] = reservedSlots / P
    require reservedSlots % P == 0       # otherwise native assertion at 0x10050
else:
    nativeUtilityState[+8] = 0
nativeUtilityState[+0xc] = uint32[0x8eb40+0x7bc] - 1 when P != 0
```

Anchors: **0xffda–0x10012**. The +8 value is a native physical allocation boundary, separating reserved utility slots from later LBA allocation units. References at **0x22188–94**, **0x2cfd8–0x2d004** and **0x2d1dc–e8** use it as an allocation/index boundary; it is not the offset of a logical utility region in an AP-visible virtual sidecar. The bounded analysis does not recover actual NAND geometry from these globals.

Native discovery also rejects a valid UtilDM found only in slot zero: **0x2981e–0x29860** emits original string **0x34de4**, `Only valid UtilDM is in slot 0 - not publishing util region`, then reinitializes metadata. The 0xff slot sentinel clears the +8 boundary at **0x29888–96**. Consequently one zero-filled metadata page, a major-version byte, or a nonzero format date does not establish native formatting.

## Metadata initialization, validation, and format side effects

The native utility metadata object is **0x7b0a4**, size **0x1000**. Initializer **0x179b0** clears the whole object at 0x179b8–ca and then writes:

| Metadata offset | Native initialization/validation |
| --- | --- |
| +0 | Major version 8, store 0x179ce–d4; checked at 0x29898–a0 and incoming-copy comparison 0x176f6–0x17706. |
| +1 | Minor version from native global 0x8eb40+0x664; store 0x179e2–e6. |
| +2 + 2×element | uint16 element-size entries. Cleared at 0x17a2a–3a; element 2 is initially 1 at 0x17a3e, not a complete physical region map. |
| +0x9a | Retired/defect slot count, initially zero at 0x17a6c; incremented at 0x17084–8a. |
| +0x9c/+0x9e/+0x9f/+0xa0 | Copied native geometry fields validated against live globals at 0x17754–8a; original-device values remain unknown. |
| +0xa1 | Usable/reserved utility-slot count, initially zero at 0x17a64; discovery sets 0x80 at 0x2975a, can reset to zero at 0x29816, and defect processing decreases it at 0x1708e–94. |
| +0xa2 | Initialized to 1 at 0x17a22; used by native copy/slot placement. |
| +0xac | Native metadata generation, initially zero; compare/select at 0x177ee–0x1781a. |
| +0x1b4/+0x1b5 | Pending-state bytes initialized to 0x0f/1 at 0x179e8–f6; rewritten together at 0x18d36–3c. Their complete media semantics remain unresolved. |
| +0x1b6 | Pending-slot sentinel initialized to 0xff at 0x17a68. |
| +0x1c4 | Utility last-format calendar epoch, initialized to zero at 0x17a9c; current format calendar is stored at 0xe4c0. |
| +0xffc | Native operation-history index, initialized 0x1c0 at 0x17a96; updated by 0x2c3fc. |

A validated selected metadata copy is copied in full at **0x178e0–ec**; major/minor, geometry and generation checks precede it. The exact lower-level NAND/page encoding, ECC and complete copy-selection algorithm have not been reconstructed.

Time subcommand 5 reads metadata +0x1c4 into descriptor +0x44 at **0xe014–20**. The native epoch diagnostic at **0x17982–a8** independently identifies it and seeds current calendar time. A virtual implementation can retain its own actual provisioning timestamp, clearly labeled virtual policy; it must not claim a recovered device format date.

### Wire 0x15 is not a harmless sidecar-initialization request

The native AP parser **0x2dbdc** loads the wire opcode at **0x2dcd6**. Dispatch at **0x2e0b4–c0** indexes the table at **0x2e0c4**:

| AP wire opcode | Parser case | Native task internal type | Service case |
| --- | --- | --- | --- |
| 0x15 | 0x2e1d8 | 0x14, stored task +0x18 at 0x2e1e2–e6 | 0xe456 |
| 0x16 | 0x2e1f0 | 0x15, stored task +0x18 at 0x2e1fa–fe | 0xe52e |
| 0x17 | 0x2e20e | 0x17 | separate service case; do not confuse with geometry internal type 0x16 |

The service dispatch is **0xd52c–0xd538 / 0xdea4**, table **0xdea8**. Wire 0x15 copies request +0x30 to task +8; wire 0x16 additionally copies request +0x34 to task +0xc. Shared/AP opcodes and internal task opcodes differ.

Wire 0x15's case **0xe456** calls **0x22008** at 0xe470. That routine quiesces native LBA activity via **0x220b0**, whose **0x22100** clears the LBA-formatted flag and resets native mapping state; it resets all 0x1000 bytes of UtilDM via 0x179b0, clears utility runtime via 0x17aa8, rebuilds native allocation state via 0xffc4, and calls allocation/bitmap processing **0x1015c**. It then rewrites native metadata generation/epoch/geometry and schedules utility persistence at 0xe4b8–0xe52c. Finally **0xeb84** calls **0xc7ec**, which sets native LBA-formatted state at **0xc856**.

This proves native wire 0x15 touches and rebuilds LBA/FTL state. It does **not** prove the exact NAND byte-erasure coverage, but it rules out advertising a no-op or a utility-only preserving implementation as faithful native format success. A raw virtual LBA backend has no reconstructed native FTL, so it should reject native 0x15/0x16 unless it implements and declares their actual destructive effects. For preserving existing firmware System/Data images, provision logical virtual utility storage explicitly outside that native-format command path.

## Minimum valid logical utility layout for the HLE boundary

This is an implementation requirement/inference for a **virtual AP service**, not recovered physical NAND formatting. A truthful HLE utility-format predicate can be derived from a persisted, validated logical region map and its actual backing files, while preserving the existing LBA volume. It must be recorded as virtual provisioning and must not inject fabricated NAND geometry, original UtilDM, device IDs, SysConfig identity or keys.

Native element-name table **0x36260**, stride 9, establishes indices: LLB=0, FW=1, UTILDM=2, DM=3, CTRLBITS=4, EFFACE=5, NVRAM=6, SYSCFG=7, PANICLOG=8, PAD=9. Native size limits at **0x304e0** are separate bounds, not evidence of actual device region sizes. In particular native LLB limit is 254 while the observed AP shadow reads 253 pages; these quantities must not be substituted for one another.

For the currently observed AP consumers, logical storage must at minimum independently back:

| AP region | Observed minimum request footprint | Evidence boundary |
| --- | ---: | --- |
| LLB | 253 × 4096 bytes | AP shadow/read bound recovered by firmware_gate; wire read 0x30, write 0x40. |
| FW | 2048 × 4096 bytes | AP 8 MiB shadow; wire read 0x31, write 0x41. |
| ControlBits | 2 × 4096 bytes | AP diagnostic read 0x34. |
| Effaceable | 1 × 4096 bytes | AP native read/write 0x35/0x45 enforces one page. |
| SysConfig | 32 × 4096 bytes | AP diagnostic read 0x37. |

AP evidence is retained in [storage-native-disassembly](firmware-artifacts/storage-native-disassembly.txt) and the sibling utility-command investigation; these AP footprints are **not** source bytes from ANS or certified original content. NVRAM's exact AP footprint remains unresolved in this bounded investigation. A model advertising it must recover its request contract before declaring that region available. UTILDM/DM/PANICLOG physical layout is not required to masquerade as native NAND in a logical HLE model; any supported logical operations still need defined real backing.

Required invariants:

- Map entries identify separate utility regions, byte lengths and storage, with 4096-byte page addressing. No region may alias System/Data LBAs or exceed its file/allocation.
- Creation/validation must establish every advertised footprint before `utilFormatted=1`; a missing, truncated or invalid map remains unformatted or fails explicitly.
- Reads fill requested DMA bytes from the backing region; writes update the same region; flush/fsync and reboot preserve them. Unsupported operations return errors, not success with untouched buffers.
- The provisioning epoch is the actual persisted virtual provisioning event. A valid initialized layout can have an epoch of zero if clock policy requires it; nonzero epoch alone is not a format predicate.
- Empty/zero-filled virtual regions are disclosed as empty virtual state. They are not recovered signed firmware, SysConfig, NVRAM, a valid keybag, or working SEP services. Cryptographic/identity-dependent consumers can still reject them.
- A missing critical region cannot be hidden by creating files on every launch; provisioning is explicit and format state is derived from the persisted layout.

The structural minimum can justify **virtual utility storage readiness** once its contract tests pass. Whether SpringBoard first boot requires additional provisioned content or security services remains a separate runtime question. No native IOP execution, physical NAND restore, filesystem mount or successful iOS UI is claimed here.
