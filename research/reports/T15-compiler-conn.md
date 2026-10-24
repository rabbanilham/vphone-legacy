# T15: AGX compiler connection lifetime investigation

2026-10-22; worktree `${WORKTREES_ROOT}/vphone-wt-compiler-conn`, branch `wt/compiler-conn`.

## Result

The block is the asynchronous reply disposer in **AGXCompilerConnection.framework's `XPCCompilerConnection::ReleaseReply(void*)`**. Capture `+0x20` is the C++ connection, and connection `+8` is its dispatch serial queue. The constructor initializes that field from `dispatch_queue_create("CompilerConnectionSerialQueue", NULL)`; it is not an IOKit output structure.

The adjacent allocation containing `BuildMachineOSBuild` is an actual `__NSCFString`: its ISA resolves to that class in the supplied cache. Its header word at `+8` is **exactly `0x10000078c`**. Thus this number has a concrete non-time explanation as CF object metadata. A connection freed and reused for a string would produce precisely this failure when the queued block reloads `this->queue`. The binary contains that lifetime hazard: the block retains the queue but captures a raw `this`; the destructor does not drain pending blocks before deleting `this`.

**Heap reuse is the leading hypothesis, not yet proven for the captured connection.** Probe115 dumped the block and adjacent memory, but did not dereference capture `0x170655840`. It also lacks construction/destruction history. No emulator-side bad write or incorrect ABI result has been established. I have not changed the board, guest software, or guest disks. The deliverable is a focused, read-only next capture plus diagnostic regression tests.

## Evidence and address mapping

Read T13 first. Inputs are under:

`/private/tmp/claude-501/-Users-bagas-Downloads-vphone-legacy/8d4c9f73-518d-4e4f-a9c7-5a124b947693/scratchpad/probe115/`

| Input | SHA256 |
|---|---|
| `breakpoints.json` | `aee01499930151777e14ef1039cf623d6a1aa718257e157d82d01b77c7f969fe` |
| `user-hook-mapping.json` | `88ef31987f1f3c26d2d5e5a2bfb1273cb77f551d74c8572f637281d554457148` |
| `crashlogs-probe114/SpringBoard_2026-10-21-103404_iPhone.ips` | `75f2eba0327aba913ced6d3d45abc612b54f5124fc3a0b3c3cf102b656b11985` |

The crash report is a separate boot: PID 38, thread 2, same bad value, main thread in MobileKeyBag through ManagedConfiguration, date in 2026. **Do not apply that report's ASLR slide to probe115.** Probe115 records PID 37, thread ID 728, elapsed 25.263285 s, slide `0x5f9c000`, and verified dispatch_release bytes. Its invoke `0x18b15b2ac - 0x5f9c000 = 0x1851bf2ac`. The 64 captured invoke bytes equal the cache bytes at file offset `0x51bf2ac`.

There is an indexing trap: `cache-index.json`'s *images* list omits AGXCompilerConnection and AGXGLDriver, and `cache-local-symbols.json` supplied no nearby names. The **cache header's own image table** contains:

- `0x1851bc000`: `/System/Library/PrivateFrameworks/AGXCompilerConnection.framework/AGXCompilerConnection`.
- `0x180260000`: `/System/Library/Extensions/AGXGLDriver.bundle/AGXGLDriver`.

Recover that table using little-endian `imagesOffset, imagesCount` at header `+24`, then 32-byte `<QQQII>` image records (address, modtime, inode, path-file-offset, pad). Parse each Mach-O's LC_SYMTAB and export trie. The public C++ names below are present there; nearby `<redacted>` functions are named by their instructions/call sites. Cache mappings from the index remain valid for code/data reads and hook verification. Disassembly used Capstone in the supplied legacy venv; `/tmp/t4-touch-evidence/cache.py` independently reconstructs the image table and public symbols this way.

## Object layout and producer

`CompilerConnectionCreate(CompilerConnection*&)` at `0x1851bf00c` calls `getenv("USE_MONOLITHIC_COMPILER")`. A leading `'1'` selects the monolithic implementation; otherwise it allocates `0x28` bytes with `operator new` and calls the XPC constructor at `0x1851bf13c`. The captured invoke establishes the XPC implementation for this incident.

