# Storage continuation audit, 2026-10-13

Read-only audit of run `build/ios712-validated/logs/20261013-091746-63dc4360`, the original firmware/image, and the loader handoff. Native instruction evidence is retained in `firmware-artifacts/storage-native-disassembly.txt`. C and loader implementations are owned by other agents and were not edited here.

**Current continuation:** the later validated utility profile and native HFS
debugger runs supersede the early failures below. Production
`20261013-122816-2da89b24` publishes full modeled capacity and `disk0s1`.
`backend/evidence/hfs-mount-20261013/after-mount/` and `bootstrap-return/`
capture W0=0 from native mountfs and the enclosing HFS mount callback.
Actual root directory, process1 and launchd are still unverified. The next
measured wait is AppleARMPE `SecureRootName` completion; see
[the bootstrap investigation](bootstrap-launchd-20261013.md) and current handover.
The following dated failures are retained as historical evidence.

**11:41 status:** the strict-backend run `20261013-114100-91c246ff` stops earlier, with `GetASPTime`/`GetInitStatus` general errors before publishing a block device. This is a truthful unsupported-command failure, not an HFS/keybag failure. Exact native opcode 0x80/0x81 contracts recovered below allow the next bounded implementation; a new trace must establish progress afterward.

**Later 11:53 run:** `20261013-115347-3a7856f6` implements the recovered clock/init queries and gets correct geometry (795696 LBAs, 4096 bytes, preferred 1048576 bytes, LBA formatted YES, utility formatted NO). Its concrete next block is now proven: the native block-storage publisher deliberately creates **zero sectors when utility is unformatted**, despite the valid LBA geometry. See the capacity predicate below. Unsupported NAND identity/timing/options requests produce genuine errors; they do not explain this particular zero-capacity branch.

## What the run proves

The **actual XNU guest** announces AppleCSI running, ASPStorage geometry and an IONANDBlockDevice. Native IOGUIDPartitionScheme selects `System@1`, then BSD names `disk0s1`. The HLE backend trace shows reads from GPT LBAs 0/1/2, the HFS volume header and native HFS metadata. This establishes real kernel execution and a working partial storage path. It does not establish a completed root mount, `/sbin/launchd`, SpringBoard, persistent writes or security hardware.

Final captured PC `0xffffff800021b4f0` is just after a native `DSB; WFI` sequence at `0xffffff800021b4e8/4ec`, in kernel CPU idle handling. The trace continues timer interrupts and CSI pings. It is not a crash PC or evidence identifying the blocked bootstrap thread. The bounded run ends via QMP; runtime-final correctly records unclean shutdown and unverified userspace. No next root-mount/keybag wait can be named conclusively without the relevant kernel thread backtrace or mount/init hook trace.

The first `iBoot version: iBoot-1940.10.58` line is XNU reading the derived DeviceTree firmware-version property. The native monitor and XNU execute, while this project loader replaces iBoot; that line alone does not prove execution of the original iBoot binary.

## Original HFS and partition mapping

Original System HFSX partition: 2185363456 bytes, block size 4096, 533536 blocks, SHA256 `c7a987439e75bb8e989f3e1ee76bea3f0fec7d9ae804e5ad1d618409f0524c30`. Original extracted system remains byte-exact. The data HFSX partition is 1073741824 bytes, 262144 blocks, SHA256 `7bc1b7e2daa3d7ffc9919057b98ba022bd78785275dfcad618b4697ec928f8f0`; it remains unprovisioned.

The run's **chosen emulator layout** exposes a synthetic 16-GiB, 4096-byte-sector GPT disk. System starts at LBA 8 and occupies 533536 LBAs; Data starts at LBA 533544 and occupies 262144. Native GPT recognition confirms the constructed partition syntax and `disk0s1` mapping. The disk GUID, partition UUIDs, total capacity and GPT bytes are emulator profile choices, not extracted physical NAND metadata. The IPSW RootFS UDIF/APM container is a restore-image container, not evidence that an actual device NAND uses this GPT layout.

