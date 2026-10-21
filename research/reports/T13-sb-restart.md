# T13: SpringBoard early-startup SIGSEGV

2026-10-22, branch `wt/sb-restart`.

## Status

**The launch-watchdog explanation is wrong for this incident.** The immediate failure is `dispatch_release(invalid_object)`, while executing a block from a dispatch continuation on a worker thread. **The supplied evidence does not identify that block's invoke function or its enqueue site, and does not establish whether the underlying cause is emulator or guest code. No emulator fix is justified yet.** This report and the committed diagnostic changes are an investigation checkpoint, not a claimed resolution.

A maintainer probe has been requested. It captures the missing block, continuation, and current queue at the actual bad release. Do not patch the guest, suppress the fault, force keybag results, or broaden TLB invalidation based on the present evidence.

## Inputs and address mapping

Read `CLAUDE_HANDOVER.md` sections 14–17, the existing jump-cache patch, the ARM TCG exclusive translator, the ANS clock handling, and the SEP/keystore reply handlers. Upstream source and all guest artifacts were left unchanged.

Inputs:

- Crash: `scratchpad/probe108-crashlogs/SpringBoard_2106-02-06-222631_iPhone.ips`, SHA256 `9732cf834254ad6b2b2f8c18ec0c903e3a2420b91fb03df678cd72e0bd359c7c`.
- Cache: `/Users/bagas/Downloads/vphone-legacy/backend/evidence/ca-surface-20261014-214745/dyld_shared_cache_arm64`, SHA256 `d3d6e3be15108b04c33e36f62afb9230400c348cc63a11b2fdc91c82b2961427`, with adjacent `cache-index.json`.
- Executable: `/tmp/t4-touch-evidence/SpringBoard`, SHA256 `7ac0648541d8b4efef35bff46de2c5b2e0aa45e48fdd1f902e580eac5ca22620`.

`LatestCrash.ips` and `LatestCrash-SpringBoard.ips` repeat the same incident identifier and registers. The supplied directory has one independent crash, not three. `probe108-breakpoints.json` has 16 events and attributes SpringBoard work to PIDs 38 and 102, confirming a respawn. It contains no worker block/continuation heap dump. No additional `.ips` was found beneath Retired.

The binary image list says runtime libdispatch base is `0x196664000`; the cache index says unslid base `0x18e178000`. Therefore:

```
cache slide = 0x084ec000
unslid VA = runtime VA - 0x084ec000
cache file offset = mapping.fileoff + unslid VA - mapping.address
```

Cross-check: MobileKeyBag `0x190138000 - 0x187c4c000` and IOKit `0x18aa80000 - 0x182594000` give the same slide. The cache's executable mapping starts at `0x180000000`, file offset 0, so fault VA `0x18e17cd94` is file offset `0x0e17cd94`.

SpringBoard has a separate slide: runtime base `0x1000f4000` minus preferred base `0x100000000` = `0xf4000`. Subtract it before using `fileoff = preferred VA - 0x100000000`.

Disassembly used Capstone from `/Users/bagas/Downloads/vphone-legacy/.venv/bin/python`. Public names below were checked against Mach-O symbol tables; SpringBoard method names were recovered with `otool -ov`. Names for redacted dispatch internals are inferred from instruction behavior and compared with Apple's era-adjacent [libdispatch-339.92.1 queue.c](https://github.com/apple-oss-distributions/libdispatch/blob/libdispatch-339.92.1/src/queue.c) and [queue_internal.h](https://github.com/apple-oss-distributions/libdispatch/blob/libdispatch-339.92.1/src/queue_internal.h); those sources are corroboration, not proof of the exact iOS build.

## Fault and provenance established by the instructions

Unslid faulting function, publicly exported as `_dispatch_release`:

```asm
18e17cd94  ldr   x8, [x0]                 // fault: read object isa/vtable
18e17cd98  adrp  x10, 0x191042000
18e17cd9c  add   x10, x10, #0xb50
18e17cda0  and   x9, x8, #1
18e17cda4  cmp   x8, x10
18e17cda8  ccmp  x9, #0, #0, hs
18e17cdac  adrp  x9, 0x191043000
18e17cdb0  add   x9, x9, #0x48
18e17cdb4  ccmp  x8, x9, #2, eq
18e17cdb8  b.lo  0x18e17cdc0
18e17cdbc  b     0x18e17c728              // Objective-C release route
18e17cdc0  b     0x18e17d014              // _os_object_release route
```