| Offset | Meaning established by instructions |
|---|---|
| `+0` | C++ vtable address point (`0x18f11a150` for XPC implementation, unslid) |
| `+8` | dispatch serial queue |
| `+0x10` | 32-bit field initialized to zero |
| `+0x18` | XPC connection to `com.apple.AGXCompilerService` |
| `+0x20` | most recent synchronous XPC reply |

At `0x1851bf16c`, `dispatch_queue_create` returns the queue in x0. At `0x1851bf17c`, `stp x8,x0,[x19]` writes vtable and queue together. At `0x1851bf180`, the constructor zeros `+0x18/+0x20`, then creates the XPC connection, stores it at `+0x18`, installs an event handler, and resumes it.

AGXGLDriver creates this object from its initializer `0x18026f2a0`: it passes owner `+0x280` by reference to CompilerConnectionCreate at `0x18026f2cc`. Its cleanup `0x18026f46c` passes the same slot to CompilerConnectionDestroy at `0x18026f484`. Constructor initialization and subsequent error paths therefore matter, as do normal driver teardown paths. These functions are redacted; no unsupported source-level name is assigned.

The XPC vtable's slots are destructor, deleting destructor, ReleaseReply, BuildRequest. AGXGLDriver contains the corresponding virtual calls. For example, `0x1802765f0..5fc` loads vtable `+0x10` and calls it with x0=connection and x1=reply at `[sp+0x80]`; analogous cleanup sites occur at `0x180276c6c`, `0x1802772c8`, and later shader paths. The next capture's ReleaseReply entry records the actual LR/frame chain, selecting the precise caller instead of guessing which shader path ran.

BuildRequest (`0x1851bf2d0`) dispatch_syncs a block on this queue. That block packages input byte arrays into XPC data/array/dictionary objects, calls `xpc_connection_send_message_with_reply_sync`, stores the reply at connection `+0x20`, checks its type/status and retrieves output data. Its tail at `0x1851bf5a4` publishes the reply pointer to the caller; x22 is the internal reply status (0 is its success path). This is native guest XPC/compiler IPC, not a host-emulated compiler service. The live capture does not say whether compilation succeeded.

## Exact failing block and lifetime hazard

ReleaseReply at `0x1851bf240` is equivalent to:

```c++
// Pseudocode reconstructed from the supplied binary.
dispatch_retain(this->queue);
dispatch_async(this->queue, ^{
    xpc_release(reply);
    dispatch_release(this->queue);  // reload through raw this
});
```

Its stack block stores `this` at `+0x20` and `reply` at `+0x28`, size `0x30`. There is no copy/dispose helper retaining the C++ object (descriptor has reserved, size, signature; flags lack BLOCK_HAS_COPY_DISPOSE).

```asm
1851bf2ac stp x29, x30, [sp, #-16]!
1851bf2b0 mov x29, sp
1851bf2b4 stp x20, x19, [sp, #-16]!
1851bf2b8 ldp x19, x0, [x0, #0x20]  // this, reply
1851bf2bc bl  0x1851bfb00            // xpc_release stub
1851bf2c0 ldr x0, [x19, #8]          // this->queue
1851bf2c4 ldp x20, x19, [sp], #16
1851bf2c8 ldp x29, x30, [sp], #16
1851bf2cc b   0x1851bf9ec            // dispatch_release stub
```

For probe115, block `0x170656dd0` captures `this=0x170655840`, `reply=0x170188540`. At the final tail call x19 has already been restored to the wrapper's block, explaining T13's register layout and unchanged LR `0x194118014`.

The deleting destructor `0x1851bf1c0` calls D2 (`0x1851bf1e8`) and then operator delete. D2 cancels/releases the XPC connection and releases `this->queue`, with no dispatch_sync/drain. The extra queue retain keeps the queue alive but **does not keep the C++ allocation containing its pointer alive**. If the owner destroys the connection while a reply disposer is pending, the block dereferences freed storage. This is a static hazard; dynamic ordering is still required to call it the incident's cause. Destruction might also be downstream of some earlier emulator-triggered error.

