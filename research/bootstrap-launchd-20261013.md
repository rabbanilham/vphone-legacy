# Native progression from HFS mount to process 1 exec

Source: the preserved original iPhone6,2 n53ap iOS 7.1.2 11D257 kernel, SHA-256 `1e0f584ff54040ded8a2ef4ed532b4298f2500b774fc14d88020453c91465666`. Exact bytes and locally decoded A64 instructions are retained in `research/firmware-artifacts/bootstrap-launchd-native-disassembly.txt`. These are original instructions, not loader patches or invented boot messages. REA was unavailable for this probe; the strict Mach-O VA mapping and Capstone method used previously were retained.

## Runtime observations already independently checked

`backend/evidence/hfs-mount-20261013/after-mount/registers.txt` stops at HFS callback **0xffffff80004b8c40**, immediately after mountfs, with X0/W0 zero. Its HMP capture has nonzero device `rootvp` at global **0xffffff80006940e0** and zero directory `rootvnode` at **0xffffff8000694090**. The separate `bootstrap-return/registers.txt` stops at **0xffffff80004e61fc**, also W0 zero. This proves both the mountfs return and its enclosing filesystem mount callback return; it does not yet prove process 1 exec.

At the latter stop X19=0xffffff800067cfc0, X22=0xffffff800067afc0, X23=0xffffff8000694000. These values corroborate the concrete global addresses below. The root directory pointer is populated later, so zero rootvnode at these stops alone is not a mount failure. The later launchd exec-call probe timed out before its breakpoint, with a kernel-idle PC; that observation does not identify a waiting thread or daemon.

## Exact intervening stages and bounded probes

| Stage / probe | Native VA and useful state |
| --- | --- |
| Filesystem mount callback returns | **0xffffff80004e61fc**; W0 errno; zero proceeds to mount bookkeeping |
| Platform registration polling branch | **0xffffff80004e64d8** loads X0 from **0xffffff800067d190**; zero branches to 4e64c8 delay and loops |
| Platform `SecureRootName` call returns | **0xffffff80004e6508**; W0 IOReturn; X19 receives it; actual return handling continues at 4e6524 |
| Directory root callback entry | **0xffffff80004bc300**; X0 mount, X1 address of `rootvnode`, X2 VFS context |
| Directory root callback returns | **0xffffff80004e65c8**; W0 errno; rootvnode at **0xffffff8000694090**; nonzero takes panic branch at 65cc |
| `/dev` name lookup returns | **0xffffff80004e6730**; W0 errno; its nameidata buffer is SP+0x220; failure takes 6968 and still proceeds to creation stage |
| Child process PID assignment | **0xffffff80005024c0**, just after native store; X19 child proc, uint32 **[X19+0x10]** PID |
| Child proc final publication path | **0xffffff8000503024**; X19 child proc, before 505700 call |
| Bootstrap child-thread creation returns | **0xffffff80004e69cc**; X0 new thread or null; native callee 502e6c returns its X22 thread at 503038 |
| One-time call into load-init | **0xffffff8000205348**; caller has set byte **[0xffffff800067a072]=1** at 205344 |
| Load-init entry | **0xffffff80004e6b44**; current proc obtained at 6b6c and retained in X19 |
| Launchd exec call | **0xffffff80004e6dc8**; X0 proc, X1 argument structure SP+0x38, X2 retval pointer SP+0x1c |
| Launchd exec returns | **0xffffff80004e6dcc**; W0 errno, X19 proc, X20 literal kernel path; zero continues, nonzero panics at 6ddc |

The platform wait is exact: bootstrap creates the symbol **`SecureRootName`** from original string VA **0xffffff80005eb094**, then waits for the platform global to be registered. Exported `IOService::setPlatform` at **0xffffff80002aa84c** stores that pointer into 0xffffff800067d190 at 2aa854. If present, bootstrap invokes its vtable slot +0x3a0 at 4e6504, with wait boolean zero and X3 loaded from SP+0x48: **the pointed-to root-name buffer is global 0xffffff800067da68**, observed as `disk0s1`, rather than the stack slot itself. The original IOPlatformExpert vtable proves this slot maps to exported `IOPlatformExpert::callPlatformFunction` at **0xffffff80002c83d0**. Its subclass can override the slot; capture the live pointer before attributing a wait. A no-platform polling loop and a blocking platform function are separate conditions.