Firmware fstab requires `/dev/disk0s1 / hfs ro` and `/dev/disk0s2 /private/var hfs rw,nosuid,nodev`. The observed root name satisfies the first identifier, but data mounting and seed provisioning remain unverified. Mounting the blank Data volume hides the original `/private/var` seed tree (336768902 bytes, including assets, directories and symlinks), as documented in `firmware-content.md`.

### Journal write discarded in the audited run

Original HFSX volume header attributes are `0x80002100`, with journalInfoBlock **33**. The JournalInfoBlock flags are 1, journal offset **139264 = block 34**, and journal size **8388608**. Therefore the trace's **WRITE LBA 42** is the System journal header: partition start 8 + filesystem block 34. It is followed by opcode **0x13**.

The audited backend opened System read-only, discarded this write and acknowledged success. It also acknowledged opcode 0x13 without flushing anything. Native XNU really requested the write; successful persistent journal handling was fabricated by that implementation. Preserve the original immutable system and use an actual persistent derivative/overlay for guest journal writes, or expose a truthful write error with a verified read-only guest contract.

Other reads map independently to the original HFS metadata:

| Guest LBA | Original HFS structure |
| ---: | --- |
| 8 | volume header, at byte 1024 in that block |
| 41 | JournalInfoBlock 33 |
| 42 | journal header block 34 |
| 2090 | extents B-tree first block 2082 |
| 20010 | catalog B-tree first block 20002 |
| 3114 | attributes B-tree first block 3106 |
| 9–25 | allocation bitmap, starting filesystem block 1 |

A bounded independent parse of all 31-MiB attributes-file extents found 41589 leaf records named `com.apple.decmpfs` and no attribute name containing `protect`. This supports a restore image with compressed system files; it does not establish data-volume keybag initialization or bypass AppleKeyStore/SEP requirements.

## Native protocol fields versus invented replies

The recovered native ARM32 ANS image is genuinely loaded into reserved RAM, with recorded iBoot-derived fixups. The audited command does **not** enable the diagnostic ARM32 IOP CPU: ANS services here are implemented by C HLE. Its responses are modeled device behavior, not output from executing that firmware.

The following ABI is supported directly by the original kernel SHA256 `1e0f584ff54040ded8a2ef4ed532b4298f2500b774fc14d88020453c91465666`:

| Contract | Native evidence |
| --- | --- |
| ASP endpoint completion: low nibble 2, tag bits 4–11, status at bit 12 upward | event handler `0xffffff80010c61c0–6254` |
| Ready tag 0xfe must advertise protocol version 10 | compare `0xffffff80010c61f8–6200` |
| Normal completion tags 0–31 | `CompleteRequest` rejects tag >=0x20 at `0xffffff80010c8140–8144` |
| Status 0 → success; 1/2 → 0xe00002c2; 3 → 0xe00002d1; 4 → 0xe00002d6; higher → 0xe00002bc | table `0xffffff80010d6fc0`, dispatch `0xffffff80010c81b0–81f4` |
| Page-count pool limits 16, 256, 2048 | `0xffffff80010c6fb8–6fdc`; largest pool requires descriptor byte-2 bit-3 set |
| PPN list at descriptor +0x30, physical page shift 12 | native population helper `0xffffff80010c79ec–7a30` and descriptor page preparation |
| Geometry +0x30 count, +0x34 bytes/LBA, +0x38 preferred **LBA count**, +0x3c lbaFormatted, +0x40 utilFormatted, +0x44 pendingLoad | `0xffffff80010c88f0–89d4`; preferred bytes logged by multiplying +0x34 and +0x38 at `0xffffff80010c8a50–58` |
| Opcode 0x13 is synchronizeCache | opcode-specific guard and diagnostic string `0xffffff80010c71d0–71e4`, `0xffffff80010d3fc2` |

