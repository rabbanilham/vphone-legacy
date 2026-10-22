# T22: TriStar system-ID forwarding and probe139 discrepancy

2026-10-22. Worktree `/Users/bagas/Downloads/vphone-wt-tristar-id`, branch `wt/tristar-id`.

**Partial result; the panic is not fixed.** The supplied kernel's provider helper does **not** translate logical register 0x20 to hardware register 0x5d. It requests a direct three-byte read at 0x20 and receives bytes in FIFO order. The existing peripheral payload `74 00 00` already satisfies that static contract. Probe139 contradicts that contract at both the outgoing prefix and the caller's received marker. The trace alone cannot identify the divergence. Changing byte order, inserting a status byte, or inventing an IPC command is unsupported by the recovered code.

Changes: correct the misleading board comment, retain the existing 0x5d compatibility alias, preserve all model behavior, and add actual probe139 transaction replay plus the statically recovered native sequence. These tests establish the peripheral contract, **not native AppleTriStar2 start completion**. No QEMU build, QEMU run, guest patch, boot argument, or forced successful return was performed.

## Inputs

Kernel `/Users/bagas/Downloads/vphone-legacy/build/ios712-validated/boot/KernelMemory.bin`, SHA256 `bba9a4bace9b505b80c05bc15648b46122c8948f86ac4f0d0625f7804d78d7d8`. Disassembled with Capstone in `/Users/bagas/Downloads/vphone-legacy/.venv/bin/python`. All addresses are full unslid VAs with `fileoff = VA - 0xffffff8000a00000 + 0x1000000`.

Supplied scratchpad `probe139/i2c0-stream.txt`, SHA256 `c4fc222a44fb5bf4ed4917db77ac1dfe7e74614e1bf8e3dd814a10f5b7c55ca9`; `probe139/serial.log` reports the stated bad IPC message ID panic at caller 0xffffff800088a600. No runtime provider object, vtable, prefix memory, or destination-buffer dump accompanies these files.

## Provider class and exact helper

The kernel's AppleTriStar2BuiltIn personality at 0xffffff800127ab1f selects IOClass **AppleTriStarBuiltIn**, IOName **AppleTriStar2**, IOProviderClass **AppleARMIICDevice**, and TriStarICClass **AppleCBTL1610**. AppleTriStarBuiltIn extends AppleTriStarDock. Its start at 0xffffff8000e9c97c casts the supplied nub and stores it at provider+0x168 (e9c994..9b4). The CBTL base start at 0xffffff8000e9c4a0 casts its provider to AppleTriStarDock (metaclass ea2698) and stores that object at CBTL+0x108 (e9c4cc). Thus +0x108 is the TriStar service, not the I2C controller or the accessory/ID bus.

The BuiltIn constructor/allocation paths install vtable **0xffffff8000ea1010**. Its entry at **0xffffff8000ea1660** (vtable+0x650) points to **0xffffff8000e9cc78**; the write entry +0x648 points to e9cc30. This identifies the implementation selected by the supplied personality; the actual runtime object/vtable still needs capture to rule out divergence from the static image.

Read helper, all relevant instructions:

```text
e9cc78 stp x29,x30,[sp,#-16]!
e9cc7c mov x29,sp
e9cc80 sub sp,sp,#16
e9cc84 mov x9,x3                 ; requested byte count
e9cc88 mov x8,x2                 ; destination buffer
e9cc8c sub x3,x29,#1             ; address of local prefix byte
e9cc90 sturb w1,[x29,#-1]        ; store original register, unchanged
e9cc94 ldr x0,[x0,#0x168]        ; AppleARMIICDevice nub
e9cc98 ldr x10,[x0]
e9cc9c ldr x10,[x10,#0x558]
e9cca0 mov w1,#1                 ; read mode
e9cca4 mov x2,#1                 ; one prefix byte
e9cca8 mov x4,x9                 ; requested data count
e9ccac mov x5,x8                 ; destination buffer
e9ccb0 blr x10
e9ccb4 mov sp,x29
e9ccb8 ldp x29,x30,[sp],#16
e9ccbc ret
```

No arithmetic, register lookup table, command exchange, or logical-address remapping occurs. The write helper e9cc30 similarly forwards the original register byte, using mode 2.

## Nub, controller, and byte layout

AppleARMIICDevice's instantiated vtable is **0xffffff8000829620** (see allocation at 800a04..a50); +0x558 resolves to **0xffffff8000800bbc**. Do not confuse it with its metaclass vtable at 829ba0.

At 800bfc..c0c it builds a transfer descriptor starting at SP+8:

| Descriptor offset | Value |
|---|---|
| +0 | nub object |
| +8 | 32-bit mode |
| +0x10 | prefix count (1) |
| +0x18 | prefix pointer |
| +0x20 | data count (3) |
| +0x28 | data destination pointer |
| +0x30/+0x34 | zero initial completion fields |

The gate/direct paths at 800c10..c5c pass this descriptor to the controller. Neither changes the prefix or adjusts the data pointer. Controller transfer starts at **0xffffff8000dbadfc**, stores the descriptor at controller+0x138, and transmits prefix bytes with `ldrb w11,[x10],#1` at **dbaeb8**, followed by FIFO writes at dbaed8/dbaeec. No prefix mapping exists here either.

