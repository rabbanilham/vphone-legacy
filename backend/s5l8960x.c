/* SPDX-License-Identifier: GPL-2.0-or-later
 * Experimental A7 bring-up machine. No claim of Cyclone or iOS compatibility.
 * Register contracts: Linux apple/s5l8960x.dtsi, irq-apple-aic.c and
 * samsung_tty.c. See README.md for limitations and source references.
 */
#include "qemu/osdep.h"
#include "qemu/units.h"
#include "qemu/cutils.h"
#include "qemu/log.h"
#include "qemu/error-report.h"
#include "qemu/bswap.h"
#include "qemu/timer.h"
#include "qapi/error.h"
#include "qapi/visitor.h"
#include "hw/core/boards.h"
#include "hw/core/loader.h"
#include "hw/core/irq.h"
#include "hw/arm/boot.h"
#include "hw/arm/machines-qom.h"
#include "target/arm/cpu.h"
#include "target/arm/cpregs.h"
#include "target/arm/gtimer.h"
#include "target/arm/arm-powerctl.h"
#include "system/address-spaces.h"
#include "system/reset.h"
#include "system/system.h"
#include "system/tcg.h"
#include "chardev/char-fe.h"
#include "crypto/aes.h"
#include "ui/console.h"
#include <sys/file.h>

#define TYPE_A7 MACHINE_TYPE_NAME("s5l8960x")
OBJECT_DECLARE_SIMPLE_TYPE(A7State, A7)
#define RAM_BASE UINT64_C(0x800000000)
#define UART_BASE UINT64_C(0x20a0a0000)
#define AIC_BASE UINT64_C(0x20e100000)
#define PMGR_BASE UINT64_C(0x20e000000)
#define PMGR_SIZE UINT64_C(0x40000)
#define PMGR_EXT_BASE UINT64_C(0x202220000)
#define PMGR_EXT_SIZE UINT64_C(0x2000)
#define GPIO_BASE UINT64_C(0x20e300000)
#define GPIO_SIZE UINT64_C(0x100000)
#define NIRQ 1024
#define NUM_BRIDGES 15
#define NUM_PERIPHERALS 28

typedef struct A7Bridge {
    hwaddr base;
    uint32_t size;
    uint8_t *regs;
    MemoryRegion mr;
} A7Bridge;

typedef struct A7Peripheral {
    A7State *owner;
    const char *name;
    hwaddr base;
    uint32_t size;
    uint8_t *regs;
    MemoryRegion mr;
    bool i2c_active, i2c_read;
    uint8_t i2c_address;
    uint8_t chestnut_pointer, i2c_rx_byte;
    bool chestnut_prefix, chestnut_pointer_valid, i2c_rx_valid;
} A7Peripheral;

#define ANS_OUTBOX_CAPACITY 64
#define SIO_OUTBOX_CAPACITY 64

typedef struct AnsMailbox {
    uint32_t inbox_ctrl;
    uint32_t inbox_int_en;
    uint32_t inbox_low;
    uint32_t outbox_ctrl;
    uint64_t outbox_queue[ANS_OUTBOX_CAPACITY];
    uint32_t outbox_head;
    uint32_t outbox_tail;
    uint32_t outbox_count;
    uint64_t last_popped_msg;
    bool hello_queued;
    bool inbox_empty_pending;
    uint64_t asp_cmd_phys;
} AnsMailbox;

typedef struct SioMailbox {
    uint32_t inbox_ctrl;
    uint32_t inbox_int_en;
    uint32_t inbox_low;
    uint32_t outbox_ctrl;
    uint64_t outbox_queue[SIO_OUTBOX_CAPACITY];
    uint32_t outbox_head;
    uint32_t outbox_tail;
    uint32_t outbox_count;
    uint64_t last_popped_msg;
    bool os_active_queued;
    bool inbox_empty_pending;
    bool irq_armed;
    QEMUTimer *irq_timer;
    uint32_t params[256];
    uint64_t dma_seg_phys;
    uint32_t dma_seg_count;
    uint64_t aes_op_phys;
    uint32_t aes_op_count;
    uint64_t disabled_keys;
} SioMailbox;

#define SEP_OUTBOX_CAPACITY 64

typedef enum SepBootPhase {
    SEP_BOOT_ROM,
    SEP_BOOT_TZ0,
    SEP_BOOT_ART,
    SEP_BOOT_FINISHED,
} SepBootPhase;

typedef struct SepMailbox {
    uint32_t inbox_ctrl;
    uint32_t inbox_int_en;
    uint32_t inbox_low;
    uint32_t outbox_ctrl;
    uint64_t outbox_queue[SEP_OUTBOX_CAPACITY];
    uint32_t outbox_head;
    uint32_t outbox_tail;
    uint32_t outbox_count;
    uint64_t last_popped_msg;
    bool inbox_empty_pending;
    bool irq_armed;
    QEMUTimer *irq_timer;
    /* Explicit software model; no SEP firmware, ROM or keys execute here. */
    SepBootPhase boot_phase;
    uint64_t tz0_request;
    hwaddr art_phys;
    hwaddr firmware_phys;
    uint32_t finish_reply_slot;
    bool finish_reply_pending;
    bool os_active_pending;
    bool os_active_sent;
    QEMUTimer *os_active_timer;
    uint32_t params[256];
    uint32_t send_ool_page[256];
    uint32_t recv_ool_page[256];
    uint8_t keybag_blob[1024];
    uint32_t keybag_blob_len;
} SepMailbox;

struct A7State {
    MachineState parent_obj;
    ARMCPU *cpu;
    ARMCPU *iop_cpu;
    MemoryRegion iop_memory, iop_boot_ram, iop_dram, iop_tables, iop_unknown;
    MemoryRegion uart, aic, pmgr, pmgr_ext, gpio;
    A7Bridge bridges[NUM_BRIDGES];
    A7Peripheral peripherals[NUM_PERIPHERALS];
    CharFrontend chr;
    QemuConsole *con;
    uint64_t boot_entry, boot_args, framebuffer, boot_el, iop_diagnostic, system_writable, display_trace;
    bool iop_started;
    unsigned iop_unknown_count;
    uint64_t apple_c15[8][16][8]; /* Storage only: hardware effects are NOT emulated. */
    uint32_t uart_reg[16], mask[32], soft[32], device_irq[32], targets[NIRQ];
    uint32_t pmgr_reg[PMGR_SIZE / 4];
    uint32_t pmgr_ext_reg[PMGR_EXT_SIZE / 4];
    uint8_t *gpio_regs;
    uint8_t rx[256];
    unsigned rx_count;
    uint32_t ipi, ipi_mask;
    bool timer_level[2];
    QEMUTimer *vblank_timer;
    uint32_t disp_token;
    uint32_t disp_pending_token;
    uint32_t disp_fifo[512], disp_fifo_count, disp_fifo_expected, disp_fifo_token;
    bool disp_swap_pending;
    /* Native Chestnut ABI subset: register 0x05 power control. Virtual board
     * starts with rails disabled; this is not a claim about silicon reset ROM. */
    uint8_t chestnut_control;
    AnsMailbox ans;
    SioMailbox sio;
    SepMailbox sep;
    int sys_fd;
    int data_fd;
    int utility_fd;
    char *system_image, *data_image, *utility_image;
    uint64_t sys_lbas, data_lbas, data_start, total_lbas;
    int64_t ans_clock_ns;
    uint32_t ans_calendar_seconds, ans_elapsed_seconds;
    bool ans_timestamp_set;
    uint32_t utility_epoch;
    uint8_t utility_header[4096];
    uint8_t gpt_lba0[4096];
    uint8_t gpt_lba1[4096];
    uint8_t gpt_lba2[4096];
    uint8_t gpt_alt_hdr[4096];
};

/* Diagnostic only. Native ANS startup at Thumb 0x1176..0x11a6 builds
 * bootstrap page tables at private PA 0x04000000..0x04002000. Their physical
 * backing is unresolved: this scratch region is provisional, not Apple SRAM.
 * All IOP peripheral accesses fail rather than fabricate a ready message.
 */
static MemTxResult iop_unknown_read(void *opaque, hwaddr off, uint64_t *value,
                                   unsigned size, MemTxAttrs attrs)
{
    A7State *s = opaque;
    if (s->iop_unknown_count++ < 20) {
        qemu_log_mask(LOG_UNIMP, "ANS IOP unsupported read PA=0x%" HWADDR_PRIx
                      " size=%u PC=0x%x\n", off + UINT64_C(0x200000000), size,
                      s->iop_cpu->env.regs[15]);
    }
    *value = 0;
    return MEMTX_ERROR;
}

static MemTxResult iop_unknown_write(void *opaque, hwaddr off, uint64_t value,
                                    unsigned size, MemTxAttrs attrs)
{
    A7State *s = opaque;
    if (s->iop_unknown_count++ < 20) {
        qemu_log_mask(LOG_UNIMP, "ANS IOP unsupported write PA=0x%" HWADDR_PRIx
                      " size=%u value=0x%" PRIx64 " PC=0x%x\n",
                      off + UINT64_C(0x200000000), size, value, s->iop_cpu->env.regs[15]);
    }
    return MEMTX_ERROR;
}

static const MemoryRegionOps iop_unknown_ops = {
    .read_with_attrs = iop_unknown_read,
    .write_with_attrs = iop_unknown_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1, .valid.max_access_size = 8,
};

static void iop_try_start(A7Peripheral *p)
{
    A7State *s = p->owner;
    uint64_t address = ldl_le_p(p->regs + 8) |
                       ((uint64_t)ldl_le_p(p->regs + 12) << 32);
    uint8_t *image = memory_region_get_ram_ptr(MACHINE(s)->ram) + 0x3f800000;
    if (!s->iop_cpu || s->iop_started ||
        !(ldl_le_p(p->regs + 0x28) & 0x10)) {
        return;
    }
    /* Exact region allocation and loader structure recovered from iBoot.
     * The caller validates the original artifact hash before populating RAM;
     * these checks reject absent or structurally incompatible runtime images.
     */
    if (address != UINT64_C(0x83f800000) ||
        ldl_le_p(p->regs + 0x18) != 8 * MiB ||
        !(ldl_le_p(p->regs + 0x20) & 1) ||
        ldl_le_p(image) != 0xfa00040e ||
        ldl_le_p(image + 0x365a0) != 0x43736943 ||
        ldl_le_p(image + 0x365a4) != 2 ||
        ldl_le_p(image + 0x365a8) != 0x982f0) {
        qemu_log_mask(LOG_GUEST_ERROR, "ANS IOP diagnostic rejects missing/incompatible native runtime region\n");
        return;
    }
    int result = arm_set_cpu_on(1, 0, 0, 1, false);
    if (result == QEMU_ARM_POWERCTL_RET_SUCCESS) {
        s->iop_started = true;
        qemu_log_mask(LOG_UNIMP, "ANS IOP native ARM32 firmware executing on experimental Cortex-A7; no handshake emulation\n");
    } else {
        qemu_log_mask(LOG_GUEST_ERROR, "ANS IOP CPU start rejected: %d\n", result);
    }
}

static void aic_update(A7State *s)
{
    bool pending = !!(s->ipi & ~s->ipi_mask);
    for (int i = 0; i < NIRQ / 32; i++) {
        uint32_t level = s->soft[i] | s->device_irq[i];
        if (i == 140 / 32 && s->rx_count &&
            (s->uart_reg[1] & 3) == 1) {
            level |= 1U << (140 % 32);
        }
        pending |= !!(level & ~s->mask[i]);
    }
    qemu_set_irq(qdev_get_gpio_in(DEVICE(s->cpu), ARM_CPU_IRQ), pending);
}

static int uart_can_read(void *opaque)
{
    return 256 - ((A7State *)opaque)->rx_count;
}

static void uart_receive(void *opaque, const uint8_t *buf, int size)
{
    A7State *s = opaque;
    size = MIN(size, 256 - s->rx_count);
    memcpy(s->rx + s->rx_count, buf, size);
    s->rx_count += size;
    aic_update(s);
}

static uint64_t uart_read(void *opaque, hwaddr off, unsigned size)
{
    A7State *s = opaque;
    switch (off) {
    case 0x10: return 6 | !!s->rx_count; /* UTRSTAT TX empty + RX ready */
    case 0x14: return 0; /* UERSTAT */
    case 0x18: return MIN(s->rx_count, 15); /* UFSTAT RX count */
    case 0x24: {
        uint8_t v = s->rx_count ? s->rx[0] : 0;
        if (s->rx_count) {
            memmove(s->rx, s->rx + 1, --s->rx_count);
        }
        aic_update(s);
        return v;
    }
    default:
        if (off < sizeof(s->uart_reg)) {
            return s->uart_reg[off / 4];
        }
        qemu_log_mask(LOG_UNIMP, "A7 UART unimplemented read 0x%" HWADDR_PRIx "\n", off);
        return 0;
    }
}

static void uart_write(void *opaque, hwaddr off, uint64_t val, unsigned size)
{
    A7State *s = opaque;
    if (off == 0x20) {
        uint8_t c = val;
        qemu_chr_fe_write_all(&s->chr, &c, 1);
    } else if (off < sizeof(s->uart_reg)) {
        s->uart_reg[off / 4] = val;
        if (off == 8 && (val & 2)) {
            s->rx_count = 0;
        }
        aic_update(s);
    } else {
        qemu_log_mask(LOG_UNIMP, "A7 UART unimplemented write 0x%" HWADDR_PRIx "\n", off);
    }
}

static uint64_t aic_read(void *opaque, hwaddr off, unsigned size)
{
    A7State *s = opaque;
    if (off == 4) { return 192; }
    if (off == 0x2000) { return 0; }
    if (off == 0x2004) {
        for (int i = 0; i < NIRQ; i++) {
            bool level = ((s->soft[i / 32] | s->device_irq[i / 32]) >> (i % 32)) & 1;
            if (i == 140 && s->rx_count && (s->uart_reg[1] & 3) == 1) {
                level = true;
            }
            if (level && !(s->mask[i / 32] & (1U << (i % 32)))) {
                s->mask[i / 32] |= 1U << (i % 32);
                aic_update(s);
                return 0x10000 | i;
            }
        }
        if (s->ipi & ~s->ipi_mask) {
            unsigned num = (s->ipi & ~s->ipi_mask & 1) ? 1 : 2;
            s->ipi_mask |= num == 1 ? 1 : 0x80000000;
            aic_update(s);
            return 0x40000 | num;
        }
        return 0;
    }
    if (off >= 0x3000 && off < 0x4000) { return s->targets[(off - 0x3000) / 4]; }
    if (off >= 0x4100 && off < 0x4180) { return s->mask[(off - 0x4100) / 4]; }
    if (off >= 0x4200 && off < 0x4280) {
        unsigned word = (off - 0x4200) / 4;
        uint32_t level = s->soft[word] | s->device_irq[word];
        if (word == 140 / 32 && s->rx_count) { level |= 1U << (140 % 32); }
        return level;
    }
    if (off >= 0x5000 && off < 0x5100) {
        unsigned reg = off & 0x7f;
        if (reg == 0x00) { return (off - 0x5000) / 0x80; }
        if (reg == 0x24 || reg == 0x28) { return s->ipi_mask; }
    }
    qemu_log_mask(LOG_UNIMP, "A7 AIC unimplemented read 0x%" HWADDR_PRIx "\n", off);
    return 0;
}

static void aic_write(void *opaque, hwaddr off, uint64_t val, unsigned size)
{
    A7State *s = opaque;
    if (off >= 0x3000 && off < 0x4000) { s->targets[(off - 0x3000) / 4] = val; }
    else if (off >= 0x4000 && off < 0x4080) { s->soft[(off - 0x4000) / 4] |= val; }
    else if (off >= 0x4080 && off < 0x4100) { s->soft[(off - 0x4080) / 4] &= ~val; }
    else if (off >= 0x4100 && off < 0x4180) { s->mask[(off - 0x4100) / 4] |= val; }
    else if (off >= 0x4180 && off < 0x4200) { s->mask[(off - 0x4180) / 4] &= ~val; }
    else if (off == 0x2008 || off == 0x5008) { s->ipi |= val & 0x80000001; }
    else if (off == 0x200c || off == 0x500c) { s->ipi &= ~val; }
    else if (off == 0x2024 || off == 0x5024) { s->ipi_mask |= val; }
    else if (off == 0x2028 || off == 0x5028) { s->ipi_mask &= ~val; }
    else { qemu_log_mask(LOG_UNIMP, "A7 AIC unimplemented write 0x%" HWADDR_PRIx "\n", off); }
    aic_update(s);
}

static const MemoryRegionOps uart_ops = {
    .read = uart_read, .write = uart_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1, .valid.max_access_size = 4,
};
static const MemoryRegionOps aic_ops = {
    .read = aic_read, .write = aic_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 4, .valid.max_access_size = 4,
};

static uint64_t pmgr_read(void *opaque, hwaddr off, unsigned size)
{
    A7State *s = opaque;
    if (off + size <= PMGR_SIZE) {
        if (size == 4) {
            return s->pmgr_reg[off / 4];
        }
        uint64_t val = 0;
        for (unsigned i = 0; i < size; i++) {
            val |= (uint64_t)((uint8_t *)s->pmgr_reg)[off + i] << (i * 8);
        }
        return val;
    }
    qemu_log_mask(LOG_UNIMP, "A7 PMGR out-of-bounds read 0x%" HWADDR_PRIx "\n", off);
    return 0;
}