The adjacent string starts at block `+0x60 = 0x170656e30`:

```
+0x00  b9 dc 55 94 a5 01 00 00    nonpointer ISA 0x1a59455dcb9
+0x08  8c 07 00 00 01 00 00 00    0x10000078c
+0x10  13 42 75 69 6c 64 ...      length byte + BuildMachineOSBuild
```

Masking the ISA with `0x1fffffff8` gives runtime class `0x19455dcb8`, unslid `0x18e5c1cb8`. That class's data/RO pointer is `0x18e5b0378`; RO `+24` points to `0x18171581f`, the string **`__NSCFString`**. This verifies the neighboring allocation's type directly. The `0x78c` low word is consistent with CFString runtime flags/type metadata and the high word with a reference count; it is not evidence of a GPU build/version integer. Both a 40-byte connection and this short inline string fit a 48-byte allocator class. **The adjacent string is at a different address from `this`; its presence alone does not prove that `this` was also reused as a string.**

## Board and property audit

Inspected `backend/s5l8960x.c`'s AGX translation, firmware ring, command, render, and DMA writers, plus loader DeviceTree setup:

- No host AGX user-client selector/struct-output implementation, XPC compiler reply generator, or `BuildMachineOSBuild` property producer exists in the board. Native guest kernel/user libraries implement those interfaces. CompilerConnection itself imports XPC/dispatch/C++ support, not IOKit registry query functions.
- AGX hardware identity is sgx0 `+0x20 = 0x14600` and `+0x18 = 0`, initialized/reset and returned in MMIO reads. AGXGLDriver's initializer separately calls IOConnectCallStructMethod selector 5 for a 4-byte output and compares versions `0x01320404/0x01320403`. That native kernel response is not written by a host user-client stub. No observed version result links this check to the bad heap field.
- `agx_gpu_access` walks CAT/PCE/PDE/PTE and splits transfers at 4 KiB. Firmware writes are 0x28-byte alive/completion entries, 4-byte producer/consumer indices, and 4-byte command-supplied stamps. Ring 3 also updates host-side context catalogue tracking. These can corrupt unrelated RAM if the interpreted mappings/command addresses are wrong; this capture supplies no physical mapping or DMA overlap proving that happened.
- Render replay (enabled by default) writes BGRA words through `agx_replay_put` into decoded GPU targets; optional `VPHONE_AGX_RT_FILL` writes bounded row/page chunks. They remain possible indirect writers if target decoding is wrong. The observed value/string pattern is not a value deliberately returned by either path. No physical alias evidence currently ties them to this object.
- Other board RAM writes go through `ans_write_ram` for storage DMA, scalar utility replies, SIO transfers, crypto, and SEP/keybag OOL replies. These use command/descriptor-derived destinations. No path has been tied to capture `0x170655840`; virtual heap contents alone cannot exclude bad physical DMA or translation elsewhere.
- `scripts/qemu/boot_image.py` preserves native GPU nodes, supplies chosen `firmware-version` as `iBoot-1940.10.58`, and sets common clocks/chip-revision plus display/PMGR/ANS properties. It does not synthesize GPU compiler build strings. Guest IORegistry properties also arise from native kext matching/personality data, so lack of a board string is not a claim that the guest never reads build metadata.

No change to TCG, a GPU version, a struct size, keybag behavior, or clock is justified by this evidence.

## Implemented capture and exact next run

New `backend/compiler_connection_hooks.json` has ten SpringBoard-only, shared-cache-verified PCs. `display_probe.py` records raw connection bytes and candidate queue/XPC/reply bytes using the register valid at each particular PC. At invoke it follows the block's `+0x20/+0x28` captures; after xpc_release it uses x19, before the failing load. Existing dispatch_release evidence now also follows both captures when readable. All changes are debugger reads. The focused hook file deliberately avoids the global high-frequency dispatch_release breakpoint.