The OS-object route reads the external reference count at object `+0xc`, then decrements it with `LDXR W8` / `STXR W11`; **none of that executes in this crash**. The failed field is object `+0`, not a queue next link, block capture, group count, source state, or exclusive-load instruction. `x0=0x10000078c` is the supplied object address itself, not an offset derived by dispatch_release. It is also not 8-byte aligned. The exception is an invalid mapping, not an alignment exception.

The preceding redacted routine is identifiable as `_dispatch_call_block_and_release`:

```asm
18e17bffc  stp   x29, x30, [sp, #-16]!
18e17c000  mov   x29, sp
18e17c004  stp   x20, x19, [sp, #-16]!
18e17c008  mov   x19, x0                  // preserve block
18e17c00c  ldr   x8, [x19, #0x10]         // Block_layout.invoke
18e17c010  blr   x8                       // invoke(block)
18e17c014  mov   x0, x19                  // NOT yet executed
18e17c018  ldp   x20, x19, [sp], #16
18e17c01c  ldp   x29, x30, [sp], #16
18e17c020  b     0x18e191d58              // block-release stub
```

`LR=0x196668014` is exactly the return from that BLR. Thus the failure is **inside the block invocation, before the wrapper releases the block**. This is not `_Block_release` crashing on the block allocation. The block likely tail-calls dispatch_release, possibly through other tail calls, explaining why its own frame is absent. Corrupted control flow or stale execution remains another possibility; a frame-less path is not proof of a legitimate tail call.

Under ordinary AAPCS64 preservation, `x19=0x1706546a0` is the block passed to the wrapper. Its `[+0x10]` invoke and captures at `[+0x20...]` are not in the report. It is distinct from bad object `x0`. The report cannot reveal which capture/global/member supplied x0.

The worker frames map as follows:

| Runtime | Unslid | Interpretation |
|---|---|---|
| `196668d94` | `18e17cd94` | dispatch_release entry |
| `196668010` | `18e17c010` | block invoke BLR |
| `196667fd0` | `18e17bfd0` | `_dispatch_client_callout`: BLR x1 |
| `19666e4a4` | `18e1824a4` | queue drain, continuation pop inlined |
| `19666a4bc` | `18e17e4bc` | queue invoke calling drain |
| `19666f0f0` | `18e1830f0` | root queue drain invoking a queued object |
| `19666f4f8` | `18e1834f8` | worker-thread entry calling root drain |

The critical drain instructions are:

```asm
18e182490  mov   x22, #0
18e182494  ldrb  w8, [x27]               // continuation flags
18e182498  tbz   w8, #2, 0x18e1824a0
18e18249c  ldr   x22, [x27, #0x28]       // optional dispatch group
18e1824a0  ldp   x1, x0, [x27, #0x18]    // dc_func, dc_ctxt
18e1824a4  bl    0x18e17bfc4             // client_callout(context, func)
18e1824a8  cbz   x22, 0x18e1824bc
```

Here `x27=0x170676b00` is the continuation candidate, `dc_func +0x18` selected call_block_and_release, and `dc_ctxt +0x20` supplied the block candidate. `x22=0` is consistent with no attached group on this pop. `x28=0xa` is a constant used elsewhere in the drain; **it is not this continuation's flags**. Async continuations may already have been returned to the thread cache before the callback, so interpret a late dump cautiously.

The drain holds the pthread/TLS base in `x24=0x1008680e0`; current queue is stored in its `+0xa0` slot. The queue header/label is also missing. Standard dispatch_async, dispatch_after, group APIs, and framework wrappers can all reach this path. **There is no justified enqueue-site attribution from the stack alone.**

## SpringBoard/main and remaining threads

Thread 0 has three nested one-time initializations; dispatch frame `+0x4e78` is `dispatch_once_f+0x38`:

| Runtime SpringBoard frame | Preferred VA | Meaning |
|---|---|---|
| `1002d0fcc` | `1001dcfcc` | `-[SBDeviceLockController isPasscodeLocked]`, after MKBGetDeviceLockState(NULL) |
| `1002d0464` | `1001dc464` | SBDeviceLockController init |
| `1002d023c` | `1001dc23c` | one-time block sending init and storing singleton |
| `1002d01fc` | `1001dc1fc` | `_sharedControllerCreateIfNecessary:` calls dispatch_once |
| `1003936f0` | `10029f6f0` | SBLockScreenManager init |
| `1003935b4` | `10029f5b4` | its singleton initializer block |
| `100393574` | `10029f574` | `_sharedInstanceCreateIfNeeded:` dispatch_once |
| `100240318` | `10014c318` | SBControlCenterController init |
| `100240200` | `10014c200` | its singleton initializer block |
| `1002401c0` | `10014c1c0` | `_sharedInstanceCreatingIfNeeded:` dispatch_once |
| `100334ccc` | `100240ccc` | SBScreenTimeTrackingController initWithAlertManager: |
| `100120b50` | `10002cb50` | SBWorkspace init |
| `1002fd4b4` | `1002094b4` | SBWorkspaceEventQueue _processNextEvent |
| `1002fce10` | `100208e10` | SBWorkspaceEventQueue _executeOrPendEvents:position: |
| `1000fb450` | `100007450` | SpringBoard applicationDidFinishLaunching: |