static void pmgr_write(void *opaque, hwaddr off, uint64_t val, unsigned size)
{
    A7State *s = opaque;
    if (off + size <= PMGR_SIZE) {
        if (off >= 0x20000 && off < 0x24000 && (off & 7) == 0) {
            /* In Apple S5L8960X PMGR, device power state registers are at 0x20000 + dev_id * 8.
             * Bits 0..3: PS_TARGET (requested power state)
             * Bits 4..7: PS_ACTUAL (current/actual power state)
             * In hardware, the state machine transitions PS_ACTUAL to match PS_TARGET.
             * In emulation, the transition is instantaneous.
             */
            val = (val & ~0xf0) | ((val & 0xf) << 4);
        }
        if (size == 4) {
            s->pmgr_reg[off / 4] = val;
            return;
        }
        for (unsigned i = 0; i < size; i++) {
            ((uint8_t *)s->pmgr_reg)[off + i] = (val >> (i * 8)) & 0xff;
        }
        return;
    }
    qemu_log_mask(LOG_UNIMP, "A7 PMGR out-of-bounds write 0x%" HWADDR_PRIx " = 0x%" PRIx64 "\n", off, val);
}

static const MemoryRegionOps pmgr_ops = {
    .read = pmgr_read, .write = pmgr_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1, .valid.max_access_size = 8,
};

static uint64_t pmgr_ext_read(void *opaque, hwaddr off, unsigned size)
{
    A7State *s = opaque;
    if (off + size <= PMGR_EXT_SIZE) {
        if (size == 4) {
            return s->pmgr_ext_reg[off / 4];
        }
        uint64_t val = 0;
        for (unsigned i = 0; i < size; i++) {
            val |= (uint64_t)((uint8_t *)s->pmgr_ext_reg)[off + i] << (i * 8);
        }
        return val;
    }
    return 0;
}

static void pmgr_ext_write(void *opaque, hwaddr off, uint64_t val, unsigned size)
{
    A7State *s = opaque;
    if (off + size <= PMGR_EXT_SIZE) {
        if (size == 4) {
            s->pmgr_ext_reg[off / 4] = val;
            return;
        }
        for (unsigned i = 0; i < size; i++) {
            ((uint8_t *)s->pmgr_ext_reg)[off + i] = (val >> (i * 8)) & 0xff;
        }
    }
}

static const MemoryRegionOps pmgr_ext_ops = {
    .read = pmgr_ext_read, .write = pmgr_ext_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1, .valid.max_access_size = 8,
};

static uint64_t gpio_read(void *opaque, hwaddr off, unsigned size)
{
    A7State *s = opaque;
    if (off + size <= GPIO_SIZE) {
        uint64_t val = 0;
        for (unsigned i = 0; i < size; i++) {
            uint8_t byte = s->gpio_regs[off + i];
            if (((off + i) & 3) == 0) {
                unsigned pin = (off + i) / 4;
                /* Board-model pull-ups:
                 * 1) Native I2C lines: pins 80-83 (0x140..0x14c).
                 *    See research/native-i2c-gpio-contract.md.
                 * 2) Physical push buttons: pins 2 (menu), 3 (hold), 4 (volup),
                 *    5 (voldown), 16 (ringerab).
                 *    On hardware, buttons are active-low (pulled high to VDD when
                 *    idle/unpressed, grounded to 0 when pressed).
                 *    AppleM68Buttons reads raw bit 0 via GPIOFunction::read and
                 *    inverts: pressed = (sample ^ inversion ^ 1). When unmodeled/0,
                 *    all buttons appear held down, triggering IOHIDEventDriver's
                 *    hold+volume sysdiagnose watchdog and NMI panic after 2.5s.
                 */
                if ((pin >= 80 && pin <= 83) ||
                    pin == 2 || pin == 3 || pin == 4 || pin == 5 || pin == 16) {
                    uint32_t config = ldl_le_p(s->gpio_regs + off + i);
                    bool driven = !(config & 0x200) && (config & 0xe) == 2;
                    bool sample = !driven || (config & 1);
                    byte = (byte & ~1) | sample;
                }
            }
            val |= (uint64_t)byte << (i * 8);
        }
        return val;
    }
    qemu_log_mask(LOG_UNIMP, "A7 GPIO out-of-bounds read 0x%" HWADDR_PRIx "\n", off);
    return 0;
}

static void gpio_write(void *opaque, hwaddr off, uint64_t val, unsigned size)
{
    A7State *s = opaque;
    if (off + size <= GPIO_SIZE) {
        for (unsigned i = 0; i < size; i++) {
            s->gpio_regs[off + i] = (val >> (i * 8)) & 0xff;
        }
        for (unsigned pin = 80; pin <= 83; pin++) {
            hwaddr reg = pin * 4;
            if (off < reg + 4 && off + size > reg) {
                uint32_t config = ldl_le_p(s->gpio_regs + reg);
                qemu_log_mask(LOG_UNIMP,
                              "A7 I2C GPIO pin=%u config=0x%x latch=%u sample=%u (pull-up board model)\n",
                              pin, config, config & 1,
                              (unsigned)(gpio_read(s, reg, 4) & 1));
            }
        }
        return;
    }
    qemu_log_mask(LOG_UNIMP, "A7 GPIO out-of-bounds write 0x%" HWADDR_PRIx " = 0x%" PRIx64 "\n", off, val);
}

static const MemoryRegionOps gpio_ops = {
    .read = gpio_read, .write = gpio_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1, .valid.max_access_size = 8,
};

static uint64_t bridge_read(void *opaque, hwaddr off, unsigned size)
{
    A7Bridge *b = opaque;
    if (off + size <= b->size) {
        if (size == 4) {
            return *(uint32_t *)(b->regs + off);
        }
        uint64_t val = 0;
        for (unsigned i = 0; i < size; i++) {
            val |= (uint64_t)b->regs[off + i] << (i * 8);
        }
        return val;
    }
    return 0;
}

static void bridge_write(void *opaque, hwaddr off, uint64_t val, unsigned size)
{
    A7Bridge *b = opaque;
    if (off + size <= b->size) {
        if (b->base == UINT64_C(0x200000000) && off == 0x828) {
            /* Apple S5L8960X MemCacheController (MCC) cache line flush register at 0x200000828.
             * Software writes (0xc0000000 | (addr >> 6)) to initiate a 64-byte
             * cache line flush and polls bit 31 until cleared to indicate completion.
             * In emulation, cache coherency is immediate so bit 31 is cleared upon write.
             */
            *(uint32_t *)(b->regs + 0x828) = (uint32_t)(val & ~0x80000000);
            return;
        }
        if (b->base == UINT64_C(0x200000000) && off == 0x830) {
            /* Apple S5L8960X MemCacheController (MCC) config register at 0x200000830.
             * Software writes target configuration ID into bits 0..4 (or 0x100 to quiesce/clear)
             * and polls status register at 0x834 for bits 5..9 and 21..25 to match.
             * In emulation, the transition is instantaneous.
             */
            uint32_t target = (val & 0x100) ? 0 : (val & 0x1f);
            uint32_t *status = (uint32_t *)(b->regs + 0x834);
            *status = (*status & ~((0x1f) | (0x1f << 5) | (0x1f << 21))) |
                      target | (target << 5) | (target << 21);
        }
        if (size == 4) {
            *(uint32_t *)(b->regs + off) = val;
            return;
        }
        for (unsigned i = 0; i < size; i++) {
            b->regs[off + i] = (val >> (i * 8)) & 0xff;
        }
    }
}

static const MemoryRegionOps bridge_ops = {
    .read = bridge_read, .write = bridge_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1, .valid.max_access_size = 8,
};

static int i2c_index(A7Peripheral *p)
{
    if (p->base == UINT64_C(0x20a110000)) { return 0; }
    if (p->base == UINT64_C(0x20a111000)) { return 1; }
    return -1;
}

static void i2c_update(A7Peripheral *p)
{
    unsigned irq = 154 + i2c_index(p);
    uint32_t bit = 1U << (irq % 32);
    if (ldl_le_p(p->regs + 0x14) & ldl_le_p(p->regs + 0x18)) {
        p->owner->device_irq[irq / 32] |= bit;
    } else {
        p->owner->device_irq[irq / 32] &= ~bit;
    }
    aic_update(p->owner);
}

static bool i2c_is_chestnut(A7Peripheral *p)
{
    return i2c_index(p) == 0 && p->i2c_address == 0x27;
}

static void i2c_nack(A7Peripheral *p)
{
    p->i2c_active = false;
    p->i2c_rx_valid = false;
    stl_le_p(p->regs + 0x14, ldl_le_p(p->regs + 0x14) | 0x08200000);
}

/* Restrict successful transfers to the recovered one-byte native register ABI.
 * Unknown registers/lengths are errors, not zero-filled successful reads. */
static void chestnut_command(A7Peripheral *p, uint32_t value)
{
    if (value & 0x100) {
        p->i2c_rx_valid = false;
        p->chestnut_prefix = !p->i2c_read;
        qemu_log_mask(LOG_UNIMP, "CHESTNUT START direction=%s control=0x%02x\n",
                      p->i2c_read ? "read" : "write", p->owner->chestnut_control);
    } else if (!p->i2c_active) {
        return;
    } else if (value & 0x400) {
        if (!p->i2c_read || !p->chestnut_pointer_valid ||
            p->chestnut_pointer != 5 || (value & 0xff) != 1 || p->i2c_rx_valid) {
            i2c_nack(p);
            return;
        }
        p->i2c_rx_byte = p->owner->chestnut_control;
        p->i2c_rx_valid = true;
        /* Native read completion is bit 20, not error bit 23. */
        stl_le_p(p->regs + 0x14, ldl_le_p(p->regs + 0x14) | (1U << 20));
        qemu_log_mask(LOG_UNIMP, "CHESTNUT READ reg=0x05 value=0x%02x\n", p->i2c_rx_byte);
    } else if (!p->i2c_read) {
        if (p->chestnut_prefix) {
            p->chestnut_pointer = value & 0xff;
            p->chestnut_pointer_valid = p->chestnut_pointer == 5;
            p->chestnut_prefix = false;
            if (!p->chestnut_pointer_valid) {
                i2c_nack(p);
                return;
            }
        } else if (p->chestnut_pointer_valid && p->chestnut_pointer == 5) {
            if (value & 0xf0) {
                i2c_nack(p);
                return;
            }
            p->owner->chestnut_control = value & 0x0f;
            p->chestnut_pointer_valid = false; /* No invented auto-increment registers. */
            qemu_log_mask(LOG_UNIMP, "CHESTNUT WRITE reg=0x05 value=0x%02x\n",
                          p->owner->chestnut_control);
        } else {
            i2c_nack(p);
            return;
        }
    } else {
        i2c_nack(p);
        return;
    }
    if (value & 0x200) {
        p->i2c_active = false;
        if (!p->i2c_read) {
            stl_le_p(p->regs + 0x14, ldl_le_p(p->regs + 0x14) | (1U << 27));
        }
    }
}

static void i2c_write(A7Peripheral *p, hwaddr off, uint32_t value)
{
    switch (off) {
    case 0: /* Native FIFO command: address/START, data/STOP, read request. */
        if (value & 0x100) {
            p->i2c_address = (value & 0xff) >> 1;
            p->i2c_read = value & 1;
            p->i2c_active = true;
            if (!i2c_is_chestnut(p)) {
                /* Other slave addresses retain the native NACK boundary. */
                i2c_nack(p);
                qemu_log_mask(LOG_UNIMP,
                              "A7 I2C%d address=0x%02x direction=%s NACK (unmodeled slave)\n",
                              i2c_index(p), p->i2c_address, p->i2c_read ? "read" : "write");
            }
        }
        if (i2c_is_chestnut(p)) {
            chestnut_command(p, value);
        } else if (value & 0x200) {
            p->i2c_active = false;
        }
        break;
    case 0x10:
        if (value & 0x80000000) {
            p->i2c_active = false;
            p->i2c_rx_valid = false;
            p->chestnut_prefix = false;
            stl_le_p(p->regs + 0x14, 0);
            stl_le_p(p->regs + 0x18, 0);
            value &= ~0x80000000U;
        }
        stl_le_p(p->regs + off, value);
        break;
    case 0x14: /* Native event acknowledgement is write-one-to-clear. */
        stl_le_p(p->regs + off, ldl_le_p(p->regs + off) & ~value);
        break;
    default:
        stl_le_p(p->regs + off, value);
    }
    i2c_update(p);
}

static void ans_update_irq(A7State *s)
{
    /* IRQ 55: Outbox non-empty interrupt (DeviceTree interrupts index 2) */
    bool outbox_irq_en = (s->ans.outbox_ctrl & 1) != 0;
    bool outbox_has_data = (s->ans.outbox_count > 0);
    if (outbox_irq_en && outbox_has_data) {
        s->device_irq[55 / 32] |= (1U << (55 % 32));
    } else {
        s->device_irq[55 / 32] &= ~(1U << (55 % 32));
    }

    /* IRQ 52: Inbox empty interrupt (DeviceTree interrupts index 1)
     * Level IRQ must not be permanently asserted when enabled, otherwise
     * an infinite FIQ interrupt storm starves all workloops.
     */
    bool inbox_irq_en = ((s->ans.inbox_int_en & 1) || (s->ans.inbox_int_en & 0x1000));
    if (inbox_irq_en && s->ans.inbox_empty_pending) {
        s->device_irq[52 / 32] |= (1U << (52 % 32));
    } else {
        s->device_irq[52 / 32] &= ~(1U << (52 % 32));
    }

    aic_update(s);
}

static void ans_outbox_push(A7State *s, uint64_t msg)
{
    if (s->ans.outbox_count < ANS_OUTBOX_CAPACITY) {
        s->ans.outbox_queue[s->ans.outbox_tail] = msg;
        s->ans.outbox_tail = (s->ans.outbox_tail + 1) % ANS_OUTBOX_CAPACITY;
        s->ans.outbox_count++;
        qemu_log_mask(LOG_UNIMP, "ANS outbox pushed 0x%" PRIx64 " (count=%u)\n",
                      msg, s->ans.outbox_count);
        ans_update_irq(s);
    } else {
        qemu_log_mask(LOG_GUEST_ERROR, "ANS outbox queue overflow, dropping 0x%" PRIx64 "\n", msg);
    }
}

static uint64_t ans_outbox_pop(A7State *s)
{
    if (s->ans.outbox_count == 0) {
        return 0;
    }
    uint64_t msg = s->ans.outbox_queue[s->ans.outbox_head];
    s->ans.outbox_head = (s->ans.outbox_head + 1) % ANS_OUTBOX_CAPACITY;
    s->ans.outbox_count--;
    s->ans.last_popped_msg = msg;
    qemu_log_mask(LOG_UNIMP, "ANS outbox popped 0x%" PRIx64 " (remaining=%u)\n",
                      msg, s->ans.outbox_count);
    ans_update_irq(s);
    return msg;
}

static uint32_t ans_crc32(const void *data, size_t len)
{
    const uint8_t *p = data;
    uint32_t crc = 0xffffffffU;
    for (size_t i = 0; i < len; i++) {
        crc ^= p[i];
        for (int b = 0; b < 8; b++) {
            crc = (crc >> 1) ^ (0xedb88320U & -(crc & 1U));
        }
    }
    return ~crc;
}

static void ans_put_utf16le(uint8_t *dst, const char *ascii)
{
    for (size_t i = 0; ascii[i] != '\0'; i++) {
        dst[i * 2] = (uint8_t)ascii[i];
        dst[i * 2 + 1] = 0;
    }
}

#define ANS_SYS_START UINT64_C(8)
#define ANS_SECTOR_SIZE 4096

static int ans_open_image(const char *path, bool writable, uint64_t *lbas)
{
    struct stat st;
    int fd = open(path, (writable ? O_RDWR : O_RDONLY) | O_CLOEXEC);
    if (fd < 0 || fstat(fd, &st) < 0 || !S_ISREG(st.st_mode) ||
        st.st_size <= 0 || st.st_size % ANS_SECTOR_SIZE != 0) {
        error_report("ANS image must be an accessible nonempty sector-aligned regular file: %s", path);
        if (fd >= 0) { close(fd); }
        exit(EXIT_FAILURE);
    }
    if (flock(fd, (writable ? LOCK_EX : LOCK_SH) | LOCK_NB) < 0) {
        error_report("ANS image is already in use: %s", path);
        close(fd);
        exit(EXIT_FAILURE);
    }
    *lbas = st.st_size / ANS_SECTOR_SIZE;
    return fd;
}