| Hook suffix (`sb_compiler_`) | Unslid PC | Object source |
|---|---|---|
| `ctor_ready` | `1851bf1ac` | x19 |
| `release_reply` | `1851bf240` | x0; x1 is reply; LR identifies caller |
| `reply_enqueue` | `1851bf298` | x20; block at SP, just before dispatch_async |
| `reply_invoke` | `1851bf2ac` | `[x0+0x20]`; x0 is block |
| `reply_after_xpc_release` | `1851bf2c0` | x19, before loading queue |
| `request_reply` | `1851bf5a4` | x24; x22 is internal status |
| `destroy` | `1851bf1e8` | x0, before D2 changes vtable |
| `delete` | `1851bf1d4` | x19, after D2, before operator delete |
| `owner_created` | `18026f2d0` | `[x19+0x280]` |
| `owner_destroy` | `18026f46c` | `[x0+0x280]` |

Run from this worktree with the maintainer's usual built backend/assets:

```sh
python3 backend/display_probe.py \
  --output /tmp/t15-compiler-lifetime \
  --seconds 90 --stop-condition duration \
  --plan ${REPO_ROOT}/build/ios712-validated/boot/boot_plan.json \
  --diagnostic-system ${REPO_ROOT}/build/diagnostic/system-diagnostic-sep.raw \
  --diagnostic-data ${REPO_ROOT}/build/diagnostic/data-diagnostic-systembag.raw \
  --breakpoint userclient_start=0xffffff8000c5cf18 \
  --max-hits userclient_start=0 \
  --user-hooks backend/compiler_connection_hooks.json \
  --max-hits sb_compiler_=128
```

The prefix cap is supported by display_probe; all ten hooks receive it. The kernel hook is still needed to derive/install the verified shared-cache slide from backboardd. Do not set USE_MONOLITHIC_COMPILER or change the guest. Retain normal AGX environment settings for this first comparison and return them with `probe-invocation.json`; no guest-time or replay behavior is deliberately changed here.

Return `breakpoints.json`, `user-hook-mapping.json`, `probe-invocation.json`, emulator log and new crash report. Require all ten hooks in the mapping; lazy mappings can be absent at installation but their code must verify when hit. Check counts: **128 hits means truncation**, so increase the prefix cap for a repeat before using absence of an event as evidence. Track `(PID, connection address, construction generation)`, not a heap address alone.

Interpretation:

1. Find the failed disposer by `reply_after_xpc_release` connection word 1, or correlate the final crash's invoke/captures. Compare its connection bytes at ctor, ReleaseReply, enqueue, invoke-before-xpc_release, and after-xpc_release. Decode a string ISA as above if present.
2. An enqueue followed by `destroy/delete` for that same generation before invoke, followed by string-shaped bytes, establishes the pending-block UAF. Caller LR/frames at owner_destroy and ReleaseReply identify teardown and shader cleanup. Check earlier request statuses for an initiating compiler/service error; do not equate the guest lifetime bug with exoneration of all emulator behavior.
3. If bytes change only across xpc_release, compare captured reply and connection addresses/headers for an alias or ownership error. If the object is already wrong at ReleaseReply, investigate earlier corruption/stale ownership using that caller rather than changing dispatch.
4. If the connection is constructed with a valid queue, never deleted in a complete trace, and later changes, the next discriminant is a write/physical-alias trace of its dynamically discovered `+8` field. At that point inspect CPU PTE/PA and compare AGX/storage DMA destinations. A separate run with host `VPHONE_AGX_REPLAY=0` can isolate replay writes, but changed scheduling/survival by itself is not proof. Broad HLE disable or guest patches would obscure the lifetime evidence.

## Validation and limits

`python3 -m unittest discover -s tests -p 'test_*.py'`: **125 tests, OK, 4 skipped**. Tests exercise capture-pointer following, unreadable/reused storage, exact-PC register selection before/after xpc_release, and owner-slot resolution. `git diff --check` passes. No backend binary/emulator was run or built; unit tests do not establish runtime survival. The next runtime capture remains necessary before an emulator fix can be justified.