The root-directory callback is identified by table data: original HFS vfsops **0xffffff800062a7f8**, slot +0x18 points to **0xffffff80004bc300**. It loads private HFS mount state, uses catalog ID **2** and tailcalls 4be86c. Bootstrap passes the address of rootvnode and branches on returned W0 at 4e65c8. This is later than the mountfs/mountroot callbacks. On success it marks the directory vnode at 65f4–6600 and proceeds to `/dev` setup and process creation.

Child creation uses native 502e6c at 4e69c8; its process allocator assigns the PID at **5024bc**. Do not assume PID one until measured. Exported `_proc_pid` at **0xffffff800026e910** is exactly `ldr w0,[x0,#0x10]; ret`, proving the field. At the creation return, a native thread's task pointer is +0x6b0 and the task's BSD proc pointer is +0x2e0, corroborated by original `_current_proc` fallback at 28925c–64. Thus, when nonnull, inspect `proc = *(*[thread+0x6b0]+0x2e0)` and then proc+0x10; direct allocator/publication probes avoid chasing a not-yet-initialized pointer.

## Launchd path, exec arguments and failure meaning

Load-init copies literal **`/sbin/launchd`** at **0xffffff8000629ff8** into a user allocation using exported `_copyout` at 4e6d18. A possible boot-option argument is appended only when the original boot flag selects it. Immediately before 4e6dc8, the argument structure is:

| Field | Meaning / native source |
| --- | --- |
| uint64 SP+0x38 | User filename pointer; literal source is `/sbin/launchd` |
| uint64 SP+0x40 | User argv vector pointer |
| uint64 SP+0x48 | env pointer, zero |
| uint64 SP+0x50 | extended fourth argument, zero |
| X19+0x10 uint32 | Actual current proc PID |
| X19+0x254 char array | Proc name set to original string `init` at 6b84–98, before exec |

The original wrapper 4fd878–98 packs three arguments and a zero fourth argument before invoking the same **4fd8a0** exec handler. That handler copies the first three fields into its image-parameter object and invokes native image activation at **4fda80**. Additional diagnostic breakpoint **4fda84** exposes its image-activation return in W0; proc is X20 and image-parameters object is X21. On successful image activation the handler proceeds to executable-state completion helpers at 4fdaa8/4fdab4. The final errno is returned at **4fdbd4–4fdbf4**.

At **4e6dcc**, nonzero W0 is passed along with X20 path to original panic string VA **0xffffff80005dde21**, `Process 1 exec of %s failed, errno %d`, at **4e6ddc**. A zero exec return is necessary evidence of successful executable setup, but actual userspace instruction execution must be corroborated separately by the guest CPU's EL0 PC/process state. No launchd execution, SpringBoard, app usability or graphical success is claimed by this static report.


## Next runtime blocker proven: AppleARMPE SecureRoot handshake

The actual indirect call in `secure-root-call/registers.txt` stops at **4e6504** with X8=**0xffffff8000805040**, X0=AppleARMPE object **0xffffff8081e8ce00**, X1=SecureRootName OSSymbol, X2=0, X3=**0xffffff800067da68**, X4/X5/X6 zero. Its HMP capture reads `disk0s1` from X3. The implementation is in original `com.apple.driver.AppleARMPlatform`, not the base IOPlatformExpert method.

The original AppleARMPE start path **804e38–804e70** reads provider property **`secure-root-prefix`** (string VA 822684), obtains its OSData bytes and stores their pointer at object +0x110. Immutable `original/DeviceTree.raw` root property is **`md\0`**, retained in the runtime DeviceTree; the live pointer 0xffffff80012bc06c is runtime allocated data, not a file-backed Mach-O address. `secure-root-provider` HMP capture reads `md` from it. No literal `secure-root-prefix` was found in the preserved iBoot, which is not proof that an indirect bootloader mutation never occurs. Native normal-boot policy has not been reconstructed sufficiently to justify altering that property.