static void ans_init_storage(A7State *s)
{
    /* Explicit image ownership; never discover unrelated ancestor files. */
    if (!!s->system_image != !!s->data_image) {
        error_report("ANS requires both system-image and data-image");
        exit(EXIT_FAILURE);
    }
    if (!s->system_image) { return; }
    if (s->iop_diagnostic) {
        warn_report("ANS diagnostic mode ignores host storage; modeled transport disabled");
        return;
    }
    s->sys_fd = ans_open_image(s->system_image, s->system_writable != 0, &s->sys_lbas);
    s->data_fd = ans_open_image(s->data_image, true, &s->data_lbas);
    struct stat system_st, data_st;
    if (fstat(s->sys_fd, &system_st) < 0 || fstat(s->data_fd, &data_st) < 0 ||
        (system_st.st_dev == data_st.st_dev && system_st.st_ino == data_st.st_ino)) {
        error_report("ANS system and data images must be different files");
        exit(EXIT_FAILURE);
    }
    s->data_start = ANS_SYS_START + s->sys_lbas;
    s->total_lbas = s->data_start + s->data_lbas + 8;
    if (s->total_lbas > UINT32_MAX) {
        error_report("ANS modeled geometry exceeds 32-bit native LBA contract");
        exit(EXIT_FAILURE);
    }
    qemu_log_mask(LOG_UNIMP, "ANS modeled storage: system=%s sectors=%" PRIu64
                  " data=%s sectors=%" PRIu64 "; native IOP execution disabled\n",
                  s->system_image, s->sys_lbas, s->data_image, s->data_lbas);

    /* LBA 0: Protective MBR (4096-byte sector) */
    memset(s->gpt_lba0, 0, sizeof(s->gpt_lba0));
    s->gpt_lba0[0x1be + 1] = 0x00;
    s->gpt_lba0[0x1be + 2] = 0x02;
    s->gpt_lba0[0x1be + 3] = 0x00;
    s->gpt_lba0[0x1be + 4] = 0xee; /* GPT Protective */
    s->gpt_lba0[0x1be + 5] = 0xff;
    s->gpt_lba0[0x1be + 6] = 0xff;
    s->gpt_lba0[0x1be + 7] = 0xff;
    stl_le_p(s->gpt_lba0 + 0x1be + 8, 1U);
    stl_le_p(s->gpt_lba0 + 0x1be + 12, (uint32_t)(s->total_lbas - 1));
    stw_le_p(s->gpt_lba0 + 0x1fe, 0xaa55U);

    /* LBA 2: GPT Partition Entry Array (32 entries * 128 bytes = 4096 bytes) */
    memset(s->gpt_lba2, 0, sizeof(s->gpt_lba2));
    /* Apple HFS+ type GUID: 48465300-0000-11AA-AA11-00306543ECAC (mixed-endian on disk) */
    static const uint8_t hfs_guid[16] = {
        0x00, 0x53, 0x46, 0x48, 0x00, 0x00, 0xaa, 0x11,
        0xaa, 0x11, 0x00, 0x30, 0x65, 0x43, 0xec, 0xac
    };
    static const uint8_t sys_uuid[16] = {
        0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
        0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e, 0x0f, 0x01
    };
    static const uint8_t data_uuid[16] = {
        0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
        0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e, 0x0f, 0x02
    };
    /* Entry 0: disk0s1 (System) */
    uint8_t *e0 = s->gpt_lba2;
    memcpy(e0 + 0x00, hfs_guid, 16);
    memcpy(e0 + 0x10, sys_uuid, 16);
    stq_le_p(e0 + 0x20, ANS_SYS_START);
    stq_le_p(e0 + 0x28, ANS_SYS_START + s->sys_lbas - 1);
    stq_le_p(e0 + 0x30, 0ULL);
    ans_put_utf16le(e0 + 0x38, "System");

    /* Entry 1: disk0s2 (Data) */
    uint8_t *e1 = s->gpt_lba2 + 128;
    memcpy(e1 + 0x00, hfs_guid, 16);
    memcpy(e1 + 0x10, data_uuid, 16);
    stq_le_p(e1 + 0x20, s->data_start);
    stq_le_p(e1 + 0x28, s->data_start + s->data_lbas - 1);
    stq_le_p(e1 + 0x30, 0ULL);
    ans_put_utf16le(e1 + 0x38, "Data");

    uint32_t entries_crc = ans_crc32(s->gpt_lba2, sizeof(s->gpt_lba2));

    /* LBA 1: Primary GPT Header */
    memset(s->gpt_lba1, 0, sizeof(s->gpt_lba1));
    static const uint8_t disk_uuid[16] = {
        0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88,
        0x99, 0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff, 0x00
    };
    memcpy(s->gpt_lba1 + 0x00, "EFI PART", 8);
    stl_le_p(s->gpt_lba1 + 0x08, 0x00010000U);
    stl_le_p(s->gpt_lba1 + 0x0c, 0x5cU);
    stl_le_p(s->gpt_lba1 + 0x10, 0U);
    stl_le_p(s->gpt_lba1 + 0x14, 0U);
    stq_le_p(s->gpt_lba1 + 0x18, 1ULL);
    stq_le_p(s->gpt_lba1 + 0x20, s->total_lbas - 1);
    stq_le_p(s->gpt_lba1 + 0x28, ANS_SYS_START);
    stq_le_p(s->gpt_lba1 + 0x30, s->total_lbas - 9);
    memcpy(s->gpt_lba1 + 0x38, disk_uuid, 16);
    stq_le_p(s->gpt_lba1 + 0x48, 2ULL);
    stl_le_p(s->gpt_lba1 + 0x50, 32U);
    stl_le_p(s->gpt_lba1 + 0x54, 128U);
    stl_le_p(s->gpt_lba1 + 0x58, entries_crc);
    stl_le_p(s->gpt_lba1 + 0x10, ans_crc32(s->gpt_lba1, 0x5c));

    /* Alternate GPT Header at s->total_lbas - 1 */
    memcpy(s->gpt_alt_hdr, s->gpt_lba1, sizeof(s->gpt_alt_hdr));
    stl_le_p(s->gpt_alt_hdr + 0x10, 0U);
    stq_le_p(s->gpt_alt_hdr + 0x18, s->total_lbas - 1);
    stq_le_p(s->gpt_alt_hdr + 0x20, 1ULL);
    stq_le_p(s->gpt_alt_hdr + 0x48, s->total_lbas - 2);
    stl_le_p(s->gpt_alt_hdr + 0x10, ans_crc32(s->gpt_alt_hdr, 0x5c));
}

/* Native ASP completion codes: 1/2 bad argument, 3 bad media, 4 timeout,
 * >4 generic error. HandleASPEvent 10c6248 -> 10c80fc/table 10d6fc0.
 * This is an explicit synchronous software model, not native IOP execution.
 */
static bool ans_ram_range(hwaddr addr, uint64_t length)
{
    return addr >= RAM_BASE && addr < RAM_BASE + GiB &&
           length <= RAM_BASE + GiB - addr;
}

static bool ans_read_ram(hwaddr addr, void *buf, size_t length)
{
    return ans_ram_range(addr, length) &&
           address_space_read(&address_space_memory, addr, MEMTXATTRS_UNSPECIFIED,
                              buf, length) == MEMTX_OK;
}

static bool ans_write_ram(hwaddr addr, const void *buf, size_t length)
{
    return ans_ram_range(addr, length) &&
           address_space_write(&address_space_memory, addr, MEMTXATTRS_UNSPECIFIED,
                               buf, length) == MEMTX_OK;
}

static bool ans_file_io(int fd, uint8_t *buf, off_t offset, bool write)
{
    size_t done = 0;
    while (done < ANS_SECTOR_SIZE) {
        ssize_t n = write ? pwrite(fd, buf + done, ANS_SECTOR_SIZE - done, offset + done)
                          : pread(fd, buf + done, ANS_SECTOR_SIZE - done, offset + done);
        if (n < 0 && errno == EINTR) { continue; }
        if (n <= 0) { return false; }
        done += n;
    }
    return true;
}

/* Model-private logical utility container. NOT native NAND metadata.
 * Native page capacities/opcodes: storage-native-disassembly.txt / profilev1.
 */
#define ANS_UTILITY_BYTES UINT64_C(9580544)
typedef struct AnsUtilityRegion {
    uint32_t read_opcode, write_opcode;
    uint64_t first_page, page_count;
} AnsUtilityRegion;
static const AnsUtilityRegion utility_regions[] = {
    { 0x30, 0x40, 1, 253 },
    { 0x31, 0x41, 254, 2048 },
    { 0x34, 0x44, 2302, 2 },
    { 0x35, 0x45, 2304, 1 },
    { 0x36, 0x46, 2305, 2 },
    { 0x37, 0x47, 2307, 32 },
};
static const uint8_t utility_magic[16] = "VPHONE-UTILITY1";
static const uint8_t utility_profile[32] = "n53ap-11D257-ASP10-utility-v1";
static const uint8_t utility_source_sha[32] = { 0xc0, 0x66, 0xbf, 0x9d, 0x1e, 0xde, 0xf9, 0x6e, 0x59, 0xfe, 0x61, 0x8e, 0xb3, 0x09, 0x33, 0x84, 0x7b, 0xa0, 0x0a, 0xc8, 0x2b, 0xff, 0x2b, 0x8a, 0x7b, 0x3e, 0xf0, 0xb7, 0xa3, 0x2c, 0x9b, 0x7e };

static bool ans_utility_header_valid(A7State *s, const uint8_t *h)
{
    if (memcmp(h, utility_magic, 16) || ldl_le_p(h + 16) != 1 ||
        ldl_le_p(h + 20) != ANS_SECTOR_SIZE || ldl_le_p(h + 24) != 6 ||
        ldl_le_p(h + 28) != 0 || ldq_le_p(h + 32) > UINT32_MAX ||
        ldq_le_p(h + 40) != s->sys_lbas || ldq_le_p(h + 48) != s->data_lbas ||
        memcmp(h + 56, utility_source_sha, 32) ||
        memcmp(h + 88, utility_profile, 32) ||
        ans_crc32(h, 4092) != ldl_le_p(h + 4092)) { return false; }
    for (unsigned i = 120; i < 128; i++) { if (h[i]) { return false; } }
    for (unsigned i = 0; i < ARRAY_SIZE(utility_regions); i++) {
        const AnsUtilityRegion *r = &utility_regions[i];
        const uint8_t *entry = h + 128 + i * 32;
        if (ldl_le_p(entry) != r->read_opcode || ldl_le_p(entry + 4) != r->write_opcode ||
            ldq_le_p(entry + 8) != r->first_page || ldq_le_p(entry + 16) != r->page_count ||
            ldq_le_p(entry + 24) != 0) { return false; }
    }
    for (unsigned i = 320; i < 4092; i++) { if (h[i]) { return false; } }
    return true;
}

static bool ans_utility_ready(A7State *s)
{
    struct stat st;
    uint8_t header[ANS_SECTOR_SIZE];
    return s->utility_fd >= 0 && fstat(s->utility_fd, &st) == 0 &&
           (uint64_t)st.st_size == ANS_UTILITY_BYTES &&
           ans_file_io(s->utility_fd, header, 0, false) &&
           memcmp(header, s->utility_header, sizeof(header)) == 0;
}

static void ans_init_utility(A7State *s)
{
    if (!s->utility_image || s->iop_diagnostic) { return; }
    if (s->sys_fd < 0 || s->data_fd < 0) {
        error_report("utility-image requires the modeled System/Data backings");
        exit(EXIT_FAILURE);
    }
    uint64_t sectors;
    s->utility_fd = ans_open_image(s->utility_image, true, &sectors);
    if (sectors * ANS_SECTOR_SIZE != ANS_UTILITY_BYTES ||
        !ans_file_io(s->utility_fd, s->utility_header, 0, false) ||
        !ans_utility_header_valid(s, s->utility_header)) {
        error_report("ANS logical utility container profile/header/CRC/geometry/length is invalid; existing bytes preserved");
        exit(EXIT_FAILURE);
    }
    s->utility_epoch = ldq_le_p(s->utility_header + 32);
    qemu_log_mask(LOG_UNIMP, "ANS logical utility namespace attached: %s, epoch=%u; erased allocation profile, not native NAND format\n",
                  s->utility_image, s->utility_epoch);
}

static const AnsUtilityRegion *ans_utility_region(uint8_t opcode)
{
    for (unsigned i = 0; i < ARRAY_SIZE(utility_regions); i++) {
        if (opcode == utility_regions[i].read_opcode || opcode == utility_regions[i].write_opcode) {
            return &utility_regions[i];
        }
    }
    return NULL;
}

static bool ans_flush(A7State *s)
{
    bool ok = fsync(s->data_fd) == 0;
    if (s->utility_fd >= 0 && fsync(s->utility_fd) != 0) { ok = false; }
    if (s->system_writable && fsync(s->sys_fd) != 0) { ok = false; }
    return ok;
}

static bool ans_backings_ready(A7State *s)
{
    int fds[2] = { s->sys_fd, s->data_fd };
    uint64_t sectors[2] = { s->sys_lbas, s->data_lbas };
    uint8_t block[ANS_SECTOR_SIZE];
    for (unsigned i = 0; i < 2; i++) {
        struct stat st;
        if (fds[i] < 0 || fstat(fds[i], &st) < 0 || !S_ISREG(st.st_mode) ||
            (uint64_t)st.st_size != sectors[i] * ANS_SECTOR_SIZE ||
            !ans_file_io(fds[i], block, 0, false) ||
            !ans_file_io(fds[i], block, st.st_size - ANS_SECTOR_SIZE, false)) {
            return false;
        }
    }
    return true;
}

static uint32_t ans_execute(A7State *s, uint8_t tag)
{
    uint32_t status = 0, limit, size;
    hwaddr cmd_addr;
    uint8_t desc[0x30], buf[ANS_SECTOR_SIZE];
    if (tag >= 32 || !s->ans.asp_cmd_phys) { return 1; }
    if (tag < 20) {
        cmd_addr = s->ans.asp_cmd_phys + tag * 0x180;
        limit = 16; size = 0x180;
    } else if (tag < 31) {
        cmd_addr = s->ans.asp_cmd_phys + 0x1e00 + (tag - 20) * 0x1440;
        limit = 256; size = 0x1440;
    } else {
        cmd_addr = s->ans.asp_cmd_phys + 0xfcc0;
        limit = 2048; size = 0x2080;
    }
    if (!ans_ram_range(cmd_addr, size) || !ans_read_ram(cmd_addr, desc, sizeof(desc))) {
        return 1;
    }
    uint8_t opcode = desc[0];
    qemu_log_mask(LOG_UNIMP, "ANS modeled exec tag=%u opcode=0x%02x descriptor=0x%" HWADDR_PRIx "\n",
                  tag, opcode, cmd_addr);
    if (opcode == 0) {
        uint8_t geometry[24];
        stl_le_p(geometry, s->total_lbas);
        stl_le_p(geometry + 4, ANS_SECTOR_SIZE);
        /* Native +38 is preferred LBA count, not bytes: 256 * 4096 = 1 MiB. */
        stl_le_p(geometry + 8, 256);
        stl_le_p(geometry + 12, 1);
        stl_le_p(geometry + 16, ans_utility_ready(s) ? 1 : 0);
        stl_le_p(geometry + 20, 0);
        return ans_write_ram(cmd_addr + 0x30, geometry, sizeof(geometry)) ? 0 : 5;
    }
    if (opcode == 0x13) { return ans_flush(s) ? 0 : 5; }
    if (opcode == 0x80) {
        /* Native GetASPTime5 / SetTimestamp6; ARM32 handler e014..e05e.
         * Fields44/48/4c are utility format epoch, calendar/elapsed seconds.
         * Epoch0 describes absent utility; otherwise retain the committed
         * logical namespace provisioning epoch (not a factory NAND date).
         * Clocks advance with guest virtual time, never host pause.
         */
        uint8_t request[24], result[12];
        if (!ans_read_ram(cmd_addr + 0x38, request, sizeof(request))) { return 1; }
        uint32_t subcommand = ldl_le_p(request);
        int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        uint32_t seconds = (now - s->ans_clock_ns) / NANOSECONDS_PER_SECOND;
        if (subcommand == 6) {
            uint32_t old_calendar = s->ans_calendar_seconds + seconds;
            uint32_t elapsed = s->ans_elapsed_seconds + seconds;
            uint32_t calendar = ldl_le_p(request + 16);
            uint32_t supplied_elapsed = ldl_le_p(request + 20);
            /* Native first-set derives elapsed from calendar delta (e712/edac).
             * Later sets reject backwards elapsed, still accepting calendar
             * (e052/e744/edbc). Backwards-update telemetry is unimplemented.
             */
            if (!s->ans_timestamp_set) {
                if (calendar >= old_calendar) { elapsed += calendar - old_calendar; }
            } else if (supplied_elapsed >= elapsed) {
                elapsed = supplied_elapsed;
            }
            s->ans_calendar_seconds = calendar;
            s->ans_elapsed_seconds = elapsed;
            s->ans_clock_ns = now;
            s->ans_timestamp_set = true;
            return 0;
        }
        if (subcommand != 5) { return 5; }
        if (s->utility_image && !ans_utility_ready(s)) { return 5; }
        stl_le_p(result, s->utility_epoch);
        stl_le_p(result + 4, s->ans_calendar_seconds + seconds);
        stl_le_p(result + 8, s->ans_elapsed_seconds + seconds);
        return ans_write_ram(cmd_addr + 0x44, result, sizeof(result)) ? 0 : 5;
    }
    if (opcode == 0x81) {
        /* GetInitStatus: native 10c6740, subcommand16, one uint32 DMA result. */
        uint8_t request[12], result[4];
        if (!ans_read_ram(cmd_addr + 0x30, request, sizeof(request))) { return 1; }
        if (ldl_le_p(request + 8) != 16) { return 5; }
        hwaddr page = (hwaddr)ldl_le_p(request) << 12;
        if (ldl_le_p(request + 4) != 4 || !ans_ram_range(page, sizeof(result))) { return 1; }
        bool ready = ans_backings_ready(s) && (!s->utility_image || ans_utility_ready(s));
        /* Native 0=init OK, 5=no NAND. Never claim unavailable utility format. */
        stl_le_p(result, ready ? 0 : 5);
        if (!ans_write_ram(page, result, sizeof(result))) { return 5; }
        return ready ? 0 : 5;
    }
    const AnsUtilityRegion *region = ans_utility_region(opcode);
    if (opcode != 0x10 && opcode != 0x11 && !region) {
        qemu_log_mask(LOG_UNIMP, "ANS unsupported opcode=0x%02x returns error, no completion success\n", opcode);
        return 5;
    }
    uint32_t lba = ldl_le_p(desc + 4), count = ldl_le_p(desc + 8);
    bool write = region ? opcode == region->write_opcode : opcode == 0x11;
    uint64_t available = region ? region->page_count : s->total_lbas;
    if (region && !ans_utility_ready(s)) { return 5; }
    if (!count || count > limit || (count > 256 && !(desc[2] & 8)) ||
        lba >= available || count > available - lba) {
        return 1;
    }
    hwaddr pages[2048];
    /* Validate the entire request before any disk/DMA side effects. Never DMA MMIO. */
    for (uint32_t i = 0; i < count; i++) {
        uint8_t ppn[4];
        if (!ans_read_ram(cmd_addr + 0x30 + i * 4, ppn, 4)) { return 1; }
        pages[i] = (hwaddr)ldl_le_p(ppn) << 12;
        if (!ans_ram_range(pages[i], ANS_SECTOR_SIZE)) { return 1; }
        uint64_t current = (uint64_t)lba + i;
        if (write && !region && !((current >= ANS_SYS_START && current < s->data_start && s->system_writable) ||
                       (current >= s->data_start && current < s->data_start + s->data_lbas))) {
            return 5; /* GPT/reserved sectors and read-only System cannot discard writes. */
        }
    }
    qemu_log_mask(LOG_UNIMP, "ANS modeled I/O opcode=0x%02x %s page=%u count=%u\n", opcode, write ? "WRITE" : "READ", lba, count);
    for (uint32_t i = 0; i < count; i++) {
        uint64_t current = (uint64_t)lba + i;
        int fd = -1;
        off_t offset = 0;
        if (region) {
            fd = s->utility_fd; offset = (region->first_page + current) * ANS_SECTOR_SIZE;
        } else if (current >= ANS_SYS_START && current < s->data_start) {
            fd = s->sys_fd; offset = (current - ANS_SYS_START) * ANS_SECTOR_SIZE;
        } else if (current >= s->data_start && current < s->data_start + s->data_lbas) {
            fd = s->data_fd; offset = (current - s->data_start) * ANS_SECTOR_SIZE;
        }
        if (write) {
            if (!ans_read_ram(pages[i], buf, sizeof(buf)) || !ans_file_io(fd, buf, offset, true)) {
                status = 5; break;
            }
        } else {
            if (fd >= 0) {
                if (!ans_file_io(fd, buf, offset, false)) { status = 5; break; }
            } else if (current == 0) { memcpy(buf, s->gpt_lba0, sizeof(buf)); }
            else if (current == 1) { memcpy(buf, s->gpt_lba1, sizeof(buf)); }
            else if (current == 2 || current == s->total_lbas - 2) { memcpy(buf, s->gpt_lba2, sizeof(buf)); }
            else if (current == s->total_lbas - 1) { memcpy(buf, s->gpt_alt_hdr, sizeof(buf)); }
            else { memset(buf, 0, sizeof(buf)); } /* Explicit reserved virtual sectors. */
            if (!ans_write_ram(pages[i], buf, sizeof(buf))) { status = 5; break; }
        }
    }
    /* A completed write is durable; flush even a partially failed host request. */
    if (write && !ans_flush(s)) { status = 5; }
    return status;
}