IOReturn labels were checked against the local Apple SDK header: 0xe00002c2 BadArgument, 0xe00002d1 BadMedia, 0xe00002d6 Timeout, 0xe00002bc general Error. These native numeric encodings allow failures to reach the guest. They do not define success for unimplemented commands.

The audited geometry response chose 4194304 LBAs, 4096 bytes/LBA and flags `lbaFormatted=1`, `utilFormatted=1`. Its preferred field was incorrectly supplied as 0x100000 bytes rather than a block count, yielding the observed preferred-byte value zero through 32-bit overflow after multiplying by 4096. The formatter flags, capacity and ready/power acknowledgements require backing state; accepting their native encoding is not proof their claimed operation happened.

## Utility storage, effaceable content and next wait

After HFS metadata traffic, this run sent unimplemented commands 0x31 repeatedly, 0x30 repeatedly, 0x35, 0x34 and finally 0x37. The old HLE returned success to all of them without supplying their result buffers. Earlier it similarly accepted NAND information/timing/options commands 0x80/0x1a/0x1b/0x19/0x81/0x79/0x76 and others without implementing them.

The final **0x37** is **ASPDiagnostic SysConfig read**, not a keybag command: native code builds it at `0xffffff80010cd7b8`, requests 0x20 blocks and DMA into its 0x20000-byte buffer. Opcode **0x34** is its ControlBits read of two blocks, built at `0xffffff80010cd74c`. Both set descriptor byte-2 bit-3 before sending. These are real utility-area requirements absent from the raw System/Data partitions.

`[effaceable:ERR] unable to find content` originates at `0xffffff8000da170c–1724` when the native generation scan finds no content. That branch then falls through initialization and the native `started` log at `0xffffff8000da1728–1754`. It is a real missing-content condition, **not a demonstrated fatal panic or the proven boot wait**. Do not patch out AppleEffaceableStorage or manufacture locker/key bytes based on that message.

There is no original-artifact evidence justifying **utilFormatted=1**: the extracted RootFS contains its HFS partition, not ANS utility, SysConfig, NVRAM or effaceable-area metadata. Native geometry stores that flag into ASPStorage +0xd8, and ASPEffaceable readiness checks it through native helpers. Exposing zero honestly can reveal an explicit unformatted path until persistent utility storage is implemented. Claiming formatted utility while returning untouched buffers is contradictory.

The root-volume metadata, missing utility storage, PMU failures, unavailable SEP services and missing AGX personality all remain independent concerns. A live stack of the bootstrap/mount thread is needed before attributing this pause to HFS, a security keybag, effaceable content or display. No daemon removal or unverified kernel patch is justified by the present evidence.

## Follow-up status

The backend owner has subsequently reported passing guest tests for bounded DMA, truthful error completions, preferred-LBA units, host I/O, fsync and persistent System journal derivatives. Those fixes occur **after** the audited 09:17 run and require a new production guest trace; historical success replies described above must not be presented as the current fixed behavior without checking the new build. This audit supplied the native error/geometry/flush/utility anchors and changed only research artifacts.

## Native time and initialization requests needed by the strict backend

Opcode **0x80** is a subcommand family; accepting every member would recreate the false-success problem. Native `GetASPTime` at VA 0xffffff80010c8ae4 requests **subcommand 5 at descriptor +0x38**. Results are three uint32 words at +0x44/+0x48/+0x4c. The kernel consumes them at 0xffffff80010c8b5c–70. Its SetTimestamp routine at 0xffffff80010c6c94 requests **subcommand 6**, supplying calendar seconds at +0x48 and elapsed seconds at +0x4c. Imports resolve directly to exported `clock_get_system_microtime` and `clock_get_calendar_nanotime`; values supplied are their seconds components, not timebase ticks.

