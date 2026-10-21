/* Synthetic EL1 test. Flat raw image loaded at PA 0x800000000.
 * 4 KiB granules, 39-bit VA, identity RAM plus one remappable 2 MiB block.
 * Both code images exist before enabling the MMU; no self-modifying code.
 */
#ifndef TLBI_OP
#define TLBI_OP vae1is
#endif
.text
.global _start
_start:
    ldr x0, =0x800010000
    msr ttbr0_el1, x0
    mov x0, #0xff
    msr mair_el1, x0
    // IPS=36 bits, TG0=4K, SH0=inner, ORGN0/IRGN0=WBWA, T0SZ=25.
    ldr x0, =0x100803519
#ifdef TBI
    orr x0, x0, #(1 << 37)
#endif
    msr tcr_el1, x0
    isb
    mrs x0, sctlr_el1
    orr x0, x0, #1
    msr sctlr_el1, x0
    isb
    ldr x20, =0x40010000
    ldr x21, =0x800020000  // results, via identity RAM block
    blr x20
    str w0, [x21]
    // Break before make. VA at start of block invalidates the whole block,
    // including executed code 64 KiB away, outside the page jump-cache clear.
    ldr x22, =0x800011000
    str xzr, [x22]
    dsb ishst
    ldr x0, =0x40000
    tlbi TLBI_OP, x0
    dsb ish
    isb
    ldr x0, =0x800400701
    str x0, [x22]
    dsb ishst
    isb
    blr x20
    str w0, [x21, #4]
    // Control: replace an ordinary L3 page and invalidate its own VA.
    ldr x20, =0x40200000
    blr x20
    str w0, [x21, #8]
    ldr x22, =0x800012000
    str xzr, [x22]
    dsb ishst
    ldr x0, =0x40200
    tlbi TLBI_OP, x0
    dsb ish
    isb
    ldr x0, =0x800601703
    str x0, [x22]
    dsb ishst
    isb
    blr x20
    str w0, [x21, #12]
    mov w0, #0xface
    str w0, [x21, #16]
1:  b 1b
.ltorg
.org 0x10000
    .quad 0
    .quad 0x800011003  // L1[1]: target block and control page
.org 0x10100
    .quad 0x800000701  // L1[32]: identity 1 GiB RAM
.org 0x11000
    .quad 0x800200701  // L2[0]: old 2 MiB block
    .quad 0x800012003  // L2[1]: L3 control table
.org 0x12000
    .quad 0x800600703
.org 0x210000
    mov w0, #0x11
    ret
.org 0x410000
    mov w0, #0x22
    ret
.org 0x600000
    mov w0, #0x33
    ret
.org 0x601000
    mov w0, #0x44
    ret