static void ans_handle_inbox_msg(A7State *s, uint64_t msg)
{
    if (s->iop_diagnostic || s->sys_fd < 0) { return; }
    if (s->ans.outbox_count == ANS_OUTBOX_CAPACITY) {
        qemu_log_mask(LOG_GUEST_ERROR, "ANS modeled inbox backpressure: request not executed\n");
        return;
    }
    uint8_t ep = (msg >> 56) & 0xff;
    uint8_t type = (msg >> 52) & 0xf;
    uint64_t payload = msg & UINT64_C(0x000fffffffffffff);

    qemu_log_mask(LOG_UNIMP, "ANS inbox: EP=%u type=0x%x payload=0x%" PRIx64 " (raw=0x%" PRIx64 ")\n",
                  ep, type, payload, msg);

    if (ep == 0) {
        /* Management Endpoint */
        if (type == 2) {
            /* MGMT_MSG_HELLO_ACK: kernel acknowledged our HELLO */
            qemu_log_mask(LOG_UNIMP, "ANS CSI: received HELLO_ACK\n");
            /* Send MGMT_MSG_POWER_ACK with state 0x20 to move IOP to running state (0x100) */
            uint64_t pwr_ack = ((uint64_t)0 << 56) | ((uint64_t)7 << 52) | 0x20ULL;
            ans_outbox_push(s, pwr_ack);
        } else if (type == 6) {
            /* MGMT_MSG_POWER_STATUS: kernel changing power state, reply with MGMT_MSG_POWER_ACK (type 7) */
            uint64_t pwr_state = payload & 0xff;
            qemu_log_mask(LOG_UNIMP, "ANS CSI: received POWER_STATUS 0x%" PRIx64 ", sending POWER_ACK (state=0x%" PRIx64 ")\n",
                          payload, pwr_state);
            uint64_t resp = ((uint64_t)0 << 56) | ((uint64_t)7 << 52) | pwr_state;
            ans_outbox_push(s, resp);
        } else if (type == 3) {
            /* Ping: reply with PING_ACK (type 4) */
            uint64_t resp = ((uint64_t)0 << 56) | ((uint64_t)4 << 52) | payload;
            ans_outbox_push(s, resp);
        }
    } else if (ep == 4) {
        /* ASP (Apple Storage Processor) Endpoint */
        uint8_t asp_type = payload & 0xf;
        uint8_t tag = (payload >> 4) & 0xff;
        qemu_log_mask(LOG_UNIMP, "ANS ASP: msg=0x%" PRIx64 " (type=%u tag=%u)\n", msg, asp_type, tag);

        if (asp_type == 0) {
            /* Physical address base registration: (fASPCommandsPhysAddr << 16) | 0x4750 */
            hwaddr base = payload >> 16;
            s->ans.asp_cmd_phys = (payload & 0xffff) == 0x4750 &&
                ans_ram_range(base, 0x11d40) ? base : 0;
            qemu_log_mask(LOG_UNIMP, "ANS ASP: registered fASPCommandsPhysAddr = 0x%" PRIx64 "\n",
                          s->ans.asp_cmd_phys);
        } else if (asp_type == 1) {
            /* Pool tag configuration notification (one-way) */
        } else if (asp_type == 3) {
            if (tag == 0xfe) {
                /* ANCReady handshake: Protocol version MUST BE 10 (0xa) in bits [15:12] */
                uint64_t resp = ((uint64_t)4 << 56) | (10ULL << 12) | (0xfeULL << 4) | 2ULL;
                ans_outbox_push(s, resp);
            } else {
                uint32_t status = ans_execute(s, tag);
                uint64_t resp = ((uint64_t)4 << 56) | ((uint64_t)status << 12) |
                                ((uint64_t)tag << 4) | 2;
                ans_outbox_push(s, resp);
            }
        }
    }
}

static uint64_t sio_pack_msg(uint8_t ep, uint8_t tag, uint8_t op,
                             uint8_t param, uint32_t data)
{
    return (uint64_t)ep |
           ((uint64_t)tag << 8) |
           ((uint64_t)op << 16) |
           ((uint64_t)param << 24) |
           ((uint64_t)data << 32);
}

static void sio_update_irq(A7State *s)
{
    /* IRQ 135: SIO Outbox non-empty interrupt (DeviceTree interrupts index 2) */
    bool outbox_irq_en = (s->sio.outbox_ctrl & 1) != 0;
    bool outbox_has_data = (s->sio.outbox_count > 0) && s->sio.irq_armed;
    if (outbox_irq_en && outbox_has_data) {
        s->device_irq[135 / 32] |= (1U << (135 % 32));
    } else {
        s->device_irq[135 / 32] &= ~(1U << (135 % 32));
    }

    /* IRQ 132: SIO Inbox empty interrupt (DeviceTree interrupts index 1) */
    bool inbox_irq_en = ((s->sio.inbox_int_en & 1) || (s->sio.inbox_int_en & 0x1000));
    if (inbox_irq_en && s->sio.inbox_empty_pending) {
        s->device_irq[132 / 32] |= (1U << (132 % 32));
    } else {
        s->device_irq[132 / 32] &= ~(1U << (132 % 32));
    }

    aic_update(s);
}

static void sio_irq_timer_cb(void *opaque)
{
    A7State *s = opaque;
    if (s->sio.outbox_count > 0) {
        s->sio.irq_armed = true;
    }
    sio_update_irq(s);
}

static void sio_outbox_push(A7State *s, uint64_t msg)
{
    if (s->sio.outbox_count < SIO_OUTBOX_CAPACITY) {
        s->sio.outbox_queue[s->sio.outbox_tail] = msg;
        s->sio.outbox_tail = (s->sio.outbox_tail + 1) % SIO_OUTBOX_CAPACITY;
        s->sio.outbox_count++;
        qemu_log_mask(LOG_UNIMP, "SIO outbox pushed 0x%" PRIx64 " (count=%u)\n",
                      msg, s->sio.outbox_count);
        if (s->sio.irq_timer) {
            s->sio.irq_armed = false;
            timer_mod(s->sio.irq_timer,
                      qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 1000);
        } else {
            s->sio.irq_armed = true;
        }
        sio_update_irq(s);
    } else {
        qemu_log_mask(LOG_GUEST_ERROR, "SIO outbox queue overflow, dropping 0x%" PRIx64 "\n", msg);
    }
}

static uint64_t sio_outbox_pop(A7State *s)
{
    if (s->sio.outbox_count == 0) {
        return 0;
    }
    uint64_t msg = s->sio.outbox_queue[s->sio.outbox_head];
    s->sio.outbox_head = (s->sio.outbox_head + 1) % SIO_OUTBOX_CAPACITY;
    s->sio.outbox_count--;
    s->sio.last_popped_msg = msg;
    if (s->sio.outbox_count == 0) {
        s->sio.irq_armed = false;
    }
    qemu_log_mask(LOG_UNIMP, "SIO outbox popped 0x%" PRIx64 " (remaining=%u)\n",
                  msg, s->sio.outbox_count);
    sio_update_irq(s);
    return msg;
}

static bool sio_execute_aes(A7State *s, uint32_t slot_index)
{
    static const uint8_t sio_uid_key[32] = {
        0x56, 0x50, 0x48, 0x4f, 0x4e, 0x45, 0x2d, 0x53,
        0x35, 0x4c, 0x38, 0x39, 0x36, 0x30, 0x58, 0x2d,
        0x55, 0x49, 0x44, 0x2d, 0x4b, 0x45, 0x59, 0x2d,
        0x30, 0x30, 0x30, 0x30, 0x30, 0x33, 0x45, 0x38
    };
    static const uint8_t sio_gid0_key[32] = {
        0x56, 0x50, 0x48, 0x4f, 0x4e, 0x45, 0x2d, 0x53,
        0x35, 0x4c, 0x38, 0x39, 0x36, 0x30, 0x58, 0x2d,
        0x47, 0x49, 0x44, 0x30, 0x2d, 0x4b, 0x45, 0x59,
        0x30, 0x30, 0x30, 0x30, 0x30, 0x33, 0x45, 0x39
    };
    static const uint8_t sio_gid1_key[32] = {
        0x56, 0x50, 0x48, 0x4f, 0x4e, 0x45, 0x2d, 0x53,
        0x35, 0x4c, 0x38, 0x39, 0x36, 0x30, 0x58, 0x2d,
        0x47, 0x49, 0x44, 0x31, 0x2d, 0x4b, 0x45, 0x59,
        0x30, 0x30, 0x30, 0x30, 0x30, 0x37, 0x44, 0x30
    };
    uint8_t op_buf[0x200];
    if (!s->sio.aes_op_phys || slot_index >= s->sio.aes_op_count || slot_index >= 8) {
        return false;
    }
    hwaddr op_addr = s->sio.aes_op_phys + (hwaddr)slot_index * 0x200;
    if (!ans_read_ram(op_addr, op_buf, sizeof(op_buf))) {
        return false;
    }
    uint32_t ctrl = ldl_le_p(op_buf + 0x00);
    uint32_t key_type = ldl_le_p(op_buf + 0x04);
    uint32_t total_blocks = ldl_le_p(op_buf + 0x38);
    uint32_t src_lo32 = ldl_le_p(op_buf + 0x178);
    uint32_t src_blocks = ldl_le_p(op_buf + 0x17c);
    uint32_t dst_lo32 = ldl_le_p(op_buf + 0x180);
    uint32_t dst_blocks = ldl_le_p(op_buf + 0x184);

    const uint8_t *key_ptr = NULL;
    int key_bits = 0;
    switch (key_type) {
    case 1:
        key_ptr = op_buf + 0x08;
        key_bits = 128;
        break;
    case 2:
        key_ptr = op_buf + 0x08;
        key_bits = 192;
        break;
    case 3:
    case 4:
        key_ptr = op_buf + 0x08;
        key_bits = 256;
        break;
    case 0x1e:
        if (s->sio.disabled_keys & (1ULL << 0x1e)) { return false; }
        key_ptr = sio_uid_key;
        key_bits = 256;
        break;
    case 0x1f:
        if (s->sio.disabled_keys & (1ULL << 0x1f)) { return false; }
        key_ptr = sio_gid0_key;
        key_bits = 256;
        break;
    case 0x14:
        if (s->sio.disabled_keys & (1ULL << 0x14)) { return false; }
        key_ptr = sio_gid1_key;
        key_bits = 256;
        break;
    default:
        return false;
    }

    bool encrypt = (ctrl & 0x03) == 0x02;
    bool decrypt = (ctrl & 0x03) == 0x01;
    bool cbc = (ctrl & 0x18) == 0x10;
    bool ecb = (ctrl & 0x18) == 0x08;
    bool inline_buf = (ctrl & 0x400) != 0;
    if ((!encrypt && !decrypt) || (!cbc && !ecb) ||
        total_blocks == 0 || total_blocks > (GiB >> 4)) {
        return false;
    }

    hwaddr src_pa, dst_pa;
    if (inline_buf) {
        if (total_blocks > 8) {
            return false;
        }
        src_pa = op_addr + 0x100;
        dst_pa = op_addr + 0x100;
    } else {
        if (src_blocks < total_blocks || dst_blocks < total_blocks) {
            return false;
        }
        src_pa = RAM_BASE + (hwaddr)(src_lo32 & 0x7fffffffU);
        dst_pa = RAM_BASE + (hwaddr)(dst_lo32 & 0x7fffffffU);
    }
    uint64_t byte_len = (uint64_t)total_blocks * 16;
    if (!ans_ram_range(src_pa, byte_len) || !ans_ram_range(dst_pa, byte_len)) {
        return false;
    }

    AES_KEY aes_key;
    if (encrypt) {
        if (AES_set_encrypt_key(key_ptr, key_bits, &aes_key) != 0) {
            return false;
        }
    } else {
        if (AES_set_decrypt_key(key_ptr, key_bits, &aes_key) != 0) {
            return false;
        }
    }

    uint8_t iv[16];
    memcpy(iv, op_buf + 0x28, 16);
    for (uint32_t b = 0; b < total_blocks; b++) {
        uint8_t in_blk[16], out_blk[16];
        if (!ans_read_ram(src_pa + (hwaddr)b * 16, in_blk, 16)) {
            return false;
        }
        if (encrypt) {
            if (cbc) {
                for (int i = 0; i < 16; i++) {
                    in_blk[i] ^= iv[i];
                }
            }
            AES_encrypt(in_blk, out_blk, &aes_key);
            if (cbc) {
                memcpy(iv, out_blk, 16);
            }
        } else {
            AES_decrypt(in_blk, out_blk, &aes_key);
            if (cbc) {
                for (int i = 0; i < 16; i++) {
                    out_blk[i] ^= iv[i];
                }
                memcpy(iv, in_blk, 16);
            }
        }
        if (!ans_write_ram(dst_pa + (hwaddr)b * 16, out_blk, 16)) {
            return false;
        }
    }
    if (cbc && !ans_write_ram(op_addr + 0x28, iv, 16)) {
        return false;
    }
    return true;
}

static void sio_handle_inbox_msg(A7State *s, uint64_t msg)
{
    if (s->sio.outbox_count == SIO_OUTBOX_CAPACITY) {
        qemu_log_mask(LOG_GUEST_ERROR, "SIO modeled inbox backpressure: request not executed\n");
        return;
    }
    uint8_t ep = msg & 0xff;
    uint8_t tag = (msg >> 8) & 0xff;
    uint8_t op = (msg >> 16) & 0xff;
    uint8_t param = (msg >> 24) & 0xff;
    uint32_t data = (uint32_t)(msg >> 32);

    qemu_log_mask(LOG_UNIMP,
                  "SIO inbox: EP=%u tag=%u op=0x%02x param=0x%02x data=0x%x (raw=0x%" PRIx64 ")\n",
                  ep, tag, op, param, data, msg);

    if (ep == 0) {
        /* kEndpointControl (AppleSmartIOControl) */
        if (op == 2) {
            /* getParam */
            sio_outbox_push(s, sio_pack_msg(0, tag, 0x67, param, s->sio.params[param]));
        } else if (op == 3) {
            /* setParam */
            s->sio.params[param] = data;
            if (param == 1) {
                s->sio.dma_seg_phys = (uint64_t)data << 12;
            } else if (param == 2) {
                s->sio.dma_seg_count = data;
            } else if (param == 3) {
                s->sio.aes_op_phys = (uint64_t)data << 12;
            } else if (param == 4) {
                s->sio.aes_op_count = data;
            }
            sio_outbox_push(s, sio_pack_msg(0, tag, 0x65, param, 0));
        } else {
            /* send_command / suspend */
            sio_outbox_push(s, sio_pack_msg(0, tag, 0x65, param, 0));
        }
    } else if (ep == 1) {
        /* AppleSmartIOAES */
        if (op == 0x0b) {
            bool ok = sio_execute_aes(s, data);
            sio_outbox_push(s, sio_pack_msg(1, tag, ok ? 0x65 : 0x66, 0, 0));
        } else if (op == 0x0c) {
            /* disableHardwareKey */
            if (param < 64) {
                s->sio.disabled_keys |= (1ULL << param);
            }
            sio_outbox_push(s, sio_pack_msg(1, tag, 0x65, param, 0));
        } else if (op == 0x0d) {
            /* isKeySupported */
            bool supported = (param == 0x1e || param == 0x1f || param == 0x14) &&
                             !(s->sio.disabled_keys & (1ULL << param));
            sio_outbox_push(s, sio_pack_msg(1, tag, supported ? 0x65 : 0x66, param, 0));
        } else {
            sio_outbox_push(s, sio_pack_msg(1, tag, 0x66, param, 0));
        }
    } else if (ep >= 2 && ep < 0x50) {
        /* AppleSmartIODMA */
        if (op == 7) {
            sio_outbox_push(s, sio_pack_msg(ep, tag, 0x69, 0, 0));
        } else if (op == 8) {
            sio_outbox_push(s, sio_pack_msg(ep, tag, 0x65, 0, 0));
            sio_outbox_push(s, sio_pack_msg(ep, 0, 0x66, 0, 0));
        } else {
            sio_outbox_push(s, sio_pack_msg(ep, tag, 0x65, 0, 0));
        }
    }
}