The original **ARM32 ANS firmware itself** proves the formerly opaque +0x44 meaning. Thumb code 0xe014–0xe03c reads utility metadata at MCU address 0x7b0a4 + 0x1c4 and writes it to descriptor +0x44, then current calendar/elapsed globals 0x8eb40 + 0x650/+0x660 to +0x48/+0x4c. Code 0x17982–0x179a8 prints the very same metadata field with the original string at 0x34b2a: `Util last formatted on ( %u ) epoch`, then seeds the calendar global from it. Therefore **+0x44 is utility last-format epoch**, not CPU boot time, current time, an ID or a secret value. Native utility initialization at 0x179b8–0x179ca zeroes the 0x1000-byte metadata object; a modeled unformatted utility area can return zero for this field with `utilFormatted=0`. A formatted utility implementation must retain its actual format epoch.

Native SetTimestamp subcommand 6 has state-dependent semantics:

- First synchronization (flag at 0x8e0a0 is zero): if new calendar >= old calendar, elapsed is increased by their difference; a backward calendar increments a diagnostic counter instead. The supplied elapsed word is not used on this first path. The flag is then set.
- Later synchronizations: provided elapsed >= current elapsed updates it; a backward elapsed increments a diagnostic counter and retains the existing elapsed value.
- Both paths set calendar to the supplied word. The firmware handler here does not turn a backward sample into an error completion.

Anchors: 0xe03e–0xe086, 0xe712–0xe748, 0xedac–0xedc8. A virtual clock profile can advance these modeled seconds with QEMU virtual time and apply native synchronization; it must describe its origin/persistence as emulator policy. No physical RTC or original format date has been recovered.

Opcode **0x81 GetInitStatus** requests **subcommand 0x10** at descriptor +0x38, a DMA PPN at +0x30 and **byte count 4** at +0x34. The returned uint32 initialization state is read from that DMA buffer. Native builder/consumer: 0xffffff80010c6740–0xffffff80010c68dc; its table is at 0xffffff80010c6a1c.

| Initialization state | Native diagnostic |
| ---: | --- |
| 0 | NAND Init OK |
| 1 | NAND PPN Bad Firmware |
| 2 | NAND PPN Version Error |
| 3 | NAND Invalid Device Parameters |
| 4 | NAND PPN Unsupported Firmware |
| 5 | No NAND detected |
| 6 | NAND Invalid Die Count |
| 7 | NAND Mismatched Chip IDs |
| 8 | NAND Scratchpad Failed |

This **payload state is distinct from mailbox completion status**. State zero requires a successfully initialized modeled storage backing; real file/geometry/sector/DMA validation must precede it. It does not claim physical NAND IDs, fuse calibration or utility formatting. Other 0x81 subcommands remain unsupported until their contract is recovered.

## Breakpoints for the later root-mount probe

The original kernel's `hfs` vfsconf entry is at **0xffffff80006159b8**, with inline name at +8 and root callback pointer at +0x28 pointing to **0xffffff80004b8c14**. Bootstrap loads the selected filesystem's +0x28 callback and invokes it at 0xffffff80004e61f8; the following 0xffffff80004e61fc branches on its returned W0 error. This identifies the HFS root callback through native table data, without guessing a stripped function name.

| Probe | Native VA |
| --- | --- |
| Exported `IOFindBSDRoot` entry | 0xffffff80002d1e34 |
| Bootstrap return from root discovery | 0xffffff80004e5e10 |
| HFS root callback entry | 0xffffff80004b8c14 |
| Underlying HFS mountfs entry | 0xffffff80004b8ca4 |
| HFS callback after mountfs, W0 error available | 0xffffff80004b8c40 |
| HFS callback return cleanup / RET | 0xffffff80004b8c98 / 0xffffff80004b8ca0 |
| Bootstrap after filesystem callback, W0 error available | 0xffffff80004e61fc |