For a read, **dbaf24..30** stores descriptor+0x20 as controller+0x148 (count) and descriptor+0x28 as controller+0x150 (buffer). The receive loop is:

```text
dbb9ec mov w8,#0
dbb9f0 ldr x10,[x19,#0xe8]
dbb9f4 ldr w9,[x10,#4]            ; RX FIFO word
dbb9f8 tbnz w9,#8,dbb9f4          ; empty flag only
dbb9fc ldr x10,[x19,#0x150]
dbba00 strb w9,[x10,w8,uxtw]      ; byte i directly to destination[i]
dbba04 add w8,w8,#1
dbba08 ldr w9,[x19,#0x148]
dbba0c cmp w8,w9
dbba10 b.lo,dbb9f0
```

At **e9ea24..3c**, getSystemID sets destination to caller SP+0xd, register to **0x20**, count to **3**, and calls that provider slot. At e9ea40 it propagates a nonzero transfer result. Otherwise e9ea4c loads exactly destination[0], e9ea50 compares it to **0x74**, and e9ea54 branches on mismatch. It decodes `(destination[1] << 2) | destination[2]` and stores a halfword at e9ea64..68. There is no byte swap, discarded length/status byte, separate ID-bus/ACCID request, or second system-ID read in this path. “bad IPC message ID” is the name associated with the marker-error return 0xe00002c6, not evidence of an IPC transport transaction.

Expected bus sequence is consequently **W134 W220 W135 W603 -> R074 R000 R000**. This would put 0x74 at SP+0xd and return synthetic ID zero. The model already supplies those bytes. The captured **W25d** and subsequent marker failure cannot be derived from this code. Possible categories include runtime code/object differences, buffer/prefix corruption, or capture effects; none is established by the available evidence.

## Every TriStar transfer in probe139

The fixture `tests/fixtures/probe139_tristar.json` preserves TX words and observed RX bytes, in original order, excluding other slaves and controller bookkeeping:

| TX commands | Observed RX | Model |
|---|---|---|
| 134 20f 135 601 | 20 | synthetic REV |
| 134 00e 201 | none | mask stored as 1 |
| 134 20d 135 601 | 00 | no events |
| 134 20a 135 601 | 00 | reset register storage |
| 134 25d 135 603 | 74 00 00 | retained compatibility alias |
| 134 20c 135 601 | 04 | cable absent, identity-ready clear |

All values are hexadecimal. Other slave transfers are interleaved; this table does not establish all transactions belong to one thread. In particular the final status read is not a second system-ID read. No initial ACCID/IPC write to TriStar appears in the capture. Mux registers 1/2 and mask 0xe remain writable; status/events/revision and the ID payload remain stable. The empty connector has no fabricated event/identity. The trace ends at the panic and cannot prove which additional transfers would follow successful start.

The replay resets/clears the controller before each transaction, verifies no NACK, RX occupancy, FIFO drain order, empty FIFO, and retained mask 1. A second replay changes **only** the inconsistent 25d prefix to 220. It also places three received bytes in an offset host buffer and checks marker/ID decoding. That host copy is an explicit contract check, not execution of the native kernel's copy loop. Existing mux/reset/isolation/malformed-window/backlight checks remain.

## Next decisive maintainer capture

The missing observations are before and after the native helper and at the marker load. Add these arguments to the normal display_probe invocation (same plan/disks/environment as probe139):

```sh
--breakpoint tristar_read_setup=0xffffff8000e9cc94 \
--hook-dump tristar_read_setup=x0:0x180 \
--hook-dump tristar_read_setup=x3:1 \
--hook-dump tristar_read_setup=x8:3 \
--breakpoint tristar_read_done=0xffffff8000e9ccb4 \
--hook-dump tristar_read_done=sp+0x2d:3 \
--breakpoint tristar_id_check=0xffffff8000e9ea4c \
--hook-dump tristar_id_check=sp+0xd:3 \
--max-hits tristar_=32
```

At setup, w1 must still be 0x20 for the ID request, x9=3, *x3=0x20, and x8 is the original destination; provider dump +0 contains the actual vtable and +0x168 the nub. At done, current SP is caller SP minus 0x20, so the original buffer is SP+0x2d; w0 should be zero. At check, SP+0xd must match the exact same buffer address and bytes. These hooks use existing bounded read-only capture support. They have not been run here.

If setup already has a wrong prefix, inspect live instructions e9ea24..3c/e9cc78..b0 and provider vtable. If setup is correct but transmitted prefix is 5d, capture dbaeb8 x10:1 and x20:0x30 to compare the controller's descriptor/prefix pointer. If helper-return bytes differ from FIFO data, capture dbba00's w8 index, w9 received word, and x10 destination, plus controller+0x150. If helper return has 74 but marker check does not, locate the intervening write/address mismatch. These discriminate the failure without inventing peripheral framing.

## Verification

- `python3 backend/busy_i2c_contract_test.py`: passes actual extracted board C, including captured and native-prefix replay.
- `python3 -m unittest discover -s tests -p 'test_*.py'`: 148 tests OK, 4 skipped.
- `git diff --check`: passes.
- `/* The TA command whose parameter buffer` remains in `backend/s5l8960x.c`.

**Outstanding:** identify the runtime prefix/buffer divergence and verify native system-ID success and full AppleTriStar2 start in the maintainer run. This commit does not claim those acceptance criteria are met.