static void sep_update_irq(A7State *s)
{
    /* IRQ 47: SEP Outbox non-empty interrupt (DeviceTree interrupts index 2) */
    bool outbox_irq_en = (s->sep.outbox_ctrl & 1) != 0;
    bool outbox_has_data = (s->sep.outbox_count > 0) && s->sep.irq_armed;
    if (outbox_irq_en && outbox_has_data) {
        s->device_irq[47 / 32] |= (1U << (47 % 32));
    } else {
        s->device_irq[47 / 32] &= ~(1U << (47 % 32));
    }

    /* IRQ 44: SEP Inbox empty interrupt (DeviceTree interrupts index 1) */
    /* AppleA7IOP 0x915900: +0x1004 sets enable bit 0; +0x1000
     * clears it. Initial +0x1004=0x1000 is configuration, not IRQ enable.
     */
    bool inbox_irq_en = (s->sep.inbox_int_en & 1) != 0;
    if (inbox_irq_en && s->sep.inbox_empty_pending) {
        s->device_irq[44 / 32] |= (1U << (44 % 32));
    } else {
        s->device_irq[44 / 32] &= ~(1U << (44 % 32));
    }

    aic_update(s);
}

static void sep_irq_timer_cb(void *opaque)
{
    A7State *s = opaque;
    if (s->sep.outbox_count > 0) {
        s->sep.irq_armed = true;
    }
    sep_update_irq(s);
}

static void sep_outbox_push(A7State *s, uint64_t msg)
{
    if (s->sep.outbox_count < SEP_OUTBOX_CAPACITY) {
        s->sep.outbox_queue[s->sep.outbox_tail] = msg;
        s->sep.outbox_tail = (s->sep.outbox_tail + 1) % SEP_OUTBOX_CAPACITY;
        s->sep.outbox_count++;
        qemu_log_mask(LOG_UNIMP, "SEP outbox pushed 0x%" PRIx64 " (count=%u)\n",
                      msg, s->sep.outbox_count);
        if (s->sep.irq_timer) {
            s->sep.irq_armed = false;
            timer_mod(s->sep.irq_timer,
                      qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 1000);
        } else {
            s->sep.irq_armed = true;
        }
        sep_update_irq(s);
    } else {
        qemu_log_mask(LOG_GUEST_ERROR, "SEP outbox queue overflow, dropping 0x%" PRIx64 "\n", msg);
    }
}

static void sep_os_active_timer_cb(void *opaque)
{
    A7State *s = opaque;
    if (!s->sep.os_active_pending || s->sep.os_active_sent) {
        return;
    }
    if (s->sep.outbox_count == SEP_OUTBOX_CAPACITY) {
        timer_mod(s->sep.os_active_timer,
                  qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 2000000);
        return;
    }
    s->sep.os_active_pending = false;
    s->sep.os_active_sent = true;
    qemu_log_mask(LOG_UNIMP, "SEP software model OS-active (not firmware execution)\n");
    sep_outbox_push(s, sio_pack_msg(0, 0, 0x0d, 0, 0));
}

static uint64_t sep_outbox_pop(A7State *s)
{
    if (s->sep.outbox_count == 0) {
        return 0;
    }
    uint64_t msg = s->sep.outbox_queue[s->sep.outbox_head];
    uint32_t slot = s->sep.outbox_head;
    s->sep.outbox_head = (s->sep.outbox_head + 1) % SEP_OUTBOX_CAPACITY;
    s->sep.outbox_count--;
    s->sep.last_popped_msg = msg;
    if (s->sep.finish_reply_pending && slot == s->sep.finish_reply_slot) {
        s->sep.finish_reply_pending = false;
        s->sep.os_active_pending = true;
        /* Model asynchronous boot after the AP consumes its finish reply.
         * This virtual-time delay is model policy, not a measured SEP latency.
         * Native state 4 -> 5 ordering must also be checked in iOS probes.
         */
        timer_mod(s->sep.os_active_timer,
                  qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 2000000);
    }
    if (s->sep.outbox_count == 0) {
        s->sep.irq_armed = false;
    }
    qemu_log_mask(LOG_UNIMP, "SEP outbox popped 0x%" PRIx64 " (remaining=%u)\n",
                  msg, s->sep.outbox_count);
    sep_update_irq(s);
    return msg;
}

static void sep_handle_inbox_msg(A7State *s, uint64_t msg)
{
    if (s->sep.outbox_count == SEP_OUTBOX_CAPACITY) {
        qemu_log_mask(LOG_GUEST_ERROR, "SEP modeled inbox backpressure: request not executed\n");
        return;
    }
    uint8_t ep = msg & 0xff;
    uint8_t tag = (msg >> 8) & 0xff;
    uint8_t op = (msg >> 16) & 0xff;
    uint8_t param = (msg >> 24) & 0xff;
    uint32_t data = msg >> 32;
    hwaddr page = (hwaddr)data << 12;
    uint32_t status = 0;
    qemu_log_mask(LOG_UNIMP,
                  "SEP software inbox: ep=0x%x tag=0x%x op=0x%x param=0x%x "
                  "data=0x%x phase=%u raw=0x%" PRIx64 "\n",
                  ep, tag, op, param, data, s->sep.boot_phase, msg);
    if (ep == 0) {
        if (s->sep.boot_phase != SEP_BOOT_FINISHED) {
            goto unsupported;
        }
        if (op == 2) {
            /* AppleSEPControl:
             * op == 2: setSendOOLBuffer(endpoint=param, phys_page=data)
             * If data == 0: getParam(param).
             * AppleSEPControl::_cmsgSend checks byte 3 == 0 for kIOReturnSuccess.
             */
            if (data != 0) {
                s->sep.params[param] = data;
                s->sep.send_ool_page[param] = data;
            }
            if (tag != 0) {
                uint64_t resp = sio_pack_msg(0, tag, data != 0 ? 0x65 : 0x67,
                                             0, s->sep.params[param]);
                sep_outbox_push(s, resp);
            }
            return;
        } else if (op == 3) {
            /* AppleSEPControl:
             * op == 3: setReceiveOOLBuffer(endpoint=param, phys_page=data) / setParam
             */
            s->sep.params[param] = data;
            s->sep.recv_ool_page[param] = data;
            if (tag != 0) {
                uint64_t resp = sio_pack_msg(0, tag, 0x65, 0, 0);
                sep_outbox_push(s, resp);
            }
            return;
        } else if (op == 0 || op == 0x0a || op == 0x0c) {
            /* General AppleSEPControl command: ACK with success */
            if (tag != 0) {
                uint64_t resp = sio_pack_msg(0, tag, 0x65, 0, 0);
                sep_outbox_push(s, resp);
            }
            return;
        } else if (op == 0x13) {
            /* Idle / keep-alive notification from AP (sent with tag 0) */
            return;
        }
        goto unsupported;
    }
    if (ep == 2) {
        if (s->sep.boot_phase != SEP_BOOT_FINISHED) {
            goto unsupported;
        }
        /* AppleSEPARTStorage:
         * op == 2 (ART_LOAD):
         * AppleSEPARTStorage::handle_first_connected sends (ep=2, tag, op=2) with
         * the current ART in Send OOL Buffer.
         * AppleSEPARTStorage::_msgAction tests bit 16 (0x10000) for reply.
         * AppleSEPARTStorage::_msgSend tests byte 3 (param) == 0 for kIOReturnSuccess.
         * Modeled reply: op=3 (sets bit 16), param=0 (success).
         */
        if (op == 2) {
            uint64_t resp = sio_pack_msg(2, tag, 3, 0, 0);
            sep_outbox_push(s, resp);
            return;
        }
        goto unsupported;
    }
    if (ep == 7) {
        if (s->sep.boot_phase != SEP_BOOT_FINISHED) {
            goto unsupported;
        }
        /* AppleSEPKeyStore:
         * Wire format:
         * bits 7:0   = ep (7)
         * bits 14:8  = cmd_id
         * bit 15     = reply flag (0 in request, 1 in reply)
         * bits 23:16 = tag
         * bits 31:24 = status (0 in success)
         * bits 47:32 = offset into receive OOL buffer
         * bits 63:48 = payload length in receive OOL buffer
         */
        uint8_t cmd = (msg >> 8) & 0x7f;
        uint8_t cmd_tag = (msg >> 16) & 0xff;
        if (cmd == 0x11 || cmd == 0x19) {
            /* Cmd 17 (0x11): get_lock_state.
             * Cmd 25 (0x19): get_lock_state (user client / system keybag query).
             * AppleSEPKeyStore::send_command checks:
             * - reply bit 15 == 1 (in _msgAction, to wake waiter)
             * - bits 23:16 == tag
             * - bits 31:24 == 0 (status success)
             * - bits 47:32 == offset (0)
             * Cmd 17 unpacks four words; Cmd 25 unpacks two (db1974).
             * Do not overwrite bytes outside the declared reply payload.
             */
            uint32_t recv_page = s->sep.recv_ool_page[7] ? s->sep.recv_ool_page[7] : s->sep.params[7];
            hwaddr recv_pa = (hwaddr)recv_page << 12;
            size_t reply_length = cmd == 0x19 ? 8 : 16;
            uint8_t zeros[16] = {0};
            if (!recv_page || !ans_write_ram(recv_pa, zeros, reply_length)) {
                goto unsupported;
            }
            uint64_t resp = ((uint64_t)ep) |
                            ((uint64_t)cmd << 8) |
                            (1ULL << 15) |
                            ((uint64_t)cmd_tag << 16) |
                            ((uint64_t)reply_length << 48);
            sep_outbox_push(s, resp);
            return;
        } else if (cmd == 0x04) {
            /* Cmd 4 is an operation, not an unconditional successful query.
             * db03cc serializes context, bag, operation, blob length/data.
             * Selector 14 (dae53c) requests bag 0, operation -1, no blob.
             * No system bag has been imported by this model. dabc3c sign-
             * extends status; -3 maps through db2fd0 to kIOReturnNotFound.
             * Other operations require separately recovered contracts.
             */
            uint16_t offset = msg >> 32;
            uint16_t length = msg >> 48;
            hwaddr send_pa = (hwaddr)s->sep.send_ool_page[7] << 12;
            uint8_t request[16];
            if (length != sizeof(request) || !send_pa ||
                offset > 4096 - sizeof(request) ||
                !ans_read_ram(send_pa + offset, request, sizeof(request))) {
                goto unsupported;
            }
            uint32_t bag = ldl_le_p(request + 4);
            int32_t operation = (int32_t)ldl_le_p(request + 8);
            uint32_t blob_length = ldl_le_p(request + 12);
            qemu_log_mask(LOG_UNIMP,
                          "SEP Cmd4 context=0x%x bag=0x%x operation=%d blob_length=%u\n",
                          ldl_le_p(request), bag, operation, blob_length);
            if (bag != 0 || operation != -1 || blob_length != 0) {
                goto unsupported;
            }
            uint64_t resp = ((uint64_t)ep) |
                            ((uint64_t)cmd << 8) |
                            (1ULL << 15) |
                            ((uint64_t)cmd_tag << 16) |
                            (UINT64_C(0xfd) << 24);
            sep_outbox_push(s, resp);
            return;
        } else if (cmd == 0x03) {
            /* Cmd 3 (0x03): AppleKeyStoreKeyBagCreateWithData / import keybag from data.
             * Handler 0xffffff8000daf388 / serializer 0xffffff8000db02fc.
             * Request: OOL buffer has [context (4B)][blob_len (4B)][blob_bytes...].
             * Reply: 4 bytes payload containing assigned uint32_t handle.
             * Reply mailbox: bit 15 set, tag echoed, status 0, length 4 (4ULL << 48).
             */
            uint16_t offset = msg >> 32;
            uint16_t length = msg >> 48;
            hwaddr send_pa = (hwaddr)s->sep.send_ool_page[7] << 12;
            uint32_t recv_page = s->sep.recv_ool_page[7] ? s->sep.recv_ool_page[7] : s->sep.params[7];
            hwaddr recv_pa = (hwaddr)recv_page << 12;
            if (length < 8 || !send_pa || !recv_page || offset > 4096 - 8) {
                goto unsupported;
            }
            uint8_t req_hdr[8];
            if (!ans_read_ram(send_pa + offset, req_hdr, sizeof(req_hdr))) {
                goto unsupported;
            }
            uint32_t blob_len = ldl_le_p(req_hdr + 4);
            if (length < 8 + blob_len || offset > 4096 - (8 + blob_len)) {
                goto unsupported;
            }
            if (blob_len <= sizeof(s->sep.keybag_blob)) {
                ans_read_ram(send_pa + offset + 8, s->sep.keybag_blob, blob_len);
                s->sep.keybag_blob_len = blob_len;
            }
            uint8_t handle_buf[4];
            stl_le_p(handle_buf, 1);
            if (!ans_write_ram(recv_pa, handle_buf, sizeof(handle_buf))) {
                goto unsupported;
            }
            uint64_t resp = ((uint64_t)ep) |
                            ((uint64_t)cmd << 8) |
                            (1ULL << 15) |
                            ((uint64_t)cmd_tag << 16) |
                            (4ULL << 48);
            sep_outbox_push(s, resp);
            return;
        } else if (cmd == 0x06) {
            /* Cmd 6 (0x06): AppleKeyStoreKeyBagCopyData / get keybag blob by handle.
             * Handler 0xffffff8000daf494 / serializer 0xffffff8000db0594.
             * Request: OOL buffer has [context (4B)][handle (4B)]. Total 8 bytes.
             * Reply: OOL buffer has [blob_len (4B)][blob bytes...].
             * Mailbox reply: length = (blob_len + 7) & ~3, status = 0.
             */
            uint16_t offset = msg >> 32;
            uint16_t length = msg >> 48;
            hwaddr send_pa = (hwaddr)s->sep.send_ool_page[7] << 12;
            uint32_t recv_page = s->sep.recv_ool_page[7] ? s->sep.recv_ool_page[7] : s->sep.params[7];
            hwaddr recv_pa = (hwaddr)recv_page << 12;
            if (length != 8 || !send_pa || !recv_page || offset > 4096 - 8) {
                goto unsupported;
            }
            uint8_t req[8];
            if (!ans_read_ram(send_pa + offset, req, sizeof(req))) {
                goto unsupported;
            }
            uint32_t handle = ldl_le_p(req + 4);
            qemu_log_mask(LOG_UNIMP, "SEP Cmd6 handle=0x%x keybag_len=%u\n",
                          handle, s->sep.keybag_blob_len);
            uint32_t reply_blob_len = s->sep.keybag_blob_len;
            uint16_t reply_payload_len = (reply_blob_len + 7) & ~3;
            if (reply_payload_len > 4096) {
                goto unsupported;
            }
            uint8_t len_buf[4];
            stl_le_p(len_buf, reply_blob_len);
            if (!ans_write_ram(recv_pa, len_buf, 4)) {
                goto unsupported;
            }
            if (reply_blob_len > 0) {
                if (!ans_write_ram(recv_pa + 4, s->sep.keybag_blob, reply_blob_len)) {
                    goto unsupported;
                }
            }
            uint64_t resp = ((uint64_t)ep) |
                            ((uint64_t)cmd << 8) |
                            (1ULL << 15) |
                            ((uint64_t)cmd_tag << 16) |
                            ((uint64_t)reply_payload_len << 48);
            sep_outbox_push(s, resp);
            return;
        } else if (cmd == 0x07) {
            /* Cmd 7 (0x07): Handler 0xffffff8000daf50c / serializer 0xffffff8000db0664.
             * Reply: 4 bytes payload (1 uint32).
             */
            uint32_t recv_page = s->sep.recv_ool_page[7] ? s->sep.recv_ool_page[7] : s->sep.params[7];
            hwaddr recv_pa = (hwaddr)recv_page << 12;
            uint8_t zeros[4] = {0};
            if (!recv_page || !ans_write_ram(recv_pa, zeros, 4)) {
                goto unsupported;
            }
            uint64_t resp = ((uint64_t)ep) |
                            ((uint64_t)cmd << 8) |
                            (1ULL << 15) |
                            ((uint64_t)cmd_tag << 16) |
                            (4ULL << 48);
            sep_outbox_push(s, resp);
            return;
        } else if (cmd == 0x08) {
            /* Cmd 8 (0x08): AppleSEPKeyStore::unwrap_key / keybag unwrap / sign.
             * Handler 0xffffff8000daf584 / serializer 0xffffff8000db0790.
             * Request: OOL buffer has [context (4B)][handle (4B)][class (4B)]
             *          [blob_len (4B)][blob bytes...][extra (4B)].
             * Reply: OOL buffer has [blob_len (4B)][blob bytes...][extra_u32 (4B)].
             * Mailbox reply: length = ((blob_len + 7) & ~3) + 4, status = 0.
             */
            uint16_t offset = msg >> 32;
            uint16_t length = msg >> 48;
            hwaddr send_pa = (hwaddr)s->sep.send_ool_page[7] << 12;
            uint32_t recv_page = s->sep.recv_ool_page[7] ? s->sep.recv_ool_page[7] : s->sep.params[7];
            hwaddr recv_pa = (hwaddr)recv_page << 12;
            if (length < 16 || !send_pa || !recv_page || offset > 4096 - 16) {
                goto unsupported;
            }
            uint8_t req_hdr[16];
            if (!ans_read_ram(send_pa + offset, req_hdr, sizeof(req_hdr))) {
                goto unsupported;
            }
            uint32_t in_blob_len = ldl_le_p(req_hdr + 12);
            uint32_t in_blob_aligned = (in_blob_len + 3) & ~3;
            if (length < 16 + in_blob_aligned + 4 || offset > 4096 - (16 + in_blob_aligned + 4)) {
                goto unsupported;
            }
            /* In unencrypted/no-effaceable mode, pass through the key blob directly */
            uint32_t reply_blob_len = in_blob_len;
            uint16_t reply_blob_aligned = (reply_blob_len + 7) & ~3;
            uint16_t reply_payload_len = reply_blob_aligned + 4;
            if (reply_payload_len > 4096) {
                goto unsupported;
            }
            uint8_t len_buf[4];
            stl_le_p(len_buf, reply_blob_len);
            if (!ans_write_ram(recv_pa, len_buf, 4)) {
                goto unsupported;
            }
            if (reply_blob_len > 0) {
                uint8_t blob_buf[1024];
                if (reply_blob_len <= sizeof(blob_buf)) {
                    ans_read_ram(send_pa + offset + 16, blob_buf, reply_blob_len);
                    ans_write_ram(recv_pa + 4, blob_buf, reply_blob_len);
                }
            }
            uint8_t extra_buf[4] = {0};
            if (!ans_write_ram(recv_pa + reply_blob_aligned, extra_buf, 4)) {
                goto unsupported;
            }
            uint64_t resp = ((uint64_t)ep) |
                            ((uint64_t)cmd << 8) |
                            (1ULL << 15) |
                            ((uint64_t)cmd_tag << 16) |
                            ((uint64_t)reply_payload_len << 48);
            sep_outbox_push(s, resp);
            return;
        } else if (cmd == 0x02 || cmd == 0x05 || cmd == 0x0a || cmd == 0x0c || cmd == 0x0d ||
                   cmd == 0x12 || cmd == 0x13 || cmd == 0x16 || cmd == 0x1a || cmd == 0x1b ||
                   cmd == 0x1e || cmd == 0x20) {
            /* Cmd 2  (0x02): keybag init / create. Handler 0xffffff8000db022c.
             * Cmd 5  (0x05): keybag free. Handler 0xffffff8000db04e4.
             * Cmd 10 (0x0a): keybag operation / status. Handler 0xffffff8000db0a58.
             * Cmd 12 (0x0c): keybag operation. Handler 0xffffff8000db0b6c.
             * Cmd 13 (0x0d): keybag session / system set. Handler 0xffffff8000db0c20.
             * Cmd 18 (0x12): register_client / session init. Handler 0xffffff8000db129c.
             * Cmd 19 (0x13): session operation. Handler 0xffffff8000db1404.
             * Cmd 22 (0x16): keybag operation / session state. Handler 0xffffff8000db1700.
             * Cmd 26 (0x1a): key locker / keybag operation. Handler 0xffffff8000db1a74.
             * Cmd 27 (0x1b): key locker operation. Handler 0xffffff8000db1b28.
             * Cmd 30 (0x1e): system keybag unlock. Handler 0xffffff8000db1dc0.
             * Cmd 32 (0x20): keybag operation. Handler 0xffffff8000db1ff0.
             * Handlers expect 0 bytes unpacked on reply (w8 == w19 == 0).
             * Reply with bit 15 set, tag echoed, status 0, length 0.
             */
            uint64_t resp = ((uint64_t)ep) |
                            ((uint64_t)cmd << 8) |
                            (1ULL << 15) |
                            ((uint64_t)cmd_tag << 16);
            sep_outbox_push(s, resp);
            return;
        }
        goto unsupported;
    }
    if (ep != 0xff) {
        goto unsupported;
    }
    switch (op) {
    case 2: /* AppleSEPBooter::_bootAction stores reply[47:32] at +0x1b2. */
        status = s->sep.boot_phase == SEP_BOOT_ROM ? 1 : 2;
        break;
    case 5: /* BootTz0: param selects firmware type, not an AP DMA pointer. */
        if (s->sep.boot_phase != SEP_BOOT_ROM) {
            goto unsupported;
        }
        s->sep.tz0_request = msg;
        s->sep.boot_phase = SEP_BOOT_TZ0;
        break;
    case 7: /* SEPART encodes its page-aligned AP physical address >> 12. */
        if (s->sep.boot_phase != SEP_BOOT_TZ0 || !ans_ram_range(page, 4096)) {
            goto unsupported;
        }
        s->sep.art_phys = page;
        s->sep.boot_phase = SEP_BOOT_ART;
        break;
    case 6: /* Firmware-backed boot finish. Native booter may omit SEPART. */
    case 8: /* Firmware-less finish; no address is consumed. */
        if ((s->sep.boot_phase != SEP_BOOT_TZ0 &&
             s->sep.boot_phase != SEP_BOOT_ART) ||
            (op == 6 && !ans_ram_range(page, 4096))) {
            goto unsupported;
        }
        s->sep.firmware_phys = op == 6 ? page : 0;
        s->sep.boot_phase = SEP_BOOT_FINISHED;
        s->sep.finish_reply_slot = s->sep.outbox_tail;
        s->sep.finish_reply_pending = true;
        break;
    default:
        goto unsupported;
    }
    uint64_t resp = sio_pack_msg(ep, tag, 0x66, 0, status);
    sep_outbox_push(s, resp);
    return;

unsupported:
    /* No recovered service-error ABI yet: log and leave the native wait
     * observable, rather than manufacturing KeyStore or crypto success.
     */
    qemu_log_mask(LOG_UNIMP,
                  "SEP unsupported request: ep=0x%x tag=0x%x op=0x%x "
                  "param=0x%x data=0x%x phase=%u raw=0x%" PRIx64 "\n",
                  ep, tag, op, param, data, s->sep.boot_phase, msg);
}