Native exported globals `rootvp` at 0xffffff80006940e0 and `rootvnode` at 0xffffff8000694090 can corroborate progress. Capture the actual caller/return W0 and thread stack before deciding the next failure. Root discovery, HFS metadata reads and successful mount completion are separate observations.

## Proven 11:53 block: native capacity publication requires utility formatting

`ASPBlockStorage::StartService` calls the geometry getter at **0xffffff80010c5474**. Getter **0xffffff80010c7cc0** copies ASPStorage geometry into block-storage object fields +0xb0 (LBA count), +0xb4 (bytes/LBA), +0xb8 (preferred LBA count), +0xbc (LBA formatted), +0xc0 (utility formatted).

At **0xffffff80010c5498–54ac**, normal full capacity requires all of:

1. Signed `burnin-size` < 0 (default -1).
2. LBA formatted field +0xbc nonzero.
3. Utility formatted field +0xc0 nonzero.

Only then does **0xffffff80010c54b4** load X20 from the geometry count. Otherwise **0xffffff80010c54ec** explicitly sets **X20=0** and alternate-mode byte +0xec to one. The native log at 0xffffff80010c5524 therefore announces zero sectors; the block-device constructor at 0xffffff80010c5530 receives that exact zero. Subsequent firmware/LLB block-device reads reject block zero as outside a zero-size device, so GPT never receives a read and `IOFindBSDRoot` cannot select System.

This is native policy, not an HLE read failure or merely missing a root-matching string. Override `utilFormatted` without backing utility state would reintroduce invented readiness. The next required work is a persistent virtual utility layout and verified format/read/write contract.

Breakpoint **0xffffff80010c54f8** captures the selected capacity in X20 and block-storage object in X19; inspect five uint32 words at X19+0xb0 and byte X19+0xec. Predicate breakpoint 0xffffff80010c54a8 reads the utility flag; 0xffffff80010c54ec proves the zero branch. `InitializeASP` reaches its success publication path at **0xffffff80010c6614–662c**, where state bit zero is set and kASPStorageReady is delivered; entry is **0xffffff80010c6420**. No successful root mount is claimed.

### Live debugger confirmation

`backend/evidence/utility-capacity-20261013/probe/` uses the retained production
boot plan with separate checksummed diagnostic System/Data copies. At native
VA `0xffffff80010c54f8`, X19 identifies the block-storage object, X20 is **0**,
and LR is `0xffffff80010c54ec`. Dynamic `$x19+0xb0` reads
`0x000c2430, 0x1000, 0x100, 1, 0`: 795696 LBAs, 4096 bytes/LBA, preferred
256 LBAs, LBA formatted and utility unformatted. Byte `$x19+0xec` is **1**.
This corroborates the exact zero-capacity branch with runtime state. The
prepared VM disks were not opened by this debugger probe.


## Persistent utility contract: native API versus physical NAND format

The minimum utility regions required by the native kernel use **4096-byte pages**, separate from System/Data LBAs. The request preamble is 0x80 bytes; byte zero is the opcode, uint32 +4 is page offset and uint32 +8 is page count. The DMA PPN list is at +0x30 with each physical page address shifted right by 12. The shared request builder at 0xffffff80010c7ad4 copies the preamble, allocates a bounded pool from +8 and descriptor byte-2 bit-3, then prepares the actual DMA mapping. Counts must be checked against both the region extent and native pool limits; a zeroed response buffer is not a successful read.