Preferred `100024dbc` lies in an unnamed block region between Objective-C methods; nearest preceding method is not sufficient to name that block.

MobileKeyBag `19013cea8 -> 187c50ea8` is `MKBGetDeviceLockState+0x60`. Its NULL-options branch calls the internal scalar query at `187c57e50`; frame `190143ea8 -> 187c57ea8` issues selector `0x11` with one scalar output. It reaches `IOConnectCallMethod` (`18aa8867c -> 18259c67c`), then a generated IOKit IPC routine (`18aadc018 -> 1825f0018`) and mach_msg_trap. This proves a key-state request is in flight; it does not prove the result or a relationship to the worker block.

Other threads contain no SpringBoard frames: thread 1 is dispatch/kernel wait, 2 and 5 pthread workers waiting in kernel, 4 UIKit/Foundation run-loop work, 6 CoreMedia/MediaToolbox async work, and 7 CoreMotion's run loop. There is no second thread stack naming the worker's block, queue, or producer.

## Emulator-versus-guest assessment

The answer is currently **undetermined**, not “genuine hardware race.” There is no hardware comparison or provenance of the bad value.

- **Atomic/exclusive implementation:** the observed queue path uses LDXR/STXR and LDAXR/STLXR. The inspected TCG translator retains the load address/value and implements stores with atomic compare/exchange and status generation. No defect was established. The fault is before dispatch_release's own refcount atomic operation. iOS 7's inspected dispatch code uses the older exclusive sequences; no LSE LDADD/CAS was observed on this path. This does not exonerate every atomic operation earlier in execution.
- **Ordering:** this board creates one AP CPU; instruction interleaving through guest scheduling remains possible. Neither a race nor an ordering defect follows from that fact. No publication/read trace was supplied.
- **Stale translation:** section 17 proves a different old-mapping bug, fixed by the active jump-cache patch. This incident lacks a guest-page-table/physical-instruction comparison or observed remapping for the invoke PC. The broad fallback also reportedly retained SpringBoard restarts, so applying it again has no supporting evidence. Capture the invoke PC and actual bytes first.
- **SEP/model returns:** current model replies to keystore Cmd17/25 with zero lock-state words (16/8 bytes), bounded to the receive OOL buffer. Main is querying state, but the crash report contains no reply value and no connection from that request to x0. Changing these results would mask or perturb behavior without explaining the bad object. Other DMA writers remain unexcluded until the corrupt field is identified.
- **Clock:** UTC crash time is `2106-02-07 06:26:31.557`, seconds `4294967191.557` (`0xffffff97` whole seconds), 104.443 seconds before unsigned 32-bit Unix wrap. Bad pointer `0x10000078c` numerically equals Unix seconds `4294969228` (UTC 07:00:28). This makes integer/pointer confusion or an overwritten time field worth testing. It is **not proof**: the crash lacks the field and write that created the value. ANS calendar code uses uint32 seconds advancing from the utility epoch; the origin of this boot's calendar value has not been traced. Do not claim a clock fix from this coincidence.

## Implemented diagnostic and verification changes

1. `backend/sb_restart_hooks.json`: exact unslid dispatch_release hook, named `sb_dispatch_release_unaligned` to select SpringBoard only.
2. `backend/display_probe.py`: skip ordinary aligned arguments without spending the suspicious-hit cap. On an unaligned call, read the object, surviving x19 block candidate (including invoke/descriptor and their bytes), x27 continuation candidate, stack, and x24 TLS current queue/header/label. Existing event capture preserves registers, instruction bytes and frame chain. These are candidate layouts, not assertions about arbitrary callers. No guest registers/memory are rewritten by the new diagnostic code.
3. `backend/crash_batch.py --process SpringBoard`: count the selected process's distinct PIDs. Preserve the historical backboardd default. Report unobserved runs and child exit status; return failure on a respawn, missing selected-process evidence, or failed probe. One recorded PID alone does not establish survival to the endpoint.
4. Four focused tests cover SpringBoard accounting independently of backboardd, missing evidence, unreadable candidates, and block/queue capture.