static uint64_t peripheral_read_impl(void *opaque, hwaddr off, unsigned size)
{
    A7Peripheral *p = opaque;
    if (off + size <= p->size) {
        if (i2c_index(p) >= 0 && size == 4) {
            if (off == 4) {
                if (p->i2c_rx_valid) {
                    uint8_t byte = p->i2c_rx_byte;
                    p->i2c_rx_valid = false;
                    return byte;
                }
                return 0x100; /* Native RX FIFO empty flag. */
            }
            if (off == 8) { return p->i2c_rx_valid ? 0x100 : 0; }
            if (off == 0x0c) { return 0; }
        }
        if (p->base == UINT64_C(0x201000000) && off == 0x20) {
            /* Apple S5L8960X Rogue G6430 GPU core ID */
            return 0x46000000;
        }
        if (p->base == UINT64_C(0x206200000) && size == 4) {
            if (off == 0x0004) {
                return *(uint32_t *)(p->regs + 0x0004);
            }
            if (off == 0x1038) {
                return (*(uint32_t *)(p->regs + 0x1038)) | (1U << 8); /* Frame Swap Complete bit 8 */
            }
            if (off == 0x1044) {
                return 0; /* DPipe Flush parameter FIFO empty */
            }
            if (off == 0x1048) {
                return *(uint32_t *)(p->regs + 0x1048);
            }
            if (off == 0x104c) {
                return (*(uint32_t *)(p->regs + 0x104c)) | 1; /* Auto Mode */
            }
            if (off == 0x4048) {
                return 2560; /* Layer 0 stride */
            }
            if (off == 0x4070) {
                return (640U << 16) | 1136U; /* Geometry: 640x1136 */
            }
        }
        if (p->base == UINT64_C(0x206300000) && size == 4) {
            if (off == 0x0004) {
                return *(uint32_t *)(p->regs + 0x0004);
            }
        }
        if (p->base == UINT64_C(0x208040000) && !p->owner->iop_diagnostic && p->owner->sys_fd >= 0) {
            A7State *s = p->owner;
            qemu_log_mask(LOG_UNIMP, "ANS MMIO READ off=0x%" HWADDR_PRIx " size=%u\n", off, size);
            switch (off) {
            case 0x1000:
                return s->ans.inbox_ctrl;
            case 0x1004:
                return s->ans.inbox_int_en;
            case 0x1008:
                /* Inbox status: Bit 17 = EMPTY, Bit 16 = FULL */
                return s->ans.outbox_count == ANS_OUTBOX_CAPACITY ? (1U << 16) : (1U << 17);
            case 0x1020: {
                uint32_t st = s->ans.outbox_ctrl & 1;
                if (s->ans.outbox_count == 0) {
                    st |= (1U << 17); /* EMPTY */
                }
                if (s->ans.outbox_count >= ANS_OUTBOX_CAPACITY) {
                    st |= (1U << 16); /* FULL */
                }
                return st;
            }
            case 0x1038: {
                if (size == 8) {
                    return ans_outbox_pop(s);
                } else if (size == 4) {
                    return (uint32_t)ans_outbox_pop(s);
                }
                break;
            }
            case 0x103c: {
                if (size == 4) {
                    return (uint32_t)(s->ans.last_popped_msg >> 32);
                }
                break;
            }
            default:
                break;
            }
        }
        if (p->base == UINT64_C(0x20ae00000)) {
            A7State *s = p->owner;
            qemu_log_mask(LOG_UNIMP, "SIO MMIO READ off=0x%" HWADDR_PRIx " size=%u\n", off, size);
            switch (off) {
            case 0x1000:
                return s->sio.inbox_ctrl;
            case 0x1004:
                return s->sio.inbox_int_en;
            case 0x1008:
                /* Inbox status: Bit 17 = EMPTY, Bit 16 = FULL */
                return s->sio.outbox_count == SIO_OUTBOX_CAPACITY ? (1U << 16) : (1U << 17);
            case 0x1020: {
                if (s->sio.outbox_count > 0 && !s->sio.irq_armed) {
                    s->sio.irq_armed = true;
                    sio_update_irq(s);
                }
                uint32_t st = s->sio.outbox_ctrl & 1;
                if (s->sio.outbox_count == 0) {
                    st |= (1U << 17); /* EMPTY */
                }
                if (s->sio.outbox_count >= SIO_OUTBOX_CAPACITY) {
                    st |= (1U << 16); /* FULL */
                }
                return st;
            }
            case 0x1038: {
                if (size == 8) {
                    return sio_outbox_pop(s);
                } else if (size == 4) {
                    return (uint32_t)sio_outbox_pop(s);
                }
                break;
            }
            case 0x103c: {
                if (size == 4) {
                    return (uint32_t)(s->sio.last_popped_msg >> 32);
                }
                break;
            }
            default:
                break;
            }
        }
        if (p->base == UINT64_C(0x20d000000)) {
            A7State *s = p->owner;
            qemu_log_mask(LOG_UNIMP, "SEP MMIO READ off=0x%" HWADDR_PRIx " size=%u\n", off, size);
            switch (off) {
            case 0x1000:
                return s->sep.inbox_ctrl;
            case 0x1004:
                return s->sep.inbox_int_en;
            case 0x1008:
                /* Inbox status: Bit 17 = EMPTY, Bit 16 = FULL */
                return s->sep.outbox_count == SEP_OUTBOX_CAPACITY ? (1U << 16) : (1U << 17);
            case 0x1020: {
                if (s->sep.outbox_count > 0 && !s->sep.irq_armed) {
                    s->sep.irq_armed = true;
                    sep_update_irq(s);
                }
                uint32_t st = s->sep.outbox_ctrl & 1;
                if (s->sep.outbox_count == 0) {
                    st |= (1U << 17); /* EMPTY */
                }
                if (s->sep.outbox_count >= SEP_OUTBOX_CAPACITY) {
                    st |= (1U << 16); /* FULL */
                }
                return st;
            }
            case 0x1038: {
                if (size == 8) {
                    return sep_outbox_pop(s);
                } else if (size == 4) {
                    return (uint32_t)sep_outbox_pop(s);
                }
                break;
            }
            case 0x103c: {
                if (size == 4) {
                    return (uint32_t)(s->sep.last_popped_msg >> 32);
                }
                break;
            }
            default:
                break;
            }
        }
        if (p->base == UINT64_C(0x201000000)) {
            if (off == 0x20) {
                return 0x00014600; /* PowerVR G6430 Silicon Core ID */
            }
            if (off == 0x18) {
                return 0x00000000; /* Core Revision */
            }
        }
        if (size == 4) {
            return *(uint32_t *)(p->regs + off);
        }
        uint64_t val = 0;
        for (unsigned i = 0; i < size; i++) {
            val |= (uint64_t)p->regs[off + i] << (i * 8);
        }
        return val;
    }
    return 0;
}