| Region | Read / write opcode | Native extent / request evidence |
| --- | --- | --- |
| LLBFirmware | 0x30 / 0x40 | 253 pages = 0xfd000 bytes; formatted readiness sets object +0xc0 to 0xfd at 0xffffff80010c2304–08, repeated at 0xffffff80010c2630–34; read builders 10c1924/10c1a7c, write 10c1cd8 |
| Firmware | 0x31 / 0x41 | 2048 pages = 8 MiB shadow; 0xffffff80010c0378/10c03b8; read builder 10c0504, write 10c0758 |
| ControlBits | 0x34 / 0x44 | two pages = 8 KiB; read 0xffffff80010cd74c–75c; write 10cd57c |
| Effaceable | 0x35 / 0x45 | exactly one page = 4 KiB; native 0xffffff80010c29d0 checks offset + requested count <= 1, 29dc checks count << 12 <= DMA length, 2aa8 stores offset and forced count one |
| NVRAM | 0x36 / 0x46 | two pages = 8 KiB; 0xffffff80010cbd38–54 / 10cb9c8–9e4 set offset zero and count two |
| SysConfig | 0x37 / 0x47 | 32 pages = 128 KiB; read 0xffffff80010cd7b8–c8; write 10cd548 |

The large-pool flag (descriptor byte-2 bit-3) is set by Firmware, LLBFirmware, NVRAM and diagnostic ControlBits/SysConfig callers; Effaceable uses its one-page request. ControlBits/SysConfig write routine 0xffffff80010cd4c0 clamps its temporary copy to the buffer size but derives the descriptor count from supplied byte length at 10cd5ac–bc; the emulator must reject out-of-region or incomplete-page requests even when a guest caller's temporary copy was bounded. These are API extents measured in the kernel, not physical NAND capacity or a recovered NAND image.

**Format is disabled by default.** At 0xffffff80010c5c88 the driver clears bytes +0x142/+0x143. It invokes `_PE_parse_boot_argn` (import 10d139c -> export 2d6d34) on `nand-enable-reformat`, destination object +0x142, length one. Both format entrypoints require that byte to equal exactly one. The original string and diagnostic are at 0xffffff80010d3b09/10d3b1e. This is a boot argument, not a DeviceTree format-ready property.

`InitializeASP` (0xffffff80010c6420–6630) does **not** invoke either format command; it retrieves geometry/time/options/info and publishes storage-ready state. A utility format caller exists in ASPEffaceable at 0xffffff80010c3d1c, which tailcalls the utility formatter only when helper 10c804c reports geometry utility flag zero. Whether an initial guest invokes that caller automatically must be measured separately; enabling the boot argument alone is not proof of format completion.

The native utility-format entrypoint **0xffffff80010ca7e0** constructs wire **opcode 0x15** at 10ca878. Optional nonnegative burnin-size is stored at descriptor +0x30. It sends a timestamp first, then after completion retrieves geometry and time again; nonzero capacity, block size, utility-formatted and LBA-formatted fields are all required at 10ca8dc–a918. It sets session byte +0x143 and emits native notifications only after those checks pass. Native LBA-format entrypoint **10ca548** constructs wire **opcode 0x16** at 10ca64c; when utility is initially unformatted it redirects to utility-format 0x15 at 10ca5d0–e4. A later invocation with utility formatted can issue 0x16. The kernel naming of 0x15 as utility format does **not** establish that its native physical implementation preserves existing LBA data.

The original ANS firmware stores proprietary physical utility metadata, including allocation boundaries and last-format epoch; its utilFormatted geometry flag is derived from an initialized physical utility map, not simply a constant readiness flag. The native wire-to-internal-op binding has now been recovered: **wire 0x15 invokes the physical full formatter**, described below. A persistent logical utility container can be an explicit emulator profile, with validated extents, mutable content, truthful completion failures, and actual flush/persistence. It must not be described as original NAND calibration, native SysConfig identity, fabricated effaceable keys or restored physical utility metadata. Unknown opcodes and unsupported format modes must return native errors. No guest format operation has been authorized or performed in this research step; no production utility flag was overridden.

Raw bytes and disassembly for all anchors above are retained in `research/firmware-artifacts/storage-native-disassembly.txt` with the original kernel SHA-256. No kernel, loader or backend edits were made by this audit.


### Native wire 0x15 is not a safe preserve-LBA utility initialization