No board-model or QEMU execution patch is included. A future fix must target the first proven bad write/read or stale mapping, then reproduce a failing regression before being called a resolution.

## Exact next probe

The maintainer must provide normal build assets in this worktree (or integrate the diagnostic changes into their probe checkout). The worktree has no built backend, boot plan, or guest disks by default. The hooks use absolute read-only cache/index paths, so no cache symlink is needed.

From this worktree, with the usual backend/build assets available:

```sh
python3 backend/display_probe.py \
  --output /tmp/t13-sb-release \
  --seconds 90 --stop-condition duration \
  --plan /Users/bagas/Downloads/vphone-legacy/build/ios712-validated/boot/boot_plan.json \
  --diagnostic-system /Users/bagas/Downloads/vphone-legacy/build/diagnostic/system-diagnostic-sep.raw \
  --diagnostic-data /Users/bagas/Downloads/vphone-legacy/build/diagnostic/data-diagnostic-systembag.raw \
  --breakpoint userclient_start=0xffffff8000c5cf18 \
  --max-hits userclient_start=0 \
  --user-hooks backend/sb_restart_hooks.json \
  --max-hits sb_dispatch_release_unaligned=1
```

The kernel hook is required: user hooks are installed when it supplies a backboardd IPC frame from which the shared-cache slide can be verified. Without it the user hook is never installed. Inspect `user-hook-mapping.json` to confirm installation and verified bytes. This is a high-frequency diagnostic breakpoint despite the event filter; do not batch it. Normal calls incur debugger steps, which can perturb timing. If it stops reproducing, repeat with deterministic `--qemu-arg=-icount --qemu-arg=shift=1,sleep=off` (the old deterministic run is not guaranteed to reproduce this exact fault).

Return `breakpoints.json`, `user-hook-mapping.json`, `probe-invocation.json`, and the new crash report if different. Find event `sb_dispatch_release_unaligned`; compare its x0/LR with this incident, decode block invoke against the verified slide, and disassemble that function to find the precise load into x0. Decode block descriptor size/copy/dispose and captures, current queue label, then trace its producer. If the invoke bytes disagree with the cache, compare guest PTEs and physical code as in T12 before changing TCG. If the capture is wrong in RAM, hook its construction and watch its first change. Those observations select the emulator patch or establish a guest use-after-free; the present snapshot cannot.

## Acceptance after a justified fix

Use at least 8 clean, independent 180-second probes, and the deterministic configuration if it reproduces the same fault. Use the same starting images, timing, and kernel hooks in before/after batches. Exclude the dispatch_release diagnostic from acceptance because it changes scheduling.

```sh
python3 backend/crash_batch.py --out-root /tmp/t13-sb-fixed --runs 8 \
  --process SpringBoard --max-parallel 2 \
  --common '--seconds 180 --stop-condition duration --plan /Users/bagas/Downloads/vphone-legacy/build/ios712-validated/boot/boot_plan.json --diagnostic-system /Users/bagas/Downloads/vphone-legacy/build/diagnostic/system-diagnostic-sep.raw --diagnostic-data /Users/bagas/Downloads/vphone-legacy/build/diagnostic/data-diagnostic-systembag.raw --breakpoint userclient_start=0xffffff8000c5cf18 --max-hits userclient_start=0'
```

For every run require probe exit 0, SpringBoard observed, exactly one SpringBoard PID, no backboardd respawn/panic, and confirmation that this PID is still alive at the final endpoint (late task/thread snapshot or process listing). The batch's PID history is a respawn detector, not a liveness check: a lone early PID that dies without a later observed hook is insufficient. Read crash reports from each surviving run image if a PID changes. `parallel_probe.py` cleans copied images, so use `display_probe.py` directly when retaining disks/crashlogs is necessary.

## Local validation and limits

Final command: `python3 -m unittest discover -s tests -p 'test_*.py'`: **119 tests, OK, 4 skipped**. No iOS boot or crash-reproduction probe was performed. During setup I briefly exposed the existing backend binary via an ignored symlink; an intermediate unit-suite run automatically executed the optional SEP contract fixture. That returned PASS but is not validation of this crash. The temporary build/bin symlinks were removed, and the final suite ran without a backend binary. No guest disk changes were made.

Pending: maintainer runtime capture, classification of the underlying cause, justified emulator fix if applicable, and before/after runtime acceptance. These are required work remaining.