static void peripheral_write_impl(void *opaque, hwaddr off, uint64_t val, unsigned size)
{
    A7Peripheral *p = opaque;
    if (off + size <= p->size) {
        if (i2c_index(p) >= 0 && size == 4) {
            i2c_write(p, off, val);
            return;
        }
        if (p->base == UINT64_C(0x201e00000) && off == 0x1000) {
            /* SGX power/reset register bit 4 self-clears upon reset completion */
            val &= ~UINT64_C(0x10);
        }
        if (p->base == UINT64_C(0x208040000) && !p->owner->iop_diagnostic && p->owner->sys_fd >= 0) {
            A7State *s = p->owner;
            qemu_log_mask(LOG_UNIMP, "ANS MMIO WRITE off=0x%" HWADDR_PRIx " val=0x%" PRIx64 " size=%u\n",
                          off, val, size);
            if (off == 0x28 && size == 4) {
                *(uint32_t *)(p->regs + off) = val;
                if (val & 0x10) {
                    if (!s->ans.hello_queued) {
                        s->ans.hello_queued = true;
                        uint64_t hello = ((uint64_t)0 << 56) | ((uint64_t)1 << 52) | 0x00080001ULL;
                        ans_outbox_push(s, hello);
                    }
                    if (s->iop_diagnostic) {
                        iop_try_start(p);
                    }
                }
                return;
            }
            switch (off) {
            case 0x884:
            case 0x8a4:
                s->ans.inbox_empty_pending = false;
                ans_update_irq(s);
                return;
            case 0x1000:
                s->ans.inbox_ctrl = val;
                ans_update_irq(s);
                return;
            case 0x1004:
                s->ans.inbox_int_en = val;
                ans_update_irq(s);
                return;
            case 0x1010:
                if (size == 8) {
                    ans_handle_inbox_msg(s, val);
                } else {
                    s->ans.inbox_low = (uint32_t)val;
                }
                ans_update_irq(s);
                return;
            case 0x1014: {
                uint64_t full_msg = ((uint64_t)(uint32_t)val << 32) | (uint64_t)s->ans.inbox_low;
                ans_handle_inbox_msg(s, full_msg);
                ans_update_irq(s);
                return;
            }
            case 0x1020:
                s->ans.outbox_ctrl = val;
                if ((val & 1) && !s->ans.hello_queued) {
                    s->ans.hello_queued = true;
                    uint64_t hello = ((uint64_t)0 << 56) | ((uint64_t)1 << 52) | 0x00080001ULL;
                    ans_outbox_push(s, hello);
                }
                ans_update_irq(s);
                return;
            default:
                break;
            }
        }
        if (p->base == UINT64_C(0x20ae00000)) {
            A7State *s = p->owner;
            qemu_log_mask(LOG_UNIMP, "SIO MMIO WRITE off=0x%" HWADDR_PRIx " val=0x%" PRIx64 " size=%u\n",
                          off, val, size);
            if (off == 0x28 && size == 4) {
                *(uint32_t *)(p->regs + off) = val;
                if ((val & 0x10) && !s->sio.os_active_queued) {
                    s->sio.os_active_queued = true;
                    /* SmartIO OS-Active control message: EP=0, TAG=0, OP=0x6d, PARAM=1 */
                    sio_outbox_push(s, sio_pack_msg(0, 0, 0x6d, 1, 0));
                }
                return;
            }
            switch (off) {
            case 0x884:
            case 0x8a4:
                s->sio.inbox_empty_pending = false;
                sio_update_irq(s);
                return;
            case 0x1000:
                s->sio.inbox_ctrl = val;
                sio_update_irq(s);
                return;
            case 0x1004:
                s->sio.inbox_int_en = val;
                sio_update_irq(s);
                return;
            case 0x1010:
                if (size == 8) {
                    sio_handle_inbox_msg(s, val);
                } else {
                    s->sio.inbox_low = (uint32_t)val;
                }
                sio_update_irq(s);
                return;
            case 0x1014: {
                uint64_t full_msg = ((uint64_t)(uint32_t)val << 32) | (uint64_t)s->sio.inbox_low;
                sio_handle_inbox_msg(s, full_msg);
                sio_update_irq(s);
                return;
            }
            case 0x1020:
                s->sio.outbox_ctrl = val;
                if ((val & 1) && !s->sio.os_active_queued) {
                    s->sio.os_active_queued = true;
                    sio_outbox_push(s, sio_pack_msg(0, 0, 0x6d, 1, 0));
                }
                sio_update_irq(s);
                return;
            default:
                break;
            }
        }
        if (p->base == UINT64_C(0x20d000000)) {
            A7State *s = p->owner;
            qemu_log_mask(LOG_UNIMP, "SEP MMIO WRITE off=0x%" HWADDR_PRIx " val=0x%" PRIx64 " size=%u\n",
                          off, val, size);
            if (off == 0x28 && size == 4) {
                *(uint32_t *)(p->regs + off) = val;
                return;
            }
            switch (off) {
            case 0x884:
            case 0x8a4:
                s->sep.inbox_empty_pending = false;
                sep_update_irq(s);
                return;
            case 0x1000:
                s->sep.inbox_ctrl = val;
                s->sep.inbox_int_en &= ~(uint32_t)val;
                sep_update_irq(s);
                return;
            case 0x1004:
                s->sep.inbox_int_en |= (uint32_t)val;
                sep_update_irq(s);
                return;
            case 0x1010:
                if (size == 8) {
                    s->sep.inbox_empty_pending = true;
                    sep_handle_inbox_msg(s, val);
                } else {
                    s->sep.inbox_low = (uint32_t)val;
                }
                sep_update_irq(s);
                return;
            case 0x1014: {
                uint64_t full_msg = ((uint64_t)(uint32_t)val << 32) | (uint64_t)s->sep.inbox_low;
                s->sep.inbox_empty_pending = true;
                sep_handle_inbox_msg(s, full_msg);
                sep_update_irq(s);
                return;
            }
            case 0x1020:
                s->sep.outbox_ctrl = val;
                sep_update_irq(s);
                return;
            default:
                break;
            }
        }
        if (p->base == UINT64_C(0x206300000) && off == 4 && size == 4) {
            /* ADBE0Driver enables timings with bit 31 (0xf411ac), then
             * reads running status bit 0 (0xf413cc). Model the synchronous
             * virtual timing generator; preserve the other configuration. */
            stl_le_p(p->regs + off, (val & ~1U) | ((val >> 31) & 1U));
            return;
        }
        if (p->base == UINT64_C(0x206200000) && size == 4) {
            if (off == 0x102c) {
                uint32_t *status = (uint32_t *)(p->regs + 0x102c);
                uint32_t mask = *(uint32_t *)(p->regs + 0x1028);
                *status &= ~(uint32_t)val;
                if ((*status & mask) == 0) {
                    p->owner->device_irq[64 / 32] &= ~(1U << (64 % 32));
                    aic_update(p->owner);
                }
                return;
            }
            if (off == 0x1028) {
                if ((val & (1U << 1)) && !(*(uint32_t *)(p->regs + 0x1028) & (1U << 1))) {
                    fprintf(stderr, "[DISP0] VBL interrupt UNMASKED (0x1028 = 0x%" PRIx64 ")\n", val);
                }
                *(uint32_t *)(p->regs + 0x1028) = (uint32_t)val;
                uint32_t status = *(uint32_t *)(p->regs + 0x102c);
                if ((status & (uint32_t)val) == 0) {
                    p->owner->device_irq[64 / 32] &= ~(1U << (64 % 32));
                    aic_update(p->owner);
                } else {
                    p->owner->device_irq[64 / 32] |= (1U << (64 % 32));
                    aic_update(p->owner);
                }
                return;
            }
            if (off == 0x10c0) {
                A7State *s = p->owner;
                if (!s->disp_fifo_expected) {
                    uint32_t count = ((uint32_t)val >> 16) & 0xfff;
                    if (((uint32_t)val >> 28) != 0xa || !count ||
                        count > ARRAY_SIZE(s->disp_fifo) || (count & 1)) {
                        qemu_log_mask(LOG_GUEST_ERROR, "DPFIFO unsupported header 0x%08x\n", (uint32_t)val);
                        return;
                    }
                    s->disp_fifo_count = 0;
                    s->disp_fifo_expected = count;
                    s->disp_fifo_token = (uint32_t)val & 0xffff;
                } else {
                    s->disp_fifo[s->disp_fifo_count++] = (uint32_t)val;
                    if (s->disp_fifo_count == s->disp_fifo_expected) {
                        unsigned count = s->disp_fifo_expected;
                        s->disp_fifo_expected = 0;
                        /* Native header A:length:token, followed by 0001:offset
                         * and value pairs. Validate all words before effects. */
                        for (unsigned i = 0; i < count; i += 2) {
                            uint32_t op = s->disp_fifo[i], addr = op & 0xffff;
                            if ((op >> 16) != 1 || (addr & 3) ||
                                addr + 4 > p->size || addr == 0x10c0) {
                                qemu_log_mask(LOG_GUEST_ERROR, "DPFIFO unsupported opcode 0x%08x\n", op);
                                return;
                            }
                        }
                        for (unsigned i = 0; i < count; i += 2) {
                            peripheral_write_impl(p, s->disp_fifo[i] & 0xffff,
                                                  s->disp_fifo[i + 1], 4);
                        }
                        s->disp_pending_token = s->disp_fifo_token;
                        s->disp_token = s->disp_fifo_token;
                        s->disp_swap_pending = true;
                        qemu_log_mask(LOG_UNIMP, "DPFIFO applied words=%u token=%u\n", count, s->disp_fifo_token);
                    }
                }
                return;
            }
            if (off == 0x1050) {
                fprintf(stderr, "[DISP0] swap commit off=0x1050 val=0x%" PRIx64 ", pending_token=%u\n",
                        val, p->owner->disp_pending_token);
                *(uint32_t *)(p->regs + 0x1044) = 0;
                p->owner->disp_swap_pending = true;
                if (p->owner->disp_pending_token != 0) {
                    p->owner->disp_token = p->owner->disp_pending_token;
                } else {
                    p->owner->disp_token++;
                }
                return;
            }
            if (off == 0x4044) {
                fprintf(stderr, "[DISP0] layer0 base off=0x4044 val=0x%" PRIx64 "\n", val);
                *(uint32_t *)(p->regs + off) = (uint32_t)val;
                if (val != 0) {
                    p->owner->framebuffer = RAM_BASE | (uint32_t)val;
                }
                return;
            }
        }
        if (size == 4) {
            *(uint32_t *)(p->regs + off) = val;
            if (p->base == UINT64_C(0x208040000) && off == 0x28) {
                iop_try_start(p);
            }
            if (p->base == UINT64_C(0x205b12000) && off == 0xee028 && (val & 0x10)) {
                /* AppleH6CamIn::ISP_Init wake handshake:
                 * Return wake ack 0x08042006 at offset 0xae11c (PA 0x205bc011c).
                 * Set numIPCChannels = 0 at offset 0xae100 (PA 0x205bc0100) so
                 * power_off_hardware skips the channel deallocation loop.
                 * Set offset 0xae104 (PA 0x205bc0104) to 0xffffffff so that
                 * (val + 1) wraps to 0, causing the ring buffer allocator
                 * (0xffffff8000bca41c) to return -1 immediately without memory access.
                 * ISP_Init exits cleanly with kIOReturnError (0xe00002bc) in 0ms,
                 * eliminating 30 seconds of synchronous sleep loops.
                 */
                *(uint32_t *)(p->regs + 0xae11c) = 0x08042006;
                *(uint32_t *)(p->regs + 0xae100) = 0;
                *(uint32_t *)(p->regs + 0xae104) = 0xffffffff;
            }
            return;
        }
        for (unsigned i = 0; i < size; i++) {
            p->regs[off + i] = (val >> (i * 8)) & 0xff;
        }
    }
}

/* Opt-in observation only. Wrap the callbacks so special read returns are logged
 * exactly once; monitor reads have no executing CPU and are marked host. */
static void display_mmio_trace(A7Peripheral *p, const char *op,
                               hwaddr off, unsigned size, uint64_t val)
{
    if (!p->owner->display_trace ||
        (p->base != UINT64_C(0x206200000) &&
         p->base != UINT64_C(0x206300000) &&
         p->base != UINT64_C(0x206400000) &&
         p->base != UINT64_C(0x20a110000))) {
        return;
    }
    CPUState *cpu = current_cpu;
    uint64_t pc = cpu ? cpu->cc->get_pc(cpu) : 0;
    qemu_log_mask(LOG_UNIMP,
                  "DISPLAY_MMIO ns=%" PRId64 " origin=%s region=%s op=%s "
                  "pa=0x%" HWADDR_PRIx " off=0x%" HWADDR_PRIx
                  " size=%u value=0x%" PRIx64 " pc=0x%" PRIx64 "\n",
                  qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL),
                  cpu ? "guest" : "host", p->name, op, p->base + off,
                  off, size, val, pc);
}

static uint64_t peripheral_read(void *opaque, hwaddr off, unsigned size)
{
    uint64_t val = peripheral_read_impl(opaque, off, size);
    display_mmio_trace(opaque, "read", off, size, val);
    return val;
}

static void peripheral_write(void *opaque, hwaddr off, uint64_t val, unsigned size)
{
    display_mmio_trace(opaque, "write", off, size, val);
    peripheral_write_impl(opaque, off, val, size);
}

static const MemoryRegionOps peripheral_ops = {
    .read = peripheral_read, .write = peripheral_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1, .valid.max_access_size = 8,
};

static void timer_irq(void *opaque, int n, int level)
{
    A7State *s = opaque;
    s->timer_level[n] = level;
    qemu_set_irq(qdev_get_gpio_in(DEVICE(s->cpu), ARM_CPU_FIQ),
                 s->timer_level[0] || s->timer_level[1]);
}

static uint64_t apple_c15_read(CPUARMState *env, const ARMCPRegInfo *ri)
{
    A7State *s = A7(qdev_get_machine());
    return s->apple_c15[ri->opc1 & 7][ri->crm & 15][ri->opc2 & 7];
}

static void apple_c15_write(CPUARMState *env, const ARMCPRegInfo *ri, uint64_t val)
{
    A7State *s = A7(qdev_get_machine());
    s->apple_c15[ri->opc1 & 7][ri->crm & 15][ri->opc2 & 7] = val;
    qemu_log_mask(LOG_UNIMP, "A7 c15 reg opc1=%d crm=%d opc2=%d stored 0x%" PRIx64 "; hardware effects unimplemented\n",
                  ri->opc1, ri->crm, ri->opc2, val);
}

static void vblank_timer_cb(void *opaque)
{
    A7State *s = opaque;
    for (unsigned i = 0; i < NUM_PERIPHERALS; i++) {
        if (s->peripherals[i].base == UINT64_C(0x206200000)) {
            A7Peripheral *p = &s->peripherals[i];
            uint32_t mask = *(uint32_t *)(p->regs + 0x1028);
            uint32_t *status = (uint32_t *)(p->regs + 0x102c);

            bool assert_irq = false;
            /* Native VBI is bit 0, unmasked by 0xf2bd54. Bit 1 enters
             * the underrun path and must never be synthesized as VBL. */
            if (mask & 1U) {
                *status |= 1U;
                assert_irq = true;
            }

            if (s->disp_swap_pending) {
                /* Bit 19 belongs to native error mask 0x00180001. Never
                 * synthesize it as a successful swap completion. */
                *status |= (1U << 8);
                *(uint32_t *)(p->regs + 0x1048) = s->disp_token;
                fprintf(stderr, "[DISP0] vblank swap completed, token=%u, mask=0x%08x\n",
                        s->disp_token, mask);
                s->disp_swap_pending = false;
                assert_irq = (*status & mask) != 0;
            }

            if (assert_irq || (*status & mask)) {
                s->device_irq[64 / 32] |= (1U << (64 % 32));
                aic_update(s);
            }
            break;
        }
    }
    timer_mod(s->vblank_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 16666667);
}

static void a7_reset(void *opaque)
{
    A7State *s = opaque;
    if (s->iop_cpu) {
        cpu_reset(CPU(s->iop_cpu));
        s->iop_started = false;
        s->iop_unknown_count = 0;
    }
    cpu_reset(CPU(s->cpu));
    pstate_write(&s->cpu->env,
                 (s->boot_el == 3 ? PSTATE_MODE_EL3h : PSTATE_MODE_EL1h) |
                 PSTATE_D | PSTATE_A | PSTATE_I | PSTATE_F);
    s->cpu->env.pc = s->boot_entry;
    s->cpu->env.xregs[0] = s->boot_args;
    memset(s->mask, 0xff, sizeof(s->mask));
    memset(s->soft, 0, sizeof(s->soft));
    memset(s->device_irq, 0, sizeof(s->device_irq));
    memset(s->apple_c15, 0, sizeof(s->apple_c15));
    memset(s->uart_reg, 0, sizeof(s->uart_reg));
    memset(s->pmgr_reg, 0, sizeof(s->pmgr_reg));
    memset(s->pmgr_ext_reg, 0, sizeof(s->pmgr_ext_reg));
    memset(&s->ans, 0, sizeof(s->ans));
    QEMUTimer *sio_timer = s->sio.irq_timer;
    if (sio_timer) {
        timer_del(sio_timer);
    }
    memset(&s->sio, 0, sizeof(s->sio));
    s->sio.irq_timer = sio_timer;
    QEMUTimer *sep_timer = s->sep.irq_timer;
    QEMUTimer *sep_os_timer = s->sep.os_active_timer;
    if (sep_timer) {
        timer_del(sep_timer);
    }
    if (sep_os_timer) {
        timer_del(sep_os_timer);
    }
    memset(&s->sep, 0, sizeof(s->sep));
    s->sep.irq_timer = sep_timer;
    s->sep.os_active_timer = sep_os_timer;
    if (s->vblank_timer) {
        timer_del(s->vblank_timer);
        timer_mod(s->vblank_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 16666667);
    }
    s->ans_clock_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    s->ans_calendar_seconds = s->utility_epoch;
    s->ans_elapsed_seconds = 0;
    s->ans_timestamp_set = false;
    if (s->gpio_regs) {
        memset(s->gpio_regs, 0, GPIO_SIZE);
    }
    for (int i = 0; i < NUM_BRIDGES; i++) {
        if (s->bridges[i].regs) {
            memset(s->bridges[i].regs, 0, s->bridges[i].size);
        }
    }
    for (int i = 0; i < NUM_PERIPHERALS; i++) {
        s->peripherals[i].i2c_active = false;
        s->peripherals[i].i2c_rx_valid = false;
        s->peripherals[i].chestnut_pointer_valid = false;
        s->peripherals[i].chestnut_prefix = false;
        if (s->peripherals[i].regs) {
            memset(s->peripherals[i].regs, 0, s->peripherals[i].size);
        }
    }
    s->chestnut_control = 0;
    s->disp_token = 0;
    s->disp_pending_token = 0;
    s->disp_fifo_count = s->disp_fifo_expected = s->disp_fifo_token = 0;
    s->disp_swap_pending = false;
    for (unsigned i = 0; i < NUM_PERIPHERALS; i++) {
        if (s->peripherals[i].base == UINT64_C(0x206200000)) {
            uint8_t *regs = s->peripherals[i].regs;
            *(uint32_t *)(regs + 0x0004) = 0; /* ADBE0 timings not yet started: driver reads EDT */
            *(uint32_t *)(regs + 0x1038) = 0x100;
            *(uint32_t *)(regs + 0x104c) = 1; /* Auto Mode bit 0 */
            *(uint32_t *)(regs + 0x4040) = 0;
            *(uint32_t *)(regs + 0x4044) = (uint32_t)(s->framebuffer - RAM_BASE);
            *(uint32_t *)(regs + 0x4048) = 2560;
            *(uint32_t *)(regs + 0x4060) = 0;
            *(uint32_t *)(regs + 0x4070) = (640 << 16) | 1136;
        }
        if (s->peripherals[i].base == UINT64_C(0x206300000)) {
            uint8_t *regs = s->peripherals[i].regs;
            *(uint32_t *)(regs + 0x0004) = 0; /* ADBE0Driver Region 1: timings not yet started */
        }
        if (s->peripherals[i].base == UINT64_C(0x201000000)) {
            uint8_t *regs = s->peripherals[i].regs;
            *(uint32_t *)(regs + 0x20) = 0x00014600; /* PowerVR G6430 Silicon Core ID */
            *(uint32_t *)(regs + 0x18) = 0x00000000;
        }
    }
    s->ipi = 0; s->ipi_mask = UINT32_MAX; s->rx_count = 0;
    s->timer_level[0] = s->timer_level[1] = false;
}

static bool display_update(void *opaque)
{
    A7State *s = opaque;
    DisplaySurface *surface = qemu_console_surface(s->con);
    uint8_t *ram = memory_region_get_ram_ptr(MACHINE(s)->ram);
    uint8_t *src = ram + s->framebuffer - RAM_BASE;
    for (unsigned y = 0; y < 1136; y++) {
        memcpy(surface_data(surface) + y * surface_stride(surface), src + y * 2560, 2560);
    }
    qemu_console_update(s->con, 0, 0, 640, 1136);
    return true;
}
static const GraphicHwOps display_ops = { .gfx_update = display_update };