The original ANS Thumb incoming-request parser is **0x2dbdc**. Its callback is installed as Thumb pointer 0x2dbdd at 0x1991a–19922 and 0x2bdf6–2be06. It loads the wire descriptor's byte-zero opcode at **0x2dcd6**. For wire opcodes 0x14–0x18, **0x2e0b4** subtracts 0x14 and **0x2e0c0** indexes a TBH table at 0x2e0c4. The raw five halfwords are 0x0005, **0x008a**, **0x0096**, 0x00ec, 0x00a5.

- Wire **0x15** therefore branches to **0x2e1d8**, writes **internal opcode 0x14** into task word +0x18's high byte at 0x2e1e2–e6, and copies wire +0x30 to task +8.
- Wire **0x16** branches to **0x2e1f0**, writes **internal opcode 0x15** at 0x2e1fa–fe, and copies wire +0x30/+0x34 to task +8/+0xc.
- The same parser maps reads 0x30–0x38 into internal type 0x0a with utility type = opcode−0x30 at 0x2df78–9c; writes 0x40–0x48 into internal type 0x0b and type = opcode−0x40 at 0x2dfb2–d6. This independently confirms the logical utility-region IDs above.

The internal formatter at **0xe456** calls **0x22008**, which initializes/reset utility metadata and LBA setup; it writes format epoch from the current modeled calendar into utility metadata +0x1c4 at 0xe4c0. The continuation at 0xeb84 calls 0xc7ec, which sets the LBA-formatted state at 0xc84a–56. Internal 0x15 at 0xe52e additionally changes LBA allocation. Full native physical-format details and NAND-dependent side effects are retained by the separate ANS firmware audit; this binding is enough to reject a claim that wire 0x15 safely preserves the existing System/Data layout.

A preprovisioned **logical emulator utility profile** must be explicitly identified as such, validate actual persistent storage and implement the bounded native read/write ABI. Its initial erased/empty contents, format epoch, and region extents must be documented as model policy; they are not recovered hardware NAND state. If wire 0x15 or 0x16 is unsupported, return an error and retain System/Data. Enabling `nand-enable-reformat` and silently redefining physical format as a successful no-op would violate the fail-closed goal.


### Observed option/identity failures are separate from the capacity predicate

Wire **0x1a** is the native NAND qualification option. AP builder 0xffffff80010c8c04 reads boot argument `nand-qual` and sets descriptor +0x30 bit zero at 10c8c90–98. Original ANS parser 0x2dcf4–2dd12 converts it into internal type 0x1b and copies that bit; handler 0xd97c–98a persists it into native global 0x7c0a4. Wire **0x1b** maps to internal type 0x1a at 0x2ddca–ddd8. Handler 0xd91a checks physical configuration fields, raises an initialization event/flag and waits until native state 0x8eb40+0x3f0 equals five at 0xd972–78. No physical startup or generic no-op success is inferred for an HLE implementation.

Both AP helpers return `Send`'s endpoint-send status. Its X22 is the endpoint method return at 0xffffff80010c7328–2c, retained until 10c746c. It waits for the request's completion state but does not replace X22 with the mailbox completion error. Thus a successfully delivered request carrying a failed native completion can pass these helpers; other callers such as GetASPTime explicitly inspect the request's error field. This explains why the latest trace can reject unsupported 0x1a/0x1b yet still reach storage-ready publication. Those failures must remain visible; their presence does not supersede the proven utility-flag zero-capacity branch.

Wire **0x79 / 0x76** are native GetNANDID alternatives: helper 0xffffff80010c976c selects 0x79 for selector zero, 0x76 for one, and rejects other selectors. After checking the real request completion, it consumes result length at descriptor +0x30 and returned bytes at +0x34, bounded against the caller's requested length. No actual physical ID content was recovered; returning fabricated ID bytes merely to satisfy property creation is unjustified. GetNANDInfo/GetNANDTiming are additional 0x81 subcommands, distinct from 0x1a/0x1b and from the bounded initialization-state subcommand 0x10.