In **SecureRootName** branch **80516c–805214**:

1. If object +0x110 is null, return zero immediately.
2. Otherwise compare that prefix against the actual root name using strlen/strncmp at **805194–8051ac**.
3. Store prefix-match bool into byte **+0x10c** at **8051c0**, set root-name-supplied byte **+0x10a=1** at **8051c8**, and wake its waiters at **8051d4**.
4. At **8051d8–8051fc**, wait while completion byte **+0x10b=0**, using imported native `_assert_wait`, `_thread_block` and a recheck.
5. Only after the counterpart completes does it return success for matched root, or native 0xe00002c1 for mismatched root at **805200–805214**.

The live `secure-root-wait/registers.txt` breakpoint **8051d8** and `hmp-0.txt` prove bytes +0x108..10f are `00 00 01 00 00 00 00 00`: **supplied=1, completed=0, matched=0**. Prefix `md` versus root `disk0s1` explains matched zero; the actual blocking condition is completed zero. This identifies a kernel handshake wait before root-directory acquisition. It is not evidence of a blocked userspace daemon, launchd failure or SEP dependence.

### Native counterpart and bounded next investigation

Original **IOCryptoAcceleratorFamily** contains **IOAESAccelerator**. Its start entry **0xffffff80008fef14** reaches **8ff0fc–8ff124**, invoking provider `SecureRoot` with parameter one = the AES object and parameter two null. AppleARMPE SecureRoot branch **80509c–8050ec** waits until root-name-supplied byte +0x10a is nonzero. With null parameter two it calls the AES object's `SecureRootCallBack` at **8052d4**, passing the measured prefix-match boolean. The AES dispatcher **8ff6c8**, recognition **8ff704–8ff714**, stores that boolean in its +0x111 at **8ff720**, calls initialization **8ff728→8ff320** and virtual slot +0x570 at **8ff73c**. After the callback actually returns, AppleARMPE stores **completed +0x10b=1 at 8052e4** and wakes at **8052f0**. A zeroed memory flag or fabricated wake-up is not equivalent to the callback having run.

The `aes-root-registration` runtime probe did not reach **8ff0fc** within 15 seconds; the separate `aes-start-entry` probe did not reach **8fef14** within 15 seconds either. Both retain failure records, timeout registers, serial, trace and QEMU exit after explicit quit. Those negative bounded observations distinguish these runs from a measured callback that starts and blocks. They do not by themselves prove an AES provider can never appear.

Original kernel prelink personalities define the provider chain:

- `AppleSmartIOAESAccelerator`, provider **AppleSmartIO**, match category **AES** (`com.apple.driver.AppleS5L8960XSmartIO`, load VA **0xffffff8000918000**).
- `AppleSmartIO`, provider **AppleA7IOP**, property-match role **SIO**.
- `AppleA7IOP-SIO`, provider **AppleARMIODevice**, IONameMatch **`iop-sio,s5l8960x`**, role **SIO**.
- Immutable DT node **/device-tree/arm-io/sio** has compatible `iop-sio,s5l8960x`, relative reg **0x0ae00000**, size **0x2000**, interrupts **133,132,135,134**, clock IDs **0x118,0x119**, iop-version one. It carries no region-base/region-size or recovered SIO firmware payload in this node. With existing arm-io base 0x200000000, MMIO is **0x20ae00000**.

Next bounded probes are native AES start **8fef14**, registration **8ff0fc/8ff124**, AES callback entry **8ff6c8**, and AppleARMPE callback return **8052d8** versus flag write **8052e4**. A missing SIO/SmartIO provider and a callback blocked inside native AES initialization must be diagnosed separately. Retain original DT policy; deleting secure-root-prefix takes the native startup default that also marks completion/trust bytes, and is a diagnostic bypass rather than recovered device compatibility. No firmware or loader patch has been made by this report; no cryptographic hardware key or root trust result has been fabricated.


### Mismatched prefix return continues bootstrap