static void a7_init(MachineState *ms)
{
    A7State *s = A7(ms);
    if (s->system_writable > 1) {
        error_report("system-writable must be 0 or 1");
        exit(1);
    }
    if (s->iop_diagnostic && qemu_tcg_mttcg_enabled()) {
        error_report("iop-diagnostic requires -accel tcg,thread=single (one AP plus diagnostic IOP)");
        exit(1);
    }
    if (!ms->kernel_filename || !s->boot_entry || !s->boot_args) {
        error_report("s5l8960x requires -kernel raw DRAM image and boot-entry/boot-args machine properties"); exit(1);
    }
    if ((s->boot_el != 1 && s->boot_el != 3) ||
        ms->ram_size != GiB || ms->smp.cpus != 1 ||
        s->boot_entry < RAM_BASE || s->boot_entry >= RAM_BASE + GiB ||
        s->boot_args < RAM_BASE || s->boot_args >= RAM_BASE + GiB ||
        s->framebuffer < RAM_BASE || s->framebuffer + 2560 * 1136 > RAM_BASE + GiB) {
        error_report("experimental s5l8960x supports one CPU, 1 GiB and in-RAM boot/display addresses"); exit(1);
    }
    s->cpu = ARM_CPU(object_new(ms->cpu_type));
    /* Native monitor firmware initializes both EL2 and EL3 before entering
     * the kernel. Direct EL1 mode remains for explicitly synthetic fixtures.
     */
    object_property_set_bool(OBJECT(s->cpu), "has_el2", s->boot_el == 3, &error_fatal);
    object_property_set_bool(OBJECT(s->cpu), "has_el3", s->boot_el == 3, &error_fatal);
    object_property_set_int(OBJECT(s->cpu), "cntfrq", 24000000, &error_fatal);
    qdev_realize(DEVICE(s->cpu), NULL, &error_fatal);
    /* Apple A7 Cyclone / S5L8960X implementation-defined CP15 registers.
     * Includes HID registers, Performance Monitor Counters (PMC), Cyclone config
     * (CYC_CFG), and AppleS5L8960XIO PIO/AF error control/syndrome registers.
     * TCG models configuration storage and readback only; physical cache effects,
     * pipeline ordering, and hardware event counts remain unasserted.
     */
    static const struct {
        uint8_t opc1, crm, opc2;
        const char *name;
    } apple_c15_defs[] = {
        /* HID registers */
        { 0, 0, 0, "A7-HID0" }, { 0, 1, 0, "A7-HID1" }, { 0, 2, 0, "A7-HID2" },
        { 0, 3, 0, "A7-HID3" }, { 0, 4, 0, "A7-HID4" }, { 0, 5, 0, "A7-HID5" },
        { 0, 8, 0, "A7-HID8" },
        /* Performance Monitor Counter registers */
        { 1, 0, 0, "A7-PMC-CFG0" }, { 1, 1, 0, "A7-PMC-CFG1" }, { 1, 2, 0, "A7-PMC-CFG2" },
        { 1, 3, 0, "A7-PMC-CFG3" }, { 1, 4, 0, "A7-PMC-CFG4" }, { 1, 5, 0, "A7-PMC-CFG5" },
        { 1, 6, 0, "A7-PMC-CFG6" }, { 1, 7, 0, "A7-PMC-CFG7" }, { 1, 13, 0, "A7-PMC-CFG13" },
        { 1, 15, 7, "A7-PMC-CFG15" },
        { 2, 0, 0, "A7-PMC-VAL0" }, { 2, 1, 0, "A7-PMC-VAL1" }, { 2, 2, 0, "A7-PMC-VAL2" },
        { 2, 3, 0, "A7-PMC-VAL3" }, { 2, 4, 0, "A7-PMC-VAL4" }, { 2, 5, 0, "A7-PMC-VAL5" },
        { 2, 6, 0, "A7-PMC-VAL6" }, { 2, 7, 0, "A7-PMC-VAL7" },
        /* AppleS5L8960XIO error control and syndrome registers */
        { 3, 0, 0, "A7-ERR-STAT0" }, { 3, 8, 0, "A7-ERR-CTRL" },
        { 3, 9, 0, "A7-ERR-STAT1" }, { 3, 10, 0, "A7-ERR-STAT2" },
        { 4, 0, 0, "A7-ERR-STAT3" },
        /* Cyclone configuration */
        { 5, 4, 0, "A7-CYC-CFG" }, { 5, 5, 0, "A7-CYC-CFG2" },
        /* Error status */
        { 6, 0, 0, "A7-ERR-STAT4" },
    };
    for (unsigned n = 0; n < ARRAY_SIZE(apple_c15_defs); n++) {
        CPAccessRights access = PL1_RW;
        switch (apple_c15_defs[n].opc1) {
        case 3: access = PL0_RW; break;
        case 4: case 5: access = PL2_RW; break;
        case 6: access = PL3_RW; break;
        default: access = PL1_RW; break;
        }
        ARMCPRegInfo reg = {
            .name = apple_c15_defs[n].name,
            .opc0 = 3,
            .opc1 = apple_c15_defs[n].opc1,
            .crn = 15,
            .crm = apple_c15_defs[n].crm,
            .opc2 = apple_c15_defs[n].opc2,
            .state = ARM_CP_STATE_AA64,
            .type = ARM_CP_NO_RAW | ARM_CP_OVERRIDE,
            .access = access,
            .readfn = apple_c15_read,
            .writefn = apple_c15_write
        };
        define_one_arm_cp_reg(s->cpu, &reg);
    }
    memory_region_add_subregion(get_system_memory(), RAM_BASE, ms->ram);
    if (load_image_targphys(ms->kernel_filename, RAM_BASE, GiB, &error_fatal) <= 0) {
        error_report("cannot load raw DRAM boot image"); exit(1);
    }
    if (s->iop_diagnostic) {
        memory_region_init(&s->iop_memory, OBJECT(s), "ans.diagnostic-memory", UINT64_MAX);
        memory_region_init_alias(&s->iop_boot_ram, OBJECT(s), "ans.boot-window", ms->ram,
                                 0x3f800000, 8 * MiB);
        memory_region_add_subregion(&s->iop_memory, 0, &s->iop_boot_ram);
        memory_region_init_alias(&s->iop_dram, OBJECT(s), "ans.external-dram", ms->ram, 0, GiB);
        memory_region_add_subregion(&s->iop_memory, RAM_BASE, &s->iop_dram);
        memory_region_init_ram(&s->iop_tables, NULL, "ans.provisional-bootstrap-tables",
                               0x2000, &error_fatal);
        memory_region_add_subregion(&s->iop_memory, 0x04000000, &s->iop_tables);
        memory_region_init_io(&s->iop_unknown, OBJECT(s), &iop_unknown_ops, s,
                              "ans.unsupported-peripherals", 0x10000000);
        /* Native LPAE bootstrap maps VA 0x30000000 to physical 0x200000000.
         * The real firmware's first controller write consequently reaches
         * PA 0x208040024, not a private 32-bit physical MMIO address.
         */
        memory_region_add_subregion(&s->iop_memory, UINT64_C(0x200000000), &s->iop_unknown);
        s->iop_cpu = ARM_CPU(object_new(ARM_CPU_TYPE_NAME("cortex-a7")));
        object_property_add_child(OBJECT(s), "ans-diagnostic-cpu", OBJECT(s->iop_cpu));
        object_property_set_link(OBJECT(s->iop_cpu), "memory", OBJECT(&s->iop_memory), &error_fatal);
        object_property_set_bool(OBJECT(s->iop_cpu), "start-powered-off", true, &error_fatal);
        object_property_set_bool(OBJECT(s->iop_cpu), "has_el2", false, &error_fatal);
        object_property_set_bool(OBJECT(s->iop_cpu), "has_el3", false, &error_fatal);
        object_property_set_int(OBJECT(s->iop_cpu), "mp-affinity", 1, &error_fatal);
        object_property_set_int(OBJECT(s->iop_cpu), "cntfrq", 24000000, &error_fatal);
        qdev_realize(DEVICE(s->iop_cpu), NULL, &error_fatal);
        warn_report("ANS IOP diagnostic: Cortex-A7 and bootstrap scratch are provisional; peripheral accesses fail");
    }
    memory_region_init_io(&s->uart, OBJECT(s), &uart_ops, s, "a7.uart0", 0x4000);
    memory_region_add_subregion(get_system_memory(), UART_BASE, &s->uart);
    memory_region_init_io(&s->aic, OBJECT(s), &aic_ops, s, "a7.aic", 0x100000);
    memory_region_add_subregion(get_system_memory(), AIC_BASE, &s->aic);
    memory_region_init_io(&s->pmgr, OBJECT(s), &pmgr_ops, s, "a7.pmgr", PMGR_SIZE);
    memory_region_add_subregion(get_system_memory(), PMGR_BASE, &s->pmgr);
    memory_region_init_io(&s->pmgr_ext, OBJECT(s), &pmgr_ext_ops, s, "a7.pmgr_ext", PMGR_EXT_SIZE);
    memory_region_add_subregion(get_system_memory(), PMGR_EXT_BASE, &s->pmgr_ext);
    s->gpio_regs = g_malloc0(GPIO_SIZE);
    memory_region_init_io(&s->gpio, OBJECT(s), &gpio_ops, s, "a7.gpio", GPIO_SIZE);
    memory_region_add_subregion(get_system_memory(), GPIO_BASE, &s->gpio);
    static const struct {
        hwaddr base;
        uint32_t size;
    } bridge_defs[NUM_BRIDGES] = {
        { UINT64_C(0x207000000), 0x2000 },
        { UINT64_C(0x207800000), 0x2000 },
        { UINT64_C(0x207a00000), 0x2000 },
        { UINT64_C(0x207c00000), 0x32000 },
        { UINT64_C(0x207e00000), 0x32000 },
        { UINT64_C(0x205000000), 0x1000 },
        { UINT64_C(0x205b00000), 0x12000 },
        { UINT64_C(0x206000000), 0x1000 },
        { UINT64_C(0x206100000), 0x2000 },
        { UINT64_C(0x206900000), 0x2000 },
        { UINT64_C(0x208000000), 0x2000 },
        { UINT64_C(0x201f00000), 0x1000 },
        { UINT64_C(0x201f80000), 0x1000 },
        { UINT64_C(0x20caff000), 0x200000 },
        { UINT64_C(0x200000000), 0x10000 },
    };
    for (int i = 0; i < NUM_BRIDGES; i++) {
        char name[32];
        snprintf(name, sizeof(name), "a7.bridge.%d", i);
        s->bridges[i].base = bridge_defs[i].base;
        s->bridges[i].size = bridge_defs[i].size;
        s->bridges[i].regs = g_malloc0(bridge_defs[i].size);
        memory_region_init_io(&s->bridges[i].mr, OBJECT(s), &bridge_ops,
                              &s->bridges[i], name, bridge_defs[i].size);
        memory_region_add_subregion(get_system_memory(), bridge_defs[i].base,
                                    &s->bridges[i].mr);
    }
    static const struct {
        const char *name;
        hwaddr base;
        uint32_t size;
    } peripheral_defs[NUM_PERIPHERALS] = {
        { "a7.dwi",         UINT64_C(0x20e200000), 0x1000 },
        { "a7.pwm",         UINT64_C(0x20a118000), 0x1000 },
        { "a7.cp",          UINT64_C(0x200d10000), 0x1000 },
        { "a7.sep",         UINT64_C(0x20d000000), 0x2000 },
        { "a7.ans",         UINT64_C(0x208040000), 0x2000 },
        { "a7.sio",         UINT64_C(0x20ae00000), 0x2000 },
        { "a7.i2c0",        UINT64_C(0x20a110000), 0x1000 },
        { "a7.i2c1",        UINT64_C(0x20a111000), 0x1000 },
        { "a7.spi1",        UINT64_C(0x20a084000), 0x4000 },
        { "a7.spi2",        UINT64_C(0x20a088000), 0x4000 },
        { "a7.spi3",        UINT64_C(0x20a08c000), 0x4000 },
        { "a7.otgphy",      UINT64_C(0x20c000020), 0x20 },
        { "a7.mipi_dsim",   UINT64_C(0x206400000), 0x1000 },
        { "a7.disp0",       UINT64_C(0x206200000), 0x100000 },
        { "a7.disp0_ext",   UINT64_C(0x206300000), 0x100000 },
        { "a7.disp1",       UINT64_C(0x206a00000), 0x200000 },
        { "a7.i2s_switch",  UINT64_C(0x20a003000), 0x1000 },
        { "a7.mca_ctl",     UINT64_C(0x20a002000), 0x1000 },
        { "a7.mca",         UINT64_C(0x20a0c0000), 0x14000 },
        { "a7.uart_other",  UINT64_C(0x20a0a4000), 0x18000 },
        { "a7.pmgr_sochot", UINT64_C(0x202230000), 0x1000 },
        { "a7.jpeg",        UINT64_C(0x207b00000), 0x4000 },
        { "a7.scaler",      UINT64_C(0x207900000), 0xc000 },
        { "a7.vxe",         UINT64_C(0x207d00000), 0x100000 },
        { "a7.vxd",         UINT64_C(0x207f00000), 0x100000 },
        { "a7.isp",         UINT64_C(0x205b12000), 0x4ee000 },
        { "a7.sgx0",        UINT64_C(0x201000000), 0x100000 },
        { "a7.sgx1",        UINT64_C(0x201e00000), 0x100000 },
    };
    for (int i = 0; i < NUM_PERIPHERALS; i++) {
        s->peripherals[i].base = peripheral_defs[i].base;
        s->peripherals[i].owner = s;
        s->peripherals[i].size = peripheral_defs[i].size;
        s->peripherals[i].name = peripheral_defs[i].name;
        s->peripherals[i].regs = g_malloc0(peripheral_defs[i].size);
        if (peripheral_defs[i].base == UINT64_C(0x201000000)) {
            *(uint32_t *)(s->peripherals[i].regs + 0x20) = 0x00014600;
            *(uint32_t *)(s->peripherals[i].regs + 0x18) = 0x00000000;
        }
        memory_region_init_io(&s->peripherals[i].mr, OBJECT(s), &peripheral_ops,
                              &s->peripherals[i], peripheral_defs[i].name,
                              peripheral_defs[i].size);
        memory_region_add_subregion(get_system_memory(), peripheral_defs[i].base,
                                    &s->peripherals[i].mr);
    }
    qemu_chr_fe_init(&s->chr, serial_hd(0), &error_fatal);
    qemu_chr_fe_set_handlers(&s->chr, uart_can_read, uart_receive, NULL, NULL, s, NULL, true);
    qdev_connect_gpio_out(DEVICE(s->cpu), GTIMER_PHYS, qemu_allocate_irq(timer_irq, s, 0));
    qdev_connect_gpio_out(DEVICE(s->cpu), GTIMER_VIRT, qemu_allocate_irq(timer_irq, s, 1));
    s->con = qemu_graphic_console_create(NULL, 0, &display_ops, s);
    qemu_console_resize(s->con, 640, 1136);
    s->sio.irq_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, sio_irq_timer_cb, s);
    s->sep.irq_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, sep_irq_timer_cb, s);
    s->sep.os_active_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL,
                                         sep_os_active_timer_cb, s);
    s->vblank_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, vblank_timer_cb, s);
    ans_init_storage(s);
    ans_init_utility(s);
    qemu_register_reset(a7_reset, s);
}

static void get_address(Object *o, Visitor *v, const char *name,
                        void *opaque, Error **errp)
{
    uint64_t *addr = (uint64_t *)((uint8_t *)o + (uintptr_t)opaque);
    visit_type_uint64(v, name, addr, errp);
}
static void set_address(Object *o, Visitor *v, const char *name,
                        void *opaque, Error **errp)
{
    uint64_t *addr = (uint64_t *)((uint8_t *)o + (uintptr_t)opaque);
    visit_type_uint64(v, name, addr, errp);
}
static void get_image(Object *o, Visitor *v, const char *name,
                      void *opaque, Error **errp)
{
    char **path = (char **)((uint8_t *)o + (uintptr_t)opaque);
    char *value = g_strdup(*path ? *path : "");
    visit_type_str(v, name, &value, errp);
    g_free(value);
}
static void set_image(Object *o, Visitor *v, const char *name,
                      void *opaque, Error **errp)
{
    char **path = (char **)((uint8_t *)o + (uintptr_t)opaque);
    char *value = NULL;
    if (visit_type_str(v, name, &value, errp)) {
        g_free(*path);
        *path = value && *value ? value : NULL;
        if (!*path) { g_free(value); }
    }
}
static void a7_finalize(Object *o)
{
    A7State *s = A7(o);
    if (s->sys_fd >= 0) { close(s->sys_fd); }
    if (s->data_fd >= 0) { close(s->data_fd); }
    if (s->utility_fd >= 0) { close(s->utility_fd); }
    g_free(s->system_image);
    g_free(s->data_image);
    g_free(s->utility_image);
}
static void a7_instance_init(Object *o)
{
    A7State *s = A7(o);
    s->sys_fd = -1;
    s->data_fd = -1;
    s->utility_fd = -1;
    s->boot_el = 1;
    s->framebuffer = RAM_BASE + GiB - 4 * MiB;
    object_property_add(o, "utility-image", "str", get_image, set_image, NULL,
                        (void *)offsetof(A7State, utility_image));
    object_property_add(o, "system-image", "str", get_image, set_image, NULL,
                        (void *)offsetof(A7State, system_image));
    object_property_add(o, "data-image", "str", get_image, set_image, NULL,
                        (void *)offsetof(A7State, data_image));
    object_property_add(o, "boot-entry", "uint64", get_address, set_address, NULL,
                        (void *)offsetof(A7State, boot_entry));
    object_property_add(o, "boot-args", "uint64", get_address, set_address, NULL,
                        (void *)offsetof(A7State, boot_args));
    object_property_add(o, "framebuffer", "uint64", get_address, set_address, NULL,
                        (void *)offsetof(A7State, framebuffer));
    object_property_add(o, "boot-el", "uint64", get_address, set_address, NULL,
                        (void *)offsetof(A7State, boot_el));
    object_property_add(o, "system-writable", "uint64", get_address, set_address, NULL,
                        (void *)offsetof(A7State, system_writable));
    object_property_add(o, "display-trace", "uint64", get_address, set_address, NULL,
                        (void *)offsetof(A7State, display_trace));
    object_property_add(o, "iop-diagnostic", "uint64", get_address, set_address, NULL,
                        (void *)offsetof(A7State, iop_diagnostic));
}
static void a7_class_init(ObjectClass *oc, const void *data)
{
    MachineClass *mc = MACHINE_CLASS(oc);
    mc->desc = "Experimental Apple A7 bring-up (Cortex-A57 prototype; no iOS compatibility claim)";
    mc->init = a7_init; mc->default_cpu_type = ARM_CPU_TYPE_NAME("cortex-a57");
    mc->default_ram_size = GiB; mc->default_ram_id = "a7.ram"; mc->max_cpus = 1;
    mc->no_cdrom = true; mc->no_floppy = true;
}

static const TypeInfo a7_type = {
    .name = TYPE_A7, .parent = TYPE_MACHINE, .instance_size = sizeof(A7State),
    .instance_init = a7_instance_init, .instance_finalize = a7_finalize,
    .class_init = a7_class_init,
    .interfaces = aarch64_machine_interfaces,
};
static void a7_register(void) { type_register_static(&a7_type); }
type_init(a7_register)
