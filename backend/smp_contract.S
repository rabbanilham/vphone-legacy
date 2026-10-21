/* Synthetic EL1 SMP contract. All addresses are physical; MMU stays off. */
.text
.globl _start
_start:
    msr daifset, #0xf
    adr x0, vectors
    msr vbar_el1, x0
    isb
    mov x4, #0x800000000
    add x4, x4, #0x20, lsl #12  // shared result page
    mov x5, #0x200000000
    movk x5, #0xe10, lsl #16    // AIC
    mrs x6, mpidr_el1
    and x6, x6, #1
    mov w0, #0x80000001
    str w0, [x5, #0x2028]      // unmask self and other IPIs, current CPU
    msr daifclr, #2
    cbnz x6, secondary
    // An IPI must not release the powered-off secondary.
    mov w0, #2
    str w0, [x5, #0x2008]
    mov w1, #0x1000
1:  subs w1, w1, #1
    b.ne 1b
    ldr w0, [x4]
    str w0, [x4, #20]          // must still be zero before PMGR release
    mov x7, #0x200000000
    movk x7, #0xe00, lsl #16   // PMGR
    add x7, x7, #0x30, lsl #12
    mov w0, #2
    str w0, [x7, #4]
    str w0, [x7, #8]           // enable_core(cpu1)
2:  ldr w0, [x4]
    cbz w0, 2b
    // CPU1 consumed the IPI queued while powered off. Send another after ACK.
3:  ldr w0, [x4, #8]
    cbz w0, 3b
    mov w0, #2
    str w0, [x5, #0x2008]
4:  ldr w0, [x4, #4]
    cbz w0, 4b
    mov w0, #0x80000000
    str w0, [x5, #0x2008]
5:  ldr w0, [x4, #12]
    cbz w0, 5b
    ldr w0, [x4, #16]
    cbz w0, 5b
    ldr w0, [x4, #8]
    cmp w0, #2
    b.ne 5b
    mov w0, #0xface
    str w0, [x4, #24]
6:  b 6b
secondary:
    mov w0, #0x11
    str w0, [x4]              // secondary actually executed guest instructions
7:  ldr w0, [x4, #8]
    cbz w0, 7b
    mov w0, #1
    str w0, [x5, #0x2008]     // cross IPI to CPU0
    mov w0, #0x80000000
    str w0, [x5, #0x2008]     // self IPI on CPU1
8:  b 8b

.balign 2048
vectors:
    .space 0x280
    b irq
    .space 0x800 - 0x284
irq:
    // Only these reserved registers are clobbered by the handler.
    ldr w10, [x5, #0x2004]
    mov w11, #1
    movk w11, #4, lsl #16
    cmp w10, w11
    b.eq other
    add w11, w11, #1
    cmp w10, w11
    b.ne failure
    mov w11, #0x80000000
    add x12, x4, #12
    b ack
other:
    mov w11, #1
    add x12, x4, #4
ack:
    add x12, x12, x6, lsl #2
    ldr w13, [x12]
    add w13, w13, #1
    str w11, [x5, #0x200c]
    str w11, [x5, #0x2028]
    str w13, [x12]
    eret
failure:
    str w10, [x4, #28]
    b failure