After the actual counterpart completes, native matched=false returns **0xe00002c1**. Bootstrap **4e651c–4e6530** compares W19 with this value. If equal, **4e6534–4e6570** clears flagged entries in a sixteen-element native table at global **0xffffff8000686cc0**, stride 0x30, releasing pointer fields +0x20/+0x28 through 23bdfc. Other return values skip that cleanup. **Both paths continue at 4e6574 to root-directory acquisition**; neither retries nor panics at this SecureRoot result handling. Therefore `md` prefix versus `disk0s1` must not be described as requiring a prefix edit. The measured block is the missing real completion handshake, independently of its eventual match result.

`research/firmware-artifacts/bootstrap-native-metadata.json` retains the decoded original prelink personality chain and exact original SIO DT properties with kernel/DT hashes. No broad SIO reverse engineering or firmware handoff claim was added; the focused next work remains determining whether native IOAES start 8fef14 is entered and which native SIO provider prerequisite prevents its registration path.

## Verified progression after SIO SmartIO / AES contract implementation (2026-10-13)

With the recovered `AppleA7IOP` / `AppleSmartIO` / `AppleSmartIOAES` contract implemented at `0x20ae00000` (IRQ 135, OS-Active message `0x00000000016d0000`, `EP=0` `setParam`/`getParam`, `EP=1` `performImmediateAESQuantum` / `performAESQuantum` / `disableHardwareKey` / `isKeySupported`), the native bootstrap and `/sbin/launchd` chain was verified on diagnostic disk copies in `backend/evidence/hfs-mount-20261013/`:

1. **`smartio-start-cpu-return/` (`0xffffff8000918a60`)**: `AppleSmartIO::start` and `AppleA7IOP::startCPU` return `X0=0` after writing `0x20ae00008=0x800f25000`, `0x20ae00018=0x8b000`, `0x20ae00028=0x10`, `0x20ae01020=0x1`.
2. **`secureroot-callback/` (`0xffffff80008ff6c8`)**: `IOAESAccelerator::start` registers `SecureRoot` and enters `SecureRootCallBack` (`X0=0xffffff8081f1a600`, `X2=0`).
3. **`secureroot-return/` (`0xffffff80004e6508`)**: After 6 inline UID key derivations (`EP=1, OP=0x0b, slot=0..5 -> 0x658101..0x658601`) and `disableHardwareKey(0x3e8)` (`EP=1, OP=0x0c, param=0x1e -> 0x1e658701`), `AppleARMPE::SecureRootName` sets `+0x10b=1` (`hmp-0.txt`: `00 00 01 01 00 00 00 00`) and wakes bootstrap at `0xffffff80004e6508` with `X0=0xe00002c1`.
4. **`rootvnode-ready/` (`0xffffff80004e65c8`)**: `VFS_ROOT` (`hfs_vfs_root`) returns `W0=0` and populates `rootvnode` at `0xffffff8000694090 = 0xffffff80820e6960`.
5. **`launchd-exec-call/` (`0xffffff80004e6dc8`)**: `load_init_program` reaches `__mac_execve` call with `X19=0xffffff8082202bd0`, `[X19+0x10]=0x00000001` (PID 1), `X20=0xffffff8000629ff8` (`"/sbin/launchd"`), and `[SP+0x38..+0x50] = {0x1000, 0x1080, 0, 0}`.
6. **`launchd-exec-return/` (`0xffffff80004e6dcc`)**: `__mac_execve("/sbin/launchd")` returns `W0=0` (`X0=0`), confirming successful Mach-O image activation of `/sbin/launchd` in process 1.
7. **`post-launchd-15s/serial.log`**: EL0 `/sbin/launchd` executes in userspace, prints `com.apple.launchd 1 *** launchd[1] has started up. ***`, runs `fsck_hfs` on `/dev/rdisk0s1` and `/dev/rdisk0s2` (`QUICKCHECK ONLY; FILESYSTEM CLEAN`), mounts `/dev/disk0s1` on `/` and `/dev/disk0s2` on `/private/var`, and then reaches the next genuine userspace blocker:
   `FIPS USER Space POST: Integrity test failed!`
   `FIPS_POST failed!com.apple.launchd 1 System: Userspace shutdown begun`.

