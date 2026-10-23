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
#include "qemu/thread.h"
#include "qemu/rcu.h"
#include "qemu/main-loop.h"
#include "qapi/error.h"
#include "qapi/visitor.h"
#include "hw/core/boards.h"
#include "hw/core/loader.h"
#include "hw/core/irq.h"
#include "hw/core/sysbus.h"
#include "hw/core/qdev-properties.h"
#include "hw/usb/hcd-ehci.h"
#include "hw/usb/hcd-ohci.h"
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
#include "ui/input.h"
#include <sys/file.h>
#include <math.h>

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
#define NUM_BRIDGES 18
#define NUM_PERIPHERALS 28

/* Bounded, host-testable script compiler. Times are relative to reset. */
#define TOUCH_SCRIPT_MAX_FRAMES 16384
#define TOUCH_TICK_NS INT64_C(16666667)
typedef struct TouchScriptFrame {
    int64_t ns;
    int x, y;
    unsigned state;
} TouchScriptFrame;
typedef struct TouchScript {
    TouchScriptFrame frames[TOUCH_SCRIPT_MAX_FRAMES];
    unsigned count;
} TouchScript;

static void touch_skip_space(const char **p)
{
    while (**p == ' ' || **p == '\t' || **p == '\n' || **p == '\r') { (*p)++; }
}

static bool touch_number(const char **p, double *value, double lo, double hi)
{
    char *end;
    touch_skip_space(p);
    *value = strtod(*p, &end);
    if (end == *p || !isfinite(*value) || *value < lo || *value > hi) {
        return false;
    }
    *p = end;
    touch_skip_space(p);
    return true;
}

static bool touch_separator(const char **p, char separator)
{
    if (**p != separator) { return false; }
    (*p)++;
    return true;
}

static bool touch_xy(const char **p, int *x, int *y)
{
    double a, b;
    if (!touch_number(p, &a, 0, 640) || !touch_separator(p, ',') ||
        !touch_number(p, &b, 0, 1136) || a != (int)a || b != (int)b) {
        return false;
    }
    *x = a; *y = b;
    return true;
}

static bool touch_script_frame(TouchScript *out, int64_t ns, unsigned state,
                               int x, int y)
{
    if (out->count == TOUCH_SCRIPT_MAX_FRAMES) { return false; }
    out->frames[out->count++] = (TouchScriptFrame) { ns, x, y, state };
    return true;
}

static bool touch_script_segment(TouchScript *out, int64_t *ns, int *x, int *y,
                                 int to_x, int to_y, double duration, unsigned n)
{
    int64_t span = duration * 1000000000;
    for (unsigned i = 1; i <= n; i++) {
        if (!touch_script_frame(out, *ns + span * i / n, 4,
                                *x + (to_x - *x) * (int)i / (int)n,
                                *y + (to_y - *y) * (int)i / (int)n)) {
            return false;
        }
    }
    *ns += span; *x = to_x; *y = to_y;
    return true;
}

/* On any error count is zero: no prefix of an invalid script can run. */
static bool touch_script_parse(const char *script, TouchScript *out,
                               char *error, size_t error_size)
{
    const char *p = script, *why = "invalid command";
    unsigned gesture = 1, command = 0;
    int x = 0, y = 0;
    int64_t ns = 0;
    double start, duration;
    out->count = 0;
    touch_skip_space(&p);
    if (strncmp(p, "at=", 3)) {
        /* Historical six-field form, including its floor(duration*60). */
        int x1, y1;
        int64_t interval, first;
        why = "expected start_s,x0,y0,x1,y1,duration_s or at=seconds;down x,y;...;up";
        if (!touch_number(&p, &start, 0, 86400) || !touch_separator(&p, ',') ||
            !touch_xy(&p, &x, &y) || !touch_separator(&p, ',') ||
            !touch_xy(&p, &x1, &y1) || !touch_separator(&p, ',') ||
            !touch_number(&p, &duration, 0.016, 60) || *p) { goto fail; }
        unsigned n = MAX(1, (unsigned)(duration * 60));
        ns = start * 1000000000;
        first = ns;
        interval = (int64_t)(duration * 1000000000) / n;
        touch_script_frame(out, ns, 1, x, y);
        ns += interval;
        touch_script_frame(out, ns, 3, x, y);
        touch_script_segment(out, &ns, &x, &y, x1, y1, duration, n);
        touch_script_frame(out, ns + interval, 7, x, y);
        /* Preserve the legacy timer's integer interval truncation exactly. */
        for (unsigned i = 1; i < out->count; i++) {
            out->frames[i].ns = first + i * interval;
        }
        return true;
    }
    for (;;) {
        bool active = false, completed = false;
        command = 0;
        why = "each gesture needs at=seconds (0..86400), after the previous up";
        if (strncmp(p, "at=", 3)) { goto fail; }
        p += 3;
        if (!touch_number(&p, &start, 0, 86400)) { goto fail; }
        ns = start * 1000000000;
        if (out->count && ns <= out->frames[out->count - 1].ns) { goto fail; }
        if (!touch_separator(&p, ';')) { goto fail; }
        for (;;) {
            touch_skip_space(&p);
            command++;
            why = "expected down x,y, move x,y dur=seconds [steps=N], hold seconds, up, or tap x,y";
            bool tap = !strncmp(p, "tap ", 4);
            if (tap || !strncmp(p, "down ", 5)) {
                if (active || completed) { why = "only one down/tap per gesture"; goto fail; }
                p += tap ? 4 : 5;
                if (!touch_xy(&p, &x, &y)) { why = "coordinates must be integer pixels x=0..640,y=0..1136"; goto fail; }
                if (!touch_script_frame(out, ns, 1, x, y) ||
                    !touch_script_frame(out, ns += TOUCH_TICK_NS, 3, x, y)) { goto full; }
                active = true;
                if (tap) {
                    if (!touch_script_segment(out, &ns, &x, &y, x, y, 0.1, 6) ||
                        !touch_script_frame(out, ns += TOUCH_TICK_NS, 7, x, y)) { goto full; }
                    active = false; completed = true;
                }
            } else if (!strncmp(p, "move ", 5) || !strncmp(p, "hold ", 5)) {
                bool move = !strncmp(p, "move ", 5);
                int to_x = x, to_y = y;
                p += 5;
                if (!active) { why = "move/hold requires an active down"; goto fail; }
                if (move) {
                    if (!touch_xy(&p, &to_x, &to_y) || strncmp(p, "dur=", 4)) {
                        why = "move requires x,y dur=seconds [steps=N]"; goto fail;
                    }
                    p += 4;
                }
                if (!touch_number(&p, &duration, 0.001, 60)) {
                    why = "duration must be finite seconds in 0.001..60"; goto fail;
                }
                unsigned n = MAX(1, (unsigned)(duration * 60));
                if (move && !strncmp(p, "steps=", 6)) {
                    double steps;
                    p += 6;
                    if (!touch_number(&p, &steps, 1, 3600) || steps != (unsigned)steps) {
                        why = "steps must be an integer in 1..3600"; goto fail;
                    }
                    n = steps;
                }
                if (!touch_script_segment(out, &ns, &x, &y, to_x, to_y, duration, n)) { goto full; }
            } else if (!strncmp(p, "up", 2)) {
                p += 2;
                if (!active) { why = "up requires an active down"; goto fail; }
                if (!touch_script_frame(out, ns += TOUCH_TICK_NS, 7, x, y)) { goto full; }
                active = false; completed = true;
            } else { goto fail; }
            touch_skip_space(&p);
            if (*p == ';') { p++; continue; }
            if (*p && *p != '|') { why = "unexpected trailing text; commands use ';', gestures use '|'"; goto fail; }
            if (active || !completed) { why = "gesture must end with up (tap supplies up)"; goto fail; }
            break;
        }
        if (!*p) { return true; }
        p++; touch_skip_space(&p); gesture++;
    }
full:
    why = "script exceeds 16384 frames";
fail:
    snprintf(error, error_size, "gesture %u command %u near '%.48s': %s",
             gesture, command, p, why);
    out->count = 0;
    return false;
}

/* Host-testable mouse contact sequencer. Input packets commit at sync. */
typedef struct MouseTouch {
    int host_x, host_y, x, y, start_x, start_y;
    bool button, last_button, pressed, dirty;
    unsigned phase; /* 0 idle, 1 start, 2 down, 3 drag/final move, 4 up */
    unsigned release_frames;
    int64_t due;
} MouseTouch;

static int mouse_touch_scale(int value, int maximum, int pixels)
{
    value = MAX(0, MIN(value, maximum));
    return (int)((int64_t)value * pixels / maximum);
}

static void mouse_touch_sync(MouseTouch *m, bool allowed, int64_t now)
{
    if (m->button && !m->last_button && !m->phase && allowed) {
        m->x = m->start_x = m->host_x;
        m->y = m->start_y = m->host_y;
        m->phase = 1;
        m->pressed = true;
        m->dirty = false;
        m->release_frames = 0;
        m->due = now;
    } else if (m->phase && m->pressed) {
        m->dirty |= m->x != m->host_x || m->y != m->host_y;
        m->x = m->host_x;
        m->y = m->host_y;
    }
    if (!m->button && m->pressed) {
        m->pressed = false;
        int dx = m->x - m->start_x;
        int dy = m->y - m->start_y;
        if (dx < 0) { dx = -dx; }
        if (dy < 0) { dy = -dy; }
        m->release_frames = (dx > 10 || dy > 10) ? 18 : 3;
    }
    /* Ignored presses require a release and a fresh press. */
    m->last_button = m->button;
}

static bool mouse_touch_next(MouseTouch *m, int64_t now, bool ready,
                             TouchScriptFrame *f)
{
    if (!m->phase || now < m->due || !ready) { return false; }
    f->ns = now;
    f->x = m->x; f->y = m->y;
    if (m->phase <= 2) {
        f->state = m->phase == 1 ? 1 : 3;
        f->x = m->start_x; f->y = m->start_y;
        m->phase++;
    } else if (m->phase == 3) {
        f->state = 4;
        m->dirty = false;
        if (!m->pressed) {
            if (m->release_frames > 0) {
                m->release_frames--;
            } else {
                m->phase = 4;
            }
        }
    } else {
        f->state = 7;
        m->phase = 0;
    }
    m->due = now + TOUCH_TICK_NS;
    return true;
}

typedef struct A7Bridge {
    hwaddr base;
    uint32_t size;
    uint8_t *regs;
    MemoryRegion mr;
} A7Bridge;

/* Samsung SPI v1 PIO contract, KernelMemory.bin (11D257):
 * start b65e48..94 selects v1: FIFO capacity 16 (NOT C9's 32 words).
 * All addresses below have prefix 0xffffff8000. readRegister b66d94 /
 * writeRegister b66da0 are ordinary 32-bit accesses. CH_CFG +00 enables
 * channel; +04 is mode/IRQ enable (PIO bit5, DMA bit6); +08 is status/W1C,
 * NOT configuration RAM. +10 pushes ONE byte even for a 32-bit store;
 * +20 pops ONE byte. +34 is max(txlen,rxlen), +4c the v1 RX count
 * (b666c0..b66794). +30/+38 are clock/CS timing; +0c controls CS bit1.
 * transferInterrupt b66ab0 snapshots +08: TX occupancy [10:6] (b66d14),
 * TX free=16-occupancy (b66d2c), RX occupancy [15:11] (b66d54).
 * It drains RX, refills TX (0xff padding for rxlen>txlen), writes the
 * snapshot back to +08 (b66bdc), and repeats until software TX/RX/discard
 * counts reach zero (b66bf4..c08). There is NO done-bit completion test.
 * It then disables TX IRQ, retaining bit21 in v1 (b66c0c..40); the action
 * b66c58 masks IRQs, clears +08 with 0x40000f, and wakes the waiter.
 * IRQ mapping below is the minimal v1 hardware interpretation: RX-ready
 * +04 bit7, TX-ready bit8, RX-count-done bit21 / status bit22. The driver
 * doesn't decode these cause bits, so the mapping beyond FIFO fields is
 * an emulation assumption, not proof from a hardware manual.
 * Shifting is synchronous with backpressure, not timed. SmartIO DMA below
 * consumes the same slave byte stream when mode bit6 is selected.
 */
#define SPI_FIFO_SIZE 16
#define SPI_PACKET_SIZE 0x10010

typedef struct A7Touch {
    uint8_t tx[SPI_PACKET_SIZE], reply[SPI_PACKET_SIZE];
    uint32_t read_value, address[32], value[32];
    unsigned memory_count;
    bool read_pending;
    uint8_t mode, ack_status;
    uint16_t boot_status;
    uint32_t firmware_bytes, firmware_checksum;
    uint8_t firmware[SPI_PACKET_SIZE];
    uint32_t firmware_length; /* latest accepted image/block, bounded */
    bool booted;
    A7State *owner;
    uint8_t reporting, sequence;
    uint8_t input_control[4], face_detection, input_reports;
    uint8_t frames[64][56], frame_len[64];
    unsigned frame_head, frame_count;
    uint32_t last_timestamp_us;
} A7Touch;

typedef struct A7SPI {
    uint8_t tx[SPI_FIFO_SIZE], rx[SPI_FIFO_SIZE];
    unsigned tx_head, tx_count, rx_head, rx_count;
    uint32_t shifted, total, causes;
    A7Touch touch;
} A7SPI;

/* Dialog D2045: native 16-bit register space, not a silicon simulation. */
typedef struct A7D2045 {
    uint8_t regs[65536];
    uint32_t rtc_base;
    int64_t rtc_start_ns;
    QEMUTimer *adc_timer;
    bool irq_low;
} A7D2045;

/* Byte-addressed board peripherals on I2C0 (CBTL1610 and LM3534). */
typedef struct A7RegisterI2C {
    uint8_t regs[16], pointer;
    bool prefix, pointer_valid;
} A7RegisterI2C;

typedef struct A7Peripheral {
    A7State *owner;
    const char *name;
    hwaddr base;
    uint32_t size;
    uint8_t *regs;
    MemoryRegion mr;
    A7SPI spi;
    bool i2c_active, i2c_read;
    uint16_t pmu_pointer;
    unsigned pmu_prefix_count, pmu_rx_head, pmu_rx_count;
    bool pmu_pointer_valid;
    uint8_t pmu_rx[255];
    A7RegisterI2C tristar, lm3534;
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

typedef struct SioDMA {
    uint32_t handle, length;
    uint8_t tag;
    bool configured, active;
} SioDMA;

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
    SioDMA spi_dma[4]; /* DT SPI1 22/23, SPI2 24/25 (TX/RX). */
    /* Other AppleSmartIODMA endpoints (e.g. UART2_RX 65): submissions that
     * have no modelled data source stay armed until a stop. */
    uint8_t dma_pending[0x50][8];
    uint8_t dma_pending_count[0x50];
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
    bool os_active_acked;           /* AP sent post-boot traffic after OS-active */
    uint32_t os_active_retries;
    QEMUTimer *os_active_timer;
    uint32_t params[256];
    uint32_t send_ool_page[256];
    uint32_t recv_ool_page[256];
    uint8_t keybag_blob[1024];
    uint32_t keybag_blob_len;
} SepMailbox;

typedef struct AGXReplaySnapshot AGXReplaySnapshot;

struct A7State {
    MachineState parent_obj;
    ARMCPU *cpu; /* cpu0 alias for boot setup */
    ARMCPU *ap_cpu[2];
    ARMCPU *iop_cpu;
    MemoryRegion iop_memory, iop_boot_ram, iop_dram, iop_tables, iop_unknown;
    MemoryRegion uart, aic, pmgr, pmgr_ext, gpio;
    MemoryRegion cpu_mmio[2];
    uint8_t cpu_regs[2][0x1000];
    A7Bridge bridges[NUM_BRIDGES];
    MemoryRegion usb_dma_memory, usb_dma_ram;
    AddressSpace usb_dma_as;
    MemoryRegion usb_otg;
    uint32_t usb_regs[0x10000 / 4];
    A7Bridge usb_aux[4];
    A7Peripheral peripherals[NUM_PERIPHERALS];
    CharFrontend chr;
    QemuConsole *con;
    uint64_t boot_entry, boot_args, framebuffer, boot_el, iop_diagnostic, system_writable, display_trace;
    uint64_t disp_mmu_table;          /* DPU0MMUPGADDR0 << 4, 0 = linear scanout */
    uint32_t disp_stride, disp_offset;
    bool iop_started, secondary_started;
    unsigned iop_unknown_count;
    uint64_t apple_c15[2][8][16][8]; /* Storage only: hardware effects are NOT emulated. */
    uint32_t uart_reg[16], mask[32], soft[32], device_irq[32], targets[NIRQ];
    uint32_t pmgr_reg[PMGR_SIZE / 4];
    uint32_t pmgr_ext_reg[PMGR_EXT_SIZE / 4];
    uint8_t *gpio_regs;
    bool touch_irq_low;
    QEMUTimer *touch_timer;
    QEMUTimer *mouse_touch_timer;
    MouseTouch mouse_touch;
    bool mouse_touch_enabled;
    TouchScript touch_script;
    int64_t touch_epoch_ns;
    bool touch_force;
    unsigned touch_step;
    int touch_settle_frames;
    uint8_t rx[256];
    unsigned rx_count;
    uint32_t ipi[2], ipi_mask[2];
    bool timer_level[2][2];
    QEMUTimer *vblank_timer;
    QEMUTimer *agx_meta_timer;
    QEMUTimer *agx_kick_timer;
    uint64_t agx_cat_base;
    uint64_t agx_ctx_cat[16]; /* Hardware slots: context_id + 3. */
    uint8_t agx_last_ta[16][0x280];
    uint32_t agx_last_ta_queue[16];
    /* Recent TA commands per context: a 3D command pairs with the TA whose
     * +0x14 parameter-buffer address equals its own +0x10 (TA for the next
     * frame is often consumed before the current 3D pass). */
    /* TA queues can run ahead of 3D by a complete native command batch.
     * Eight entries evicted clock/Home passes before their renders arrived. */
    uint8_t agx_ta_ring[16][256][0x280];
    uint8_t agx_ta_ring_next[16];
    AGXReplaySnapshot *agx_ta_snapshot[16][256];
    bool agx_last_ta_valid[16];
    unsigned agx_frame_dump_count;
    uint64_t agx_frame_dump_seen;     /* qualifying frames seen, for SKIP/EVERY */
    uint64_t agx_shared_gva;
    uint32_t agx_meta_alive_count;
    uint32_t agx_queues[32];
    unsigned agx_queue_count;
    uint32_t agx_pending_doorbells;
    /* Dedicated off-BQL AGX replay render thread state */
    QemuThread agx_render_thread;
    QemuMutex agx_render_mutex;
    QemuCond agx_render_cond;
    struct AGXRenderJob *agx_job_head;
    struct AGXRenderJob *agx_job_tail;
    /* Finished jobs, completed in submission order by one BH: separate
     * one-shot BHs may run in any order, which published a later stamp and
     * the completion event before an earlier stamp (probe-167 GPU hang). */
    struct AGXRenderJob *agx_done_head, *agx_done_tail;
    QEMUBH *agx_done_bh;
    bool agx_render_thread_running;
    bool agx_render_thread_exit;
    unsigned agx_inflight_jobs;
    /* Queue read index already consumed on the host but not yet published to
     * the guest (published by agx_replay_done_bh after the pixels land).
     * A drain before then must continue from here, not re-read the guest's
     * stale index, or it would replay and stamp the same commands twice. */
    struct { uint64_t control; uint32_t index; bool valid; } agx_host_read[16];
    uint32_t disp_token;
    uint32_t disp_pending_token;
    uint32_t disp_fifo[512], disp_fifo_count, disp_fifo_expected, disp_fifo_token;
    bool disp_swap_pending;
    /* Native Chestnut ABI subset: register 0x05 power control. Virtual board
     * starts with rails disabled; this is not a claim about silicon reset ROM. */
    uint8_t chestnut_control;
    A7D2045 pmu;
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
    int result = arm_set_cpu_on(0x100, 0, 0, 1, false);
    if (result == QEMU_ARM_POWERCTL_RET_SUCCESS) {
        s->iop_started = true;
        qemu_log_mask(LOG_UNIMP, "ANS IOP native ARM32 firmware executing on experimental Cortex-A7; no handshake emulation\n");
    } else {
        qemu_log_mask(LOG_GUEST_ERROR, "ANS IOP CPU start rejected: %d\n", result);
    }
}

/* AIC's 0x2000 window follows the executing application processor. */
static unsigned a7_current_cpu(A7State *s)
{
    return s->ap_cpu[1] && current_cpu == CPU(s->ap_cpu[1]) ? 1 : 0;
}

static bool aic_target(A7State *s, unsigned irq, unsigned cpu)
{
    /* Retain the original uniprocessor routing, including target=0. */
    if (MACHINE(s)->smp.cpus == 1) {
        return cpu == 0;
    }
    return ((s->targets[irq] ? s->targets[irq] : 1) & (1U << cpu)) != 0;
}

static uint32_t aic_level(A7State *s, unsigned word)
{
    uint32_t level = s->soft[word] | s->device_irq[word];
    if (word == 140 / 32 && s->rx_count && (s->uart_reg[1] & 3) == 1) {
        level |= 1U << (140 % 32);
    }
    return level;
}

static void aic_update(A7State *s)
{
    for (unsigned c = 0; c < MACHINE(s)->smp.cpus; c++) {
        bool pending = !!(s->ipi[c] & ~s->ipi_mask[c]);
        for (unsigned i = 0; i < NIRQ; i++) {
            if ((aic_level(s, i / 32) & ~s->mask[i / 32] &
                 (1U << (i % 32))) && aic_target(s, i, c)) {
                pending = true;
                break;
            }
        }
        qemu_set_irq(qdev_get_gpio_in(DEVICE(s->ap_cpu[c]), ARM_CPU_IRQ), pending);
    }
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

static bool aic_cpu_window(A7State *s, hwaddr *off, unsigned *cpu)
{
    *cpu = a7_current_cpu(s);
    if (*off >= 0x2000 && *off < 0x2080) {
        *off -= 0x2000;
        return true;
    }
    if (*off >= 0x5000 && *off < 0x5100) {
        *cpu = (*off - 0x5000) / 0x80;
        *off &= 0x7f;
        return true;
    }
    return false;
}

static uint64_t aic_read(void *opaque, hwaddr off, unsigned size)
{
    A7State *s = opaque;
    unsigned c;
    if (aic_cpu_window(s, &off, &c)) {
        if (off == 0) { return c; }
        if (off == 0x24 || off == 0x28) { return s->ipi_mask[c]; }
        if (off == 4 && c < MACHINE(s)->smp.cpus) {
            for (unsigned i = 0; i < NIRQ; i++) {
                uint32_t bit = 1U << (i % 32);
                if ((aic_level(s, i / 32) & ~s->mask[i / 32] & bit) &&
                    aic_target(s, i, c)) {
                    /* Acknowledge atomically masks the shared source. */
                    s->mask[i / 32] |= bit;
                    aic_update(s);
                    return 0x10000 | i;
                }
            }
            uint32_t pending = s->ipi[c] & ~s->ipi_mask[c];
            if (pending) {
                unsigned num = (pending & 1) ? 1 : 2;
                s->ipi_mask[c] |= num == 1 ? 1 : 0x80000000;
                aic_update(s);
                return 0x40000 | num;
            }
        }
        return 0;
    }
    if (off == 4) {
        /* AppleInterruptController::start (10e5fe8) takes bits 20:16 as the
         * CPU count minus one and bits 9:0 as the external IRQ count. With 0
         * there it believed one CPU and its IPID path (10e63d0) refused every
         * IPI to CPU1, which then only woke on its own timer. */
        return ((uint64_t)(MACHINE(s)->smp.cpus - 1) << 16) | 192;
    }
    if (off >= 0x3000 && off < 0x4000) { return s->targets[(off - 0x3000) / 4]; }
    if (off >= 0x4100 && off < 0x4180) { return s->mask[(off - 0x4100) / 4]; }
    if (off >= 0x4200 && off < 0x4280) {
        unsigned word = (off - 0x4200) / 4;
        uint32_t level = s->soft[word] | s->device_irq[word];
        if (word == 140 / 32 && s->rx_count) { level |= 1U << (140 % 32); }
        return level;
    }
    qemu_log_mask(LOG_UNIMP, "A7 AIC unimplemented read 0x%" HWADDR_PRIx "\n", off);
    return 0;
}

static void aic_write(void *opaque, hwaddr off, uint64_t val, unsigned size)
{
    A7State *s = opaque;
    unsigned c;
    if (aic_cpu_window(s, &off, &c)) {
        switch (off) {
        case 8:
            /* Low bits select destinations; bit 31 requests a self IPI. */
            for (unsigned target = 0; target < MACHINE(s)->smp.cpus; target++) {
                if (val & (1U << target)) { s->ipi[target] |= 1; }
            }
            if (val & 0x80000000) { s->ipi[c] |= 0x80000000; }
            break;
        case 0xc: s->ipi[c] &= ~val; break;
        case 0x1c:
            /* IPID sends "other" IPIs by writing 1 to 0x501c + cpu * 0x80
             * (10e63e4); in that window c is the target CPU. */
            if (val & 1) { s->ipi[c] |= 1; }
            break;
        case 0x24: s->ipi_mask[c] |= val; break;
        case 0x28: s->ipi_mask[c] &= ~val; break;
        default: break;
        }
    } else if (off >= 0x3000 && off < 0x4000) { s->targets[(off - 0x3000) / 4] = val; }
    else if (off >= 0x4000 && off < 0x4080) { s->soft[(off - 0x4000) / 4] |= val; }
    else if (off >= 0x4080 && off < 0x4100) {
        unsigned word = (off - 0x4080) / 4;
        if (word == 2 && (val & ((1U << 20) | 2U))) {
            fprintf(stderr, "[AIC] Soft clear word 2 val=0x%" PRIx64 "\n", val);
        }
        s->soft[word] &= ~val;
    }
    else if (off >= 0x4100 && off < 0x4180) { s->mask[(off - 0x4100) / 4] |= val; }
    else if (off >= 0x4180 && off < 0x4200) {
        unsigned word = (off - 0x4180) / 4;
        if (word == 2 && (val & ((1U << 20) | 2U))) {
            fprintf(stderr, "[AIC] Mask clear (unmask) word 2 val=0x%" PRIx64 "\n", val);
        }
        s->mask[word] &= ~val;
    }
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

/* Native enable_core (0xffffff8000eccad8/0xffffff8000eccaf4) writes
 * a core bitmask to PMGR+0x30004 and +0x30008. The secure monitor installs
 * its warm-reset trampoline at the start of its 2-MiB-aligned allocation
 * (0x410000c194..0x410000c1b8); x0=0 selects the secondary path at c1d4.
 * Direct EL1 contract images instead enter the same reset code as cpu0 and
 * dispatch on MPIDR. The native firmware owns the eventual kernel entry.
 */
static void a7_release_cpus(A7State *s, uint32_t cores)
{
    if ((cores & 2) && s->ap_cpu[1] && !s->secondary_started) {
        uint64_t entry = s->boot_el == 3 ? s->boot_entry & ~UINT64_C(0x1fffff)
                                        : s->boot_entry;
        int result = arm_set_cpu_on(1, entry, 0, s->boot_el, true);
        s->secondary_started = result == QEMU_ARM_POWERCTL_RET_SUCCESS;
        if (!s->secondary_started) {
            qemu_log_mask(LOG_GUEST_ERROR, "A7 CPU1 release rejected: %d\n", result);
        }
    }
}

static void pmgr_write(void *opaque, hwaddr off, uint64_t val, unsigned size)
{
    A7State *s = opaque;
    if (off + size <= PMGR_SIZE) {
        if (size == 4 && off == 0x30008) {
            a7_release_cpus(s, val);
        }
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

/* VPHONE_TRACE_MMIO=a7.spi1,a7.gpio,... logs every access to the named
 * regions (bring-up aid for unmodelled devices). */
static bool mmio_trace_wanted(const char *name)
{
    static char *list;
    static bool parsed;
    if (!parsed) {
        const char *env = getenv("VPHONE_TRACE_MMIO");
        list = env ? g_strdup_printf(",%s,", env) : NULL;
        parsed = true;
    }
    if (!list) {
        return false;
    }
    g_autofree char *needle = g_strdup_printf(",%s,", name);
    return strstr(list, needle) != NULL;
}

static void mmio_trace(const char *name, const char *op, hwaddr off, unsigned size, uint64_t val)
{
    if (mmio_trace_wanted(name)) {
        CPUState *cpu = current_cpu;
        qemu_log_mask(LOG_UNIMP, "MMIO %s %s off=0x%" HWADDR_PRIx " size=%u val=0x%" PRIx64
                      " pc=0x%" PRIx64 " ns=%" PRId64 "\n", name, op, off, size, val,
                      cpu ? cpu->cc->get_pc(cpu) : 0, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
    }
}

/* GPIOIC ec99ec/ec9a0c: seven IRQ groups, each with seven 32-pin
 * status words. Group is config[23:16], NOT pin / 32 (DT #gpio-gcbs=7).
 * ec9c58 disables with type 0xe; ec9d60 enables with type 0xa for n53.
 * Bit zero is the sampled line, not an interrupt enable bit.
 * ec9cd0..cec maps IOKit flags to types: bit0 (kIOInterruptTypeLevel)
 * gives 6/4 (level low/high), else 0xa/8 (falling/rising edge); bit2
 * gives 0xc (both edges). n53 touch has flags 0 => falling edge 0xa.
 * Its handler never masks the pin, and ec9a28..b04 acks then rescans
 * until pending clears, so a level latch here livelocks (probe 89). */
static void touch_gpio_update(A7State *s)
{
    uint32_t config = ldl_le_p(s->gpio_regs + 22 * 4);
    unsigned group = (config >> 16) & 0xff;
    unsigned type = config & 0xe;
    if (group < 7 && ((type == 6 && s->touch_irq_low) ||
                      (type == 4 && !s->touch_irq_low))) {
        uint8_t *status = s->gpio_regs + 0x800 + group * 0x40;
        stl_le_p(status, ldl_le_p(status) | (1u << 22));
    }
    config = ldl_le_p(s->gpio_regs + 15 * 4);
    group = (config >> 16) & 0xff;
    type = config & 0xe;
    if (group < 7 && ((type == 6 && s->pmu.irq_low) ||
                      (type == 4 && !s->pmu.irq_low))) {
        uint8_t *status = s->gpio_regs + 0x800 + group * 0x40;
        stl_le_p(status, ldl_le_p(status) | (1u << 15));
    }
    for (unsigned g = 0; g < 7; g++) {
        uint32_t pending = 0;
        for (unsigned w = 0; w < 7; w++) {
            pending |= ldl_le_p(s->gpio_regs + 0x800 + g * 0x40 + w * 4);
        }
        unsigned irq = 108 + g;
        if (pending) { s->device_irq[irq / 32] |= 1u << (irq % 32); }
        else { s->device_irq[irq / 32] &= ~(1u << (irq % 32)); }
    }
    aic_update(s);
}

static void touch_gpio_level(A7State *s, bool low)
{
    uint32_t config = ldl_le_p(s->gpio_regs + 22 * 4);
    unsigned group = (config >> 16) & 0xff, type = config & 0xe;
    bool edge = s->touch_irq_low != low &&
                (type == 0xc || (type == 0xa && low) || (type == 8 && !low));
    if (group < 7 && edge) {
        uint8_t *status = s->gpio_regs + 0x800 + group * 0x40;
        stl_le_p(status, ldl_le_p(status) | (1u << 22));
    }
    s->touch_irq_low = low;
    touch_gpio_update(s);
}

static uint64_t gpio_read(void *opaque, hwaddr off, unsigned size)
{
    A7State *s = opaque;
    if (off + size <= GPIO_SIZE) {
        uint64_t val = 0;
        for (unsigned i = 0; i < size; i++) {
            uint8_t byte = s->gpio_regs[off + i];
            if (off + i == 15 * 4) {
                byte = (byte & ~1) | !s->pmu.irq_low;
            }
            if (off + i == 22 * 4) {
                byte = (byte & ~1) | !s->touch_irq_low;
            }
            if (((off + i) & 3) == 0) {
                unsigned pin = (off + i) / 4;
                /* Board-model pull-ups:
                 * 1) Native I2C lines: pins 80-83 (0x140..0x14c).
                 *    See research/native-i2c-gpio-contract.md.
                 * 2) Idle active-low peripheral IRQs: TriStar GPIO146 and
                 *    the unresponsive Mesa SPI2 slave GPIO79.
                 * 3) Physical push buttons: pins 2 (menu), 3 (hold), 4 (volup),
                 *    5 (voldown), 16 (ringerab).
                 *    On hardware, buttons are active-low (pulled high to VDD when
                 *    idle/unpressed, grounded to 0 when pressed).
                 *    AppleM68Buttons reads raw bit 0 via GPIOFunction::read and
                 *    inverts: pressed = (sample ^ inversion ^ 1). When unmodeled/0,
                 *    all buttons appear held down, triggering IOHIDEventDriver's
                 *    hold+volume sysdiagnose watchdog and NMI panic after 2.5s.
                 * 4) Wi-Fi HSIC: GPIO166 (0xa6, function-host_ready output) and
                 *    GPIO167 (0xa7, function-device_ready active-high input).
                 *    When the BCM43342 Wi-Fi module is absent or unpowered,
                 *    pin 167 remains 0 (LOW), indicating DEVICE_READY = 0.
                 *    EHCI1 port 0 PORTSC bit 0 (CCS) remains 0 (disconnected).
                 *    AppleBCMWLANBusInterfaceHSIC unconditionally executes 4
                 *    enumeration retry timeouts (~21.86s) before reporting
                 *    bus enumeration failure; real hardware has no early exit,
                 *    so pin 167 must faithfully remain 0 (not pulled up).
                 */
                if ((pin >= 80 && pin <= 83) || pin == 79 || pin == 146 ||
                    pin == 2 || pin == 3 || pin == 4 || pin == 5 || pin == 16) {
                    uint32_t config = ldl_le_p(s->gpio_regs + off + i);
                    bool driven = !(config & 0x200) && (config & 0xe) == 2;
                    bool sample = !driven || (config & 1);
                    byte = (byte & ~1) | sample;
                }
            }
            val |= (uint64_t)byte << (i * 8);
        }
        mmio_trace("a7.gpio", "read", off, size, val);
        return val;
    }
    qemu_log_mask(LOG_UNIMP, "A7 GPIO out-of-bounds read 0x%" HWADDR_PRIx "\n", off);
    return 0;
}

static void gpio_write(void *opaque, hwaddr off, uint64_t val, unsigned size)
{
    A7State *s = opaque;
    mmio_trace("a7.gpio", "write", off, size, val);
    if (off + size <= GPIO_SIZE) {
        for (unsigned i = 0; i < size; i++) {
            hwaddr a = off + i;
            uint8_t byte = val >> (i * 8);
            if (a >= 0x800 && a < 0x9c0 && (a & 0x3f) < 28) {
                s->gpio_regs[a] &= ~byte; /* ec9ae8: W1C */
            } else {
                s->gpio_regs[a] = byte;
            }
        }
        touch_gpio_update(s);
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

/* Detached DWC OTG device core. Exact n53 DT reservation; no USB host or
 * packet engine. Native e703cc/e6e688 polls GRSTCTL, e70974 polls GOUTNAKEFF,
 * e70a84 polls EPDISBLD. Commands complete synchronously on an idle bus.
 * Capability values describe the modeled 14-endpoint, 2080-word FIFO core;
 * they are not a claimed readback of A7 silicon. */
static void a7_otg_reset(A7State *s)
{
    uint32_t *r = s->usb_regs;
    memset(r, 0, 0x10000);
    r[0x000 / 4] = 1u << 16; /* CONIDSTS=B device, no A/B session valid */
    r[0x00c / 4] = 0x1400;
    r[0x010 / 4] = 1u << 31; /* AHBIdle */
    r[0x024 / 4] = 0x400;
    r[0x028 / 4] = 0x01000400;
    r[0x02c / 4] = 0x00080100; /* empty nonperiodic FIFO/queue */
    r[0x040 / 4] = 0x4f54280a; /* Synopsys OTG 2.80a */
    r[0x044 / 4] = 0x264; /* DT eps-dir-bitmap */
    r[0x048 / 4] = (1u << 19) | (7u << 10) | (1u << 6) | (2u << 3);
    r[0x04c / 4] = (0x820u << 16) | (4u << 4) | 4;
    r[0x050 / 4] = (7u << 26) | (1u << 25) | 7u;
    r[0x100 / 4] = 0x01000500;
    for (unsigned i = 0; i < 15; i++) {
        r[(0x104 + 4 * i) / 4] = (0x20u << 16) | (0x600 + 0x20 * i);
    }
    r[0x804 / 4] = 2; /* soft disconnect */
    r[0x808 / 4] = 1; /* suspended, no enumeration */
    for (unsigned ep = 0; ep < 16; ep++) {
        r[(0x900 + 0x20 * ep) / 4] = 1u << 17; /* NAKSTS */
        r[(0xb00 + 0x20 * ep) / 4] = 1u << 17;
        r[(0x918 + 0x20 * ep) / 4] = ep ? 0x20 : 0x100; /* TX FIFO space */
    }
}

static uint32_t a7_otg_daint(A7State *s)
{
    uint32_t *r = s->usb_regs, pending = 0;
    for (unsigned ep = 0; ep < 16; ep++) {
        if (r[(0x908 + 32 * ep) / 4] & r[0x810 / 4]) { pending |= 1u << ep; }
        if (r[(0xb08 + 32 * ep) / 4] & r[0x814 / 4]) { pending |= 1u << (16 + ep); }
    }
    return pending;
}

static uint32_t a7_otg_word(A7State *s, unsigned off)
{
    uint32_t *r = s->usb_regs;
    if (off == 0x818) { return a7_otg_daint(s); }
    if (off == 0x14) {
        uint32_t pending = a7_otg_daint(s) & r[0x81c / 4];
        /* Device mode, empty TX FIFO, no RX/session/reset/enumeration event.
         * NAK effective levels track DCTL requests, including when W1C'd. */
        return 0x20 | ((pending & 0xffff) ? (1u << 18) : 0) |
               ((pending >> 16) ? (1u << 19) : 0) |
               ((r[0x804 / 4] & 4) ? 0x40 : 0) |
               ((r[0x804 / 4] & 8) ? 0x80 : 0);
    }
    return r[off / 4];
}

static uint64_t a7_otg_read(void *opaque, hwaddr off, unsigned size)
{
    A7State *s = opaque;
    uint64_t value = 0;
    for (unsigned i = 0; i < size; i++) {
        value |= (uint64_t)((a7_otg_word(s, (off + i) & ~3u) >>
                             (8 * ((off + i) & 3))) & 0xff) << (8 * i);
    }
    return value;
}

static void a7_otg_store(A7State *s, unsigned off, uint32_t v, uint32_t mask)
{
    uint32_t *r = s->usb_regs;
    uint32_t old = r[off / 4];
    uint32_t next = (old & ~mask) | (v & mask);
    if (off == 0x10) {
        if (v & mask & 1) {
            a7_otg_reset(s);
        }
        r[off / 4] = 1u << 31; /* soft reset / RX+TX flush complete */
    } else if (off == 0x804) {
        uint32_t nak = old & 0xc;
        if (v & mask & (1u << 7)) { nak |= 4; }
        if (v & mask & (1u << 8)) { nak &= ~4u; }
        if (v & mask & (1u << 9)) { nak |= 8; }
        if (v & mask & (1u << 10)) { nak &= ~8u; }
        r[off / 4] = (next & ~0x78du) | nak; /* command pulses + RO status */
    } else if ((off >= 0x900 && off < 0xd00) && !(off & 0x1f)) {
        /* Endpoint CNAK/SNAK and disable are commands. No cable means no
         * transfer completion, even when software arms EP0 receive DMA. */
        uint32_t nak = old & (1u << 17);
        if (v & mask & (1u << 26)) { nak &= ~(1u << 17); }
        if (v & mask & (1u << 27)) { nak |= 1u << 17; }
        if (v & mask & (1u << 30)) {
            next &= ~(3u << 30);
            r[(off + 8) / 4] |= 2; /* EPDISBLD */
        }
        r[off / 4] = (next & ~((3u << 26) | (1u << 17))) | nak;
    } else if (off == 4 || off == 0x14 ||
               (off >= 0x900 && off < 0xd00 && (off & 0x1f) == 8)) {
        r[off / 4] &= ~(v & mask); /* W1C */
    } else if (off == 0 || off == 0x2c || (off >= 0x40 && off <= 0x50) ||
               off == 0x808 || off == 0x818 || off == 0x20 || off == 0x1c ||
               (off >= 0x900 && off < 0xb00 && (off & 0x1f) == 0x18)) {
        /* RO identity, cable, empty RX, interrupt summary and FIFO space. */
    } else {
        r[off / 4] = next; /* full DT aperture, including vendor holes */
    }
}

static void a7_usb_irq(void *opaque, int irq, int level);

static void a7_otg_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    A7State *s = opaque;
    /* Merge lanes once per word so a partial W1C/command cannot replay RO
     * or adjacent bits. QEMU splits unaligned accesses for this region. */
    while (size) {
        unsigned lane = off & 3, n = MIN(size, 4 - lane);
        uint32_t mask = (UINT32_MAX >> (8 * (4 - n))) << (8 * lane);
        a7_otg_store(s, off & ~3u, (uint32_t)value << (8 * lane), mask);
        off += n; size -= n; value >>= 8 * n;
    }
    a7_usb_irq(s, 162, (s->usb_regs[8 / 4] & 1) &&
                (a7_otg_word(s, 0x14) & s->usb_regs[0x18 / 4]));
}

static const MemoryRegionOps a7_otg_ops = {
    .read = a7_otg_read, .write = a7_otg_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 4 },
    .impl = { .min_access_size = 1, .max_access_size = 4 },
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

static int spi_index(A7Peripheral *p)
{
    if (p->base == UINT64_C(0x20a084000)) { return 0; }
    if (p->base == UINT64_C(0x20a088000)) { return 1; }
    if (p->base == UINT64_C(0x20a08c000)) { return 2; }
    return -1;
}

/* Chosen device identity, not a dump of an actual N53 controller.
 * N1 E2 supplies interface info; D0 is the sensor region descriptor.
 * Report consumers: f88c60 (D1 family),
 * f88d0c..9c (D3 endian/rows/cols/BE bcd), f88fc0 (D9 two LE u32).
 * Firmware caveat: deviceHasFirmware f8a05c reads guest object+c1;
 * f86d10..20 sets it after loader success. There is no SPI firmware-query
 * opcode. The loader below accepts the wire image and boot commands;
 * the native timer sets the cached flag only after loader success.
 * N1 uses E2 for interface info (f9065c: version RX[2], LE max RX[3:5]),
 * E3 for metadata (f908c8: error RX[2], LE length RX[3:5]), E6/E7 for
 * data (f90de4/f9106c: payload RX[3..], excluding report ID).
 */
static const struct {
    uint8_t id, len, data[16];
} touch_reports[] = {
    /* N1 E2 supplies interface info. D0 here is Sensor Region Descriptor,
     * parsed as count + count*7 bytes (187cd0788), NOT interface info.
     * No explicit regions: userland uses its surface-dimension fallback. */
    { 0xd0, 1, { 0 } },
    { 0xd1, 1, { 0x01 } },
    { 0xd3, 5, { 0x01, 0x10, 0x1b, 0x02, 0x66 } },
    /* D9 physical size is in hundredths of mm (HID 8d04..24), NOT
     * framebuffer pixels. The 16-byte form also publishes raw bounds
     * xmin,ymin,xmax,ymax; MTParseSensorSurfaceDescriptor (187cd08d0)
     * and mt_InitProcessing (187cd40a0..4124) override the family grid.
     * This chosen 51x91 mm surface uses pixel-valued raw coordinates. */
    { 0xd9, 16, { 0xec, 0x13, 0, 0, 0x8c, 0x23, 0, 0,
                  0, 0, 0, 0, 0x80, 0x02, 0x70, 0x04 } },
    { 0xd7, 1, { 0 } },
    { 0xab, 1, { 0 } },
    { 0x70, 1, { 0 } },
    /* 187cd3760: two device-endian u16 masks, detectable/suppressing. */
    { 0xbf, 4, { 0 } },
    { 0xaf, 1, { 0 } },
    { 0xc8, 1, { 0 } },
    /* f88ee8..f88f2c copies A1 to Sensor Region Param; no decode here. */
    { 0xa1, 1, { 0 } },
};

static const uint8_t *touch_report(A7Touch *t, uint8_t id, unsigned *len)
{
    for (unsigned i = 0; i < ARRAY_SIZE(touch_reports); i++) {
        if (touch_reports[i].id == id) {
            *len = touch_reports[i].len;
            return id == 0xab ? &t->mode :
                   id == 0x70 ? &t->reporting :
                   id == 0xbf ? t->input_control :
                   id == 0xaf ? &t->face_detection :
                   id == 0xc8 ? &t->input_reports : touch_reports[i].data;
        }
    }
    *len = 0;
    return NULL;
}

/* f93578: sum bytes mod 65536; byte order belongs to each protocol. */
static uint16_t touch_sum(const uint8_t *buf, unsigned len)
{
    uint16_t sum = 0;
    for (unsigned i = 0; i < len; i++) { sum += buf[i]; }
    return sum;
}

static void touch_enqueue(A7Touch *t, const uint8_t *frame, unsigned len)
{
    if (t->frame_count == ARRAY_SIZE(t->frames)) {
        error_report("A7 touch queue full; gesture paused");
        return;
    }
    unsigned tail = (t->frame_head + t->frame_count) % ARRAY_SIZE(t->frames);
    memcpy(t->frames[tail], frame, len);
    t->frame_len[tail] = len;
    t->frame_count++;
    touch_gpio_level(t->owner, true);
}

/* MultitouchSupport 187ccdfb0 -> 187ccf8e0: report 74, variable header
 * and contact stride; D3 endian=1 selects LE at 187ccf9a0. Full 30-byte
 * contact avoids the derived-force fallback used for 20-byte records. */
static void touch_contact(A7Touch *t, unsigned state, int x, int y)
{
    uint8_t f[56] = { 0x74, t->sequence++, 26, 4 };
    uint32_t ts = (uint32_t)(qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) / 1000);
    if (ts <= t->last_timestamp_us) {
        ts = t->last_timestamp_us + 1;
    }
    t->last_timestamp_us = ts;
    stl_le_p(f + 4, ts);
    f[10] = 4; /* contact frame, flags[1:0]=0 -> type 6 */
    stw_le_p(f + 12, 30);
    f[14] = 1;
    /* Binary+2 -> MTContact+18 is identity, not flags. Zero is
     * unidentified and fails MTParserPath::isFingerContact (HID a134).
     * Identity2 is a finger (1..5); path ID1 stays stable across frames. */
    f[26] = 1; f[27] = state; f[28] = 2; f[29] = 1;
    stw_le_p(f + 30, x);
    stw_le_p(f + 32, 1136 - y); /* user syntax is top-left pixels */
    stw_le_p(f + 38, state == 7 ? 0 : 400); /* ellipse radii, 1/100 mm */
    stw_le_p(f + 40, state == 7 ? 0 : 300);
    stw_le_p(f + 44, state == 7 ? 0 : 256); /* force */
    /* Full-stride extensions are density and quality, NOT radii:
     * 187cd1da0..1dbc/1d78..1d8c scale these by 1/256. */
    stw_le_p(f + 46, state == 7 ? 0 : 256); /* density */
    stw_le_p(f + 48, state == 7 ? 0 : 256); /* related density */
    stw_le_p(f + 50, state == 7 ? 0 : 256); /* quality */
    stw_le_p(f + 52, state == 7 ? 0 : 256); /* signal */
    touch_enqueue(t, f, sizeof(f));
}

static A7Touch *touch_device(A7State *s)
{
    for (unsigned i = 0; i < NUM_PERIPHERALS; i++) {
        if (s->peripherals[i].base == UINT64_C(0x20a084000)) {
            return &s->peripherals[i].spi.touch;
        }
    }
    return NULL;
}

/* Report70 is MTDeviceSetUILocked, not MTDeviceStart. Normal embedded
 * detection modes use BF (187cd39b8); standard mode0 uses mask 019d
 * (including bit2). Treat bit2 as contact enable for this personality.
 * UI lock (70=1) disables contact delivery. */
static bool touch_reporting_enabled(A7Touch *t)
{
    return t->reporting != 1 && (lduw_le_p(t->input_control) & 4);
}

static void touch_boot_complete(A7Touch *t)
{
    for (unsigned i = 0; i < t->frame_count; i++) {
        unsigned at = (t->frame_head + i) % ARRAY_SIZE(t->frames);
        if (t->frame_len[at] == 2 && t->frames[at][0] == 0x60 &&
            t->frames[at][1] == 2) { return; }
    }
    const uint8_t ready[] = { 0x60, 2 };
    touch_enqueue(t, ready, sizeof(ready));
}

/* A stop cancels contact data, not the asynchronous boot completion that
 * lets MultitouchHID restore its deferred InputDetectionMode (7af4/68ec).
 * Compact in FIFO order; never discard a boot notification awaiting EB. */
static void touch_stop_contacts(A7Touch *t)
{
    /* The controller discarded this contact session. Require a fresh click;
     * keep last_button so a held mouse cannot silently restart it. */
    t->owner->mouse_touch.phase = 0;
    t->owner->mouse_touch.pressed = false;
    timer_del(t->owner->mouse_touch_timer);
    unsigned kept = 0;
    for (unsigned i = 0; i < t->frame_count; i++) {
        unsigned at = (t->frame_head + i) % ARRAY_SIZE(t->frames);
        if (t->frame_len[at] == 2 && t->frames[at][0] == 0x60 &&
            t->frames[at][1] == 2) {
            unsigned to = (t->frame_head + kept++) % ARRAY_SIZE(t->frames);
            memcpy(t->frames[to], t->frames[at], 2);
            t->frame_len[to] = 2;
        }
    }
    t->frame_count = kept;
    touch_gpio_level(t->owner, kept != 0);
}

static void touch_timer_cb(void *opaque)
{
    A7State *s = opaque;
    A7Touch *t = touch_device(s);
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (s->touch_step >= s->touch_script.count) { return; }
    if (!t || (!s->touch_force && !touch_reporting_enabled(t)) ||
        t->frame_count == 64) {
        timer_mod(s->touch_timer, now + TOUCH_TICK_NS);
        return;
    }
    TouchScriptFrame *f = &s->touch_script.frames[s->touch_step];
    if (f->state == 7) {
        if (s->touch_settle_frames < 0) {
            /* Travel is measured from this gesture's start, not the script's. */
            unsigned start = s->touch_step;
            while (start > 0 && s->touch_script.frames[start].state != 1) {
                start--;
            }
            int dx = f->x - s->touch_script.frames[start].x;
            int dy = f->y - s->touch_script.frames[start].y;
            if (dx < 0) { dx = -dx; }
            if (dy < 0) { dy = -dy; }
            int64_t prev_ns = s->touch_step > 0 ?
                s->touch_script.frames[s->touch_step - 1].ns : 0;
            if ((dx > 10 || dy > 10) && (f->ns - prev_ns <= TOUCH_TICK_NS * 2)) {
                s->touch_settle_frames = 18;
            } else {
                s->touch_settle_frames = 0;
            }
        }
        if (s->touch_settle_frames > 0) {
            s->touch_settle_frames--;
            touch_contact(t, 4, f->x, f->y);
            timer_mod(s->touch_timer, now + TOUCH_TICK_NS);
            return;
        }
    }
    s->touch_settle_frames = -1;
    /* Policy/FIFO waits stretch the remaining timeline, preserving every
     * transition instead of bursting overdue frames into the guest. */
    s->touch_epoch_ns = MAX(s->touch_epoch_ns, now - f->ns);
    touch_contact(t, f->state, f->x, f->y);
    qemu_log_mask(LOG_UNIMP, "A7 TOUCH step=%u/%u state=%u x=%d y=%d "
                  "queued=%u ns=%" PRId64 " forced=%u ui=%u detectable=%04x\n",
                  s->touch_step, s->touch_script.count, f->state, f->x, f->y,
                  t->frame_count, now, s->touch_force,
                  t->reporting, lduw_le_p(t->input_control));
    if (++s->touch_step < s->touch_script.count) {
        timer_mod(s->touch_timer, s->touch_epoch_ns +
                  s->touch_script.frames[s->touch_step].ns);
    }
}

static void touch_script_reset(A7State *s)
{
    const char *script = getenv("VPHONE_TOUCH_SCRIPT");
    const char *force = getenv("VPHONE_TOUCH_FORCE");
    char error[256];
    timer_del(s->touch_timer);
    s->touch_step = 0;
    s->touch_settle_frames = -1;
    s->touch_script.count = 0;
    s->touch_force = force && !strcmp(force, "1");
    s->touch_epoch_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (!script || !*script) { return; }
    if (!touch_script_parse(script, &s->touch_script, error, sizeof(error))) {
        error_report("VPHONE_TOUCH_SCRIPT: %s; injection disabled", error);
        return;
    }
    timer_mod(s->touch_timer, s->touch_epoch_ns + s->touch_script.frames[0].ns);
}

/* Board-owned display device gives QEMU input a real console binding. */
#define TYPE_A7_TOUCH_DISPLAY "a7-touch-display"
OBJECT_DECLARE_SIMPLE_TYPE(A7TouchDisplay, A7_TOUCH_DISPLAY)
struct A7TouchDisplay {
    SysBusDevice parent_obj;
    A7State *board;
};

static void mouse_touch_timer_cb(void *opaque)
{
    A7State *s = opaque;
    MouseTouch *m = &s->mouse_touch;
    A7Touch *t = touch_device(s);
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    TouchScriptFrame f;
    bool ready = t && (s->touch_force || touch_reporting_enabled(t)) &&
                 t->frame_count < ARRAY_SIZE(t->frames);
    if (mouse_touch_next(m, now, ready, &f)) {
        touch_contact(t, f.state, f.x, f.y);
        qemu_log_mask(LOG_UNIMP, "A7 MOUSE state=%u x=%d y=%d phase=%u rel=%u "
                      "queued=%u ns=%" PRId64 "\n",
                      f.state, f.x, f.y, m->phase, m->release_frames,
                      t->frame_count, now);
    }
    if (m->phase != 0) {
        timer_mod(s->mouse_touch_timer,
                  m->due > now ? m->due : now + TOUCH_TICK_NS);
    } else {
        timer_del(s->mouse_touch_timer);
    }
}

static void mouse_touch_event(DeviceState *dev, QemuConsole *src,
                             QemuInputEvent *evt)
{
    A7State *s = A7_TOUCH_DISPLAY(dev)->board;
    MouseTouch *m = &s->mouse_touch;
    if (src != s->con) { return; }
    if (evt->type == INPUT_EVENT_KIND_ABS) {
        if (evt->abs.axis == INPUT_AXIS_X) {
            m->host_x = mouse_touch_scale(evt->abs.value, INPUT_EVENT_ABS_MAX, 640);
        } else if (evt->abs.axis == INPUT_AXIS_Y) {
            int y = mouse_touch_scale(evt->abs.value, INPUT_EVENT_ABS_MAX, 1136);
            /* MultitouchHID shifts raw contact ~16 px up in guest Y.
             * Compensate so mouse clicks land on the intended pixel. */
            m->host_y = MIN(1136, y + 16);
        }
    } else if (evt->type == INPUT_EVENT_KIND_BTN &&
               evt->btn.button == INPUT_BUTTON_LEFT) {
        m->button = evt->btn.down;
    }
}

static void mouse_touch_input_sync(DeviceState *dev)
{
    A7State *s = A7_TOUCH_DISPLAY(dev)->board;
    A7Touch *t = touch_device(s);
    bool allowed = s->mouse_touch_enabled && t &&
                   (s->touch_force || touch_reporting_enabled(t)) &&
                   s->touch_step >= s->touch_script.count;
    mouse_touch_sync(&s->mouse_touch, allowed,
                     qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
    mouse_touch_timer_cb(s);
}

static const QemuInputHandler mouse_touch_handler = {
    .name = "A7 touchscreen mouse",
    .mask = INPUT_EVENT_MASK_ABS | INPUT_EVENT_MASK_BTN,
    .event = mouse_touch_event,
    .sync = mouse_touch_input_sync,
};

static uint32_t touch_word(const uint8_t *b)
{
    /* f90098..c4 / f901d8..230: BE halfwords, LOW halfword first. */
    return ((uint32_t)b[2] << 24) | ((uint32_t)b[3] << 16) |
           ((uint32_t)b[0] << 8) | b[1];
}

static uint32_t touch_memory_read(A7Touch *t, uint32_t address)
{
    for (unsigned i = 0; i < t->memory_count; i++) {
        if (t->address[i] == address) { return t->value[i]; }
    }
    /* f93f88 checks CM/revision; APU_EN default follows C9 hypothesis. */
    if (address == 0x10008ffc) { return 0x434d11a0; }
    if (address == 0x10003800) { return 1; }
    return 0;
}

static void touch_finish(A7Touch *t, unsigned len)
{
    uint8_t *b = t->tx;
    /* HBPP raw boot image f9232c and framed firmware f925b0/f928e4.
     * Retain the latest wire image and accounting; no firmware execution.
     * Frame: 18 e1, 30 01, length/4 BE, address in BE low half first,
     * header sum BE, word-swapped data, u32 sum in the same half order.
     */
    if (len >= 3 && (b[0] == 0xea || b[0] == 0xeb)) {
        if (b[2] == 1 && t->frame_count &&
            len == t->frame_len[t->frame_head] + 7u &&
            lduw_le_p(b + len - 2) == touch_sum(b, 3)) {
            t->frame_head = (t->frame_head + 1) % ARRAY_SIZE(t->frames);
            t->frame_count--;
            /* Release after each read and re-assert for the next frame
             * so every pending frame produces a fresh falling edge. */
            touch_gpio_level(t->owner, false);
            if (t->frame_count) { touch_gpio_level(t->owner, true); }
        }
        return;
    }
    if (len > 512 || (len >= 16 && b[0] == 0x18 &&
                      b[1] == 0xe1 && b[2] == 0x30 && b[3] == 1)) {
        bool framed = b[0] == 0x18 && b[1] == 0xe1 &&
                      b[2] == 0x30 && b[3] == 1;
        bool valid = true;
        unsigned payload = len;
        uint32_t sum = 0;
        bool idle = true;
        for (unsigned i = 0; i < len; i++) {
            sum += b[i];
            idle = idle && b[i] == 0xff;
        }
        if (!framed && idle) { return; } /* RX-only dummy clocks */
        if (framed) {
            payload = (unsigned)lduw_be_p(b + 4) * 4;
            valid = payload <= len - 16 &&
                    touch_sum(b + 4, 6) == lduw_be_p(b + 10);
            sum = 0;
            if (valid) {
                for (unsigned i = 0; i < payload; i++) { sum += b[12 + i]; }
                valid = sum == touch_word(b + 12 + payload);
            }
        }
        t->boot_status = valid ? 0x4bc1 : 0xdead;
        if (valid) {
            memcpy(t->firmware, b + (framed ? 12 : 0), payload);
            t->firmware_length = payload;
            t->firmware_bytes += payload;
            t->firmware_checksum += sum;
        }
    } else if (len == 12 && b[0] == 0x1d && b[1] == 0x53 &&
               t->firmware_bytes &&
               touch_sum(b + 2, 8) == lduw_be_p(b + 10)) {
        /* f92e6c..f92f30: final execute address/argument command. */
        t->booted = true;
        t->boot_status = 0;
        /* Do not assert the level-low line yet: before the driver attaches
         * its handler, GPIOIC ec9a28..b04 acks and rescans forever while it
         * stays low (probe 85 livelock). Boot completion is announced on
         * the later 70 write instead. */
    } else if (len == 2 && b[0] == 0x1f && b[1] == 1 &&
               t->firmware_bytes) {
        /* f92220/f9402c: execute, then status helper and E2. */
        t->booted = true;
        t->boot_status = 0;
    } else if (len == 8 && b[0] == 0x1c && b[1] == 0x73 &&
        touch_sum(b + 2, 4) == lduw_be_p(b + 6)) {
        t->read_value = touch_memory_read(t, touch_word(b + 2));
        t->read_pending = true;
    } else if (len == 16 && b[0] == 0x1e && b[1] == 0x33 &&
               touch_sum(b + 2, 12) == lduw_be_p(b + 14)) {
        uint32_t address = touch_word(b + 2);
        uint32_t mask = touch_word(b + 6), value = touch_word(b + 10);
        unsigned i;
        for (i = 0; i < t->memory_count; i++) {
            if (t->address[i] == address) { break; }
        }
        if (i < ARRAY_SIZE(t->address)) {
            uint32_t old = touch_memory_read(t, address);
            if (i == t->memory_count) { t->memory_count++; }
            t->address[i] = address;
            t->value[i] = (old & ~mask) | (value & mask);
        }
    } else if (len == 16 && b[0] == 0xe4) {
        unsigned report_len;
        const uint8_t *report = touch_report(t, b[1], &report_len);
        t->ack_status = !report || b[2] != report_len || b[2] > 11 ||
                        touch_sum(b, 14) != lduw_le_p(b + 14);
        /* f91588..dc: opcode,id,length,data; E1 RX[8]==0 acknowledges
         * SetReport (f9167c..a8 / f91a48). */
        if (!t->ack_status && b[1] == 0xab && b[2] == 1) {
            t->mode = b[3];
        }
        if (!t->ack_status && b[1] == 0xbf && b[2] == 4) {
            bool was_enabled = touch_reporting_enabled(t);
            memcpy(t->input_control, b + 3, 4);
            if (was_enabled && !touch_reporting_enabled(t)) {
                touch_stop_contacts(t);
            }
        }
        if (!t->ack_status && b[1] == 0xaf && b[2] == 1) {
            t->face_detection = b[3];
        }
        if (!t->ack_status && b[1] == 0xc8 && b[2] == 1) {
            t->input_reports = b[3];
        }
        if (!t->ack_status && b[1] == 0x70 && b[2] == 1) {
            t->reporting = b[3];
            memset(t->input_control, 0, sizeof(t->input_control));
            touch_stop_contacts(t);
            if (t->reporting == 2 && t->booted) {
                /* Exiting UI lock resets the firmware reporting session.
                 * Announce completion even if boot's earlier notification
                 * was drained before a userland client subscribed. */
                touch_boot_complete(t);
            }
        }
    } else if (len >= 6 && b[0] == 0x92 &&
               (uint16_t)(b[1] + touch_sum(b + 4, len - 6)) ==
               lduw_be_p(b + len - 2)) {
        if (b[1] == 0xab && len == 7) { t->mode = b[4]; }
    }
}

static uint8_t touch_shift(A7Touch *t, unsigned pos, unsigned total, uint8_t tx)
{
    uint8_t *r = t->reply, *b = t->tx;
    unsigned len;
    const uint8_t *data;
    if (pos >= SPI_PACKET_SIZE || total > SPI_PACKET_SIZE) { return 0xff; }
    b[pos] = tx;
    if (pos == 0) {
        memset(r, 0, sizeof(t->reply));
        /* f8fcd4..fdd4 requires TWO valid BE tokens, at RX[0:2]
         * AND RX[2:4]. 4ad1 is an accepted HBPP token;
         * deviceHasFirmware f8a05c reads cached +c1 (not this reply). */
        if (tx == 0x1a) {
            static const uint8_t sync[] = { 0x4a, 0xd1, 0x4a, 0xd1 };
            memcpy(r, sync, sizeof(sync));
            if (total == 2 && t->boot_status) {
                stw_be_p(r, t->boot_status);
                t->boot_status = 0; /* one upload receipt; memory ops use 4ad1 */
            }
            if (t->read_pending && total == 8) {
                uint32_t v = t->read_value;
                r[2] = v >> 8; r[3] = v;
                r[4] = v >> 24; r[5] = v >> 16;
                t->read_pending = false; /* f90018..34 decodes RX[2..5] */
            }
        } else if (tx == 0xea || tx == 0xeb) {
            /* f8f884..c4: 16-byte length reply, LE sum at14. */
            unsigned n = t->frame_count ? t->frame_len[t->frame_head] + 2 : 0;
            r[0] = tx; stw_le_p(r + 1, n);
            stw_le_p(r + 14, touch_sum(r, 14));
        } else if (tx == 0xee) {
            /* f93a3c wakes controller, discards all RX bytes. */
            r[0] = tx; stw_le_p(r + 14, tx);
        } else if (tx >= 0xe1 && tx <= 0xe7) {
            r[0] = tx;
            if (tx == 0xe1) { r[8] = t->ack_status; }
            if (tx == 0xe2) {
                r[2] = 0x11; r[3] = 0; r[4] = 5;
            }
        } else if (tx == 0xd0) {
            r[0] = 0xaa; r[1] = 0x11; r[2] = 5;
        } else if (tx == 0x8f || tx == 0x82 || tx == 0x92) {
            r[0] = 0xaa;
        } else {
            memset(r, 0xff, sizeof(t->reply));
        }
    }
    if (pos == 1) {
        data = touch_report(t, b[1], &len);
        if (b[0] == 0xe3) {
            r[2] = data ? 0 : 1; r[3] = len; r[4] = len >> 8;
        } else if (b[0] == 0xe6 || b[0] == 0xe7) {
            r[1] = b[1];
            if (data) { memcpy(r + 3, data, len); }
        } else if (b[0] == 0x8f) {
            /* f87eb0..ec: BE checksum=id+RX[4]+RX[5], length 12b. */
            r[4] = len >> 8; r[5] = len;
            stw_be_p(r + 6, b[1] + r[4] + r[5]);
        } else if (b[0] == 0x82) {
            /* Base deviceGetReport f881d8..250: AA, payload at 4,
             * BE checksum=sum(payload)+id at payload_end. */
            if (data) { memcpy(r + 4, data, len); }
            stw_be_p(r + 4 + len, b[1] + touch_sum(r + 4, len));
        }
    }
    if (pos == 2 && (b[0] == 0xe6 || b[0] == 0xe7)) {
        /* Request phase 0 and result phase 1; checksum is over response
         * bytes, not request. E6 f90dc8..dc; E7 f91050..68. */
        r[2] = b[2];
    }
    if (pos == 2 && (b[0] == 0xea || b[0] == 0xeb) && tx == 1) {
        /* f8fabc/f8fb28: five-byte zero-sum header, payload at5,
         * LE payload sum in the last2. Query length includes checksum. */
        unsigned n = t->frame_count ? t->frame_len[t->frame_head] : 0;
        if (total == n + 7) {
            stw_le_p(r + 2, n + 2);
            r[4] = -(r[0] + r[1] + r[2] + r[3]);
            if (n) { memcpy(r + 5, t->frames[t->frame_head], n); }
            stw_le_p(r + 5 + n, touch_sum(r + 5, n));
        }
    }
    if (b[0] >= 0xe1 && b[0] <= 0xe7 && total >= 5) {
        unsigned end = total - 2;
        if (pos == end) { stw_le_p(r + end, touch_sum(r, end)); }
    }
    if (b[0] == 0x92 && total >= 6 && pos == total - 2) {
        /* f8877c..a4 verifies the checksum of host report id+payload. */
        stw_be_p(r + pos, b[1] + touch_sum(b + 4, total - 6));
    }
    uint8_t result = r[pos];
    if (pos + 1 == total) { touch_finish(t, total); }
    return result;
}

static uint32_t spi_status(A7SPI *spi)
{
    return spi->causes | (spi->tx_count << 6) | (spi->rx_count << 11);
}

static void spi_update(A7Peripheral *p)
{
    A7SPI *spi = &p->spi;
    uint32_t mode = ldl_le_p(p->regs + 4);
    unsigned irq = 137 + spi_index(p);
    bool level = ((mode & 0x80) && spi->rx_count) ||
                 ((mode & 0x100) && spi->total &&
                  spi->shifted < spi->total && spi->tx_count < SPI_FIFO_SIZE) ||
                 ((mode & 0x200000) && (spi->causes & 0x400000));
    if (level) {
        p->owner->device_irq[irq / 32] |= 1U << (irq % 32);
    } else {
        p->owner->device_irq[irq / 32] &= ~(1U << (irq % 32));
    }
    aic_update(p->owner);
}

static void sio_spi_dma_run(A7Peripheral *p);

static void spi_run(A7Peripheral *p)
{
    A7SPI *spi = &p->spi;
    if ((spi_index(p) == 0 || spi_index(p) == 1) &&
        (ldl_le_p(p->regs + 4) & 0x40)) {
        sio_spi_dma_run(p);
        spi_update(p);
        return;
    }
    /* PIO enable + channel enable. SPI2/3 have an unconnected MISO line.
     * GPIO CS0 selection is performed by the guest; no GPIO IRQ modeled. */
    if (!(ldl_le_p(p->regs) & 1) ||
        !(ldl_le_p(p->regs + 4) & 0x20) ||
        (ldl_le_p(p->regs + 4) & 0x40)) {
        spi_update(p);
        return;
    }
    while (spi->tx_count && spi->rx_count < SPI_FIFO_SIZE &&
           spi->shifted < spi->total) {
        uint8_t tx = spi->tx[spi->tx_head];
        uint8_t rx = spi_index(p) == 0 ?
            touch_shift(&spi->touch, spi->shifted, spi->total, tx) : 0xff;
        spi->tx_head = (spi->tx_head + 1) % SPI_FIFO_SIZE;
        spi->tx_count--;
        spi->rx[(spi->rx_head + spi->rx_count) % SPI_FIFO_SIZE] = rx;
        spi->rx_count++;
        spi->shifted++;
        if (spi->shifted == ldl_le_p(p->regs + 0x4c)) {
            spi->causes |= 0x400000;
        }
    }
    spi_update(p);
}

static uint32_t spi_read(A7Peripheral *p, hwaddr off)
{
    A7SPI *spi = &p->spi;
    if (off == 8) { return spi_status(spi); }
    if (off == 0x20) {
        uint8_t value = 0;
        if (spi->rx_count) {
            value = spi->rx[spi->rx_head];
            spi->rx_head = (spi->rx_head + 1) % SPI_FIFO_SIZE;
            spi->rx_count--;
        }
        spi_run(p);
        return value;
    }
    return ldl_le_p(p->regs + off);
}

static void spi_write(A7Peripheral *p, hwaddr off, uint32_t value)
{
    A7SPI *spi = &p->spi;
    if (off == 8) {
        spi->causes &= ~(value & 0x40000f); /* counts are read-only */
    } else if (off == 0x10) {
        if (spi->tx_count < SPI_FIFO_SIZE) {
            spi->tx[(spi->tx_head + spi->tx_count) % SPI_FIFO_SIZE] = value;
            spi->tx_count++;
        }
    } else {
        stl_le_p(p->regs + off, value);
        if (off == 0x34) {
            spi->total = value;
            spi->shifted = spi->causes = 0;
            spi->tx_head = spi->tx_count = spi->rx_head = spi->rx_count = 0;
        } else if (off == 0 && !value) {
            spi->total = spi->shifted = spi->causes = 0;
            spi->tx_head = spi->tx_count = spi->rx_head = spi->rx_count = 0;
        }
    }
    spi_run(p);
}

/* Recovered ABI: 10513ec/105164c BE register prefix; 10532b0 reads
 * 5c6..5cb, discarding two fractional bytes and decoding LE seconds.
 * PMURTC 105cd38 adds the persistent LE offset at 4004..4007.
 * Unspecified registers are writable storage, with zero reset values.
 * Calibration and ADC samples below are explicit emulation choices. */
static void d2045_update_irq(A7State *s)
{
    bool low = false;
    for (unsigned i = 0; i < 12; i++) {
        low |= (s->pmu.regs[0x50 + i] & ~s->pmu.regs[0x70 + i]) != 0;
    }
    uint32_t config = ldl_le_p(s->gpio_regs + 15 * 4);
    unsigned group = (config >> 16) & 0xff, type = config & 0xe;
    if (group < 7 && s->pmu.irq_low != low &&
        (type == 0xc || (type == 0xa && low) || (type == 8 && !low))) {
        uint8_t *status = s->gpio_regs + 0x800 + group * 0x40;
        stl_le_p(status, ldl_le_p(status) | (1u << 15));
    }
    s->pmu.irq_low = low;
    touch_gpio_update(s);
}

static uint32_t d2045_seconds(A7D2045 *d)
{
    return d->rtc_base +
           (qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) - d->rtc_start_ns) / 1000000000;
}

static unsigned d2045_adc_sample(unsigned channel)
{
    /* 12-bit raw samples: nominal battery, NTC divider, die temperature.
     * Exact analog scaling/calibration needs validation on the next boot. */
    switch (channel) {
    case 8: return 1061; /* die temperature, native fallback trim 0x425 */
    case 0: case 4: return 2130; /* VCC_MAIN / VBAT: about 3.8 V */
    case 2: case 7: return 2048; /* thermistor / divider: about 1.25 V */
    case 11: case 12: case 13: case 14: return 432; /* tdev: about 25 C */
    case 9: return 0; /* VBUS: detached, 1053d90..da8 / 105462c..648 */
    default: return 2048;
    }
}

static void d2045_adc_done(void *opaque)
{
    A7State *s = opaque;
    A7D2045 *d = &s->pmu;
    unsigned sample = d2045_adc_sample(d->regs[0x500] & 0x1f);
    d->regs[0x502] = sample & 0xf;
    d->regs[0x503] = sample >> 4;
    d->regs[0x500] &= ~0x80;
    /* 105152c: event 91 is consumed internally and wakes ADC state 1 -> 2. */
    d->regs[0x5b] |= 2;
    d2045_update_irq(s);
}

static void d2045_reset(A7State *s)
{
    A7D2045 *d = &s->pmu;
    const char *epoch = getenv("VPHONE_RTC_EPOCH");
    char *end = NULL;
    uint64_t seconds = time(NULL);
    if (epoch) {
        errno = 0;
        unsigned long long parsed = strtoull(epoch, &end, 10);
        if (!errno && epoch[0] >= '0' && epoch[0] <= '9' &&
            end != epoch && !*end && parsed <= UINT32_MAX) {
            seconds = parsed;
        } else {
            error_report("Invalid VPHONE_RTC_EPOCH '%s'; using host Unix time", epoch);
        }
    }
    timer_del(d->adc_timer);
    memset(d->regs, 0, sizeof(d->regs));
    memset(d->regs + 0x70, 0xff, 12); /* all events masked */
    /* Nondegenerate synthetic calibration endpoints, not hardware OTP IDs. */
    d->regs[0x4a9] = 0x42;
    d->regs[0x4aa] = 5;
    d->regs[0x519] = 0x00; d->regs[0x51a] = 0x04;
    d->regs[0x51b] = 0x0c; d->regs[0x51c] = 0x00;
    d->regs[0x51d] = 0x00; d->regs[0x51e] = 0x04;
    d->regs[0x51f] = 0x00; d->regs[0x520] = 0x0c;
    d->rtc_base = seconds;
    d->rtc_start_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    d->irq_low = false;
}

static void d2045_store(A7State *s, uint16_t reg, uint8_t value)
{
    A7D2045 *d = &s->pmu;
    if (reg >= 0x5c8 && reg <= 0x5cb) {
        uint32_t seconds = d2045_seconds(d);
        unsigned shift = (reg - 0x5c8) * 8;
        d->rtc_base = (seconds & ~(0xffu << shift)) | (uint32_t)value << shift;
        d->rtc_start_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    }
    if (reg >= 0x50 && reg < 0x5c) {
        d->regs[reg] &= ~value; /* defensive W1C; normal driver uses read-clear */
    } else {
        d->regs[reg] = value;
    }
    if (reg == 0x500) {
        if (value & 0x80) {
            timer_mod(d->adc_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 1000000);
        } else {
            timer_del(d->adc_timer);
        }
    } else if (reg == 0x541 && (value & 0x80)) {
        unsigned sample = d2045_adc_sample(value & 0x3f);
        d->regs[0x541] &= ~0x80;
        d->regs[0x542] = sample & 0xf;
        d->regs[0x543] = sample >> 4;
    }
    d2045_update_irq(s);
}

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
    p->pmu_rx_head = p->pmu_rx_count = 0;
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
            /* AppleChestnutDisplayPMU::setLDO writes 0x17 (rails on) and
             * 0x10 (off): bit 4 is part of the native register (probe-151). */
            if (value & 0xe0) {
                i2c_nack(p);
                return;
            }
            p->owner->chestnut_control = value & 0x1f;
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

/* Native AppleCBTL1610: REV 0x0f (e9e8c8), muxes 1/2
 * (e9eff8/e9f1b0), status 0x0c and read-clear events 0x0d
 * (e9eb70/e9f2ac), mask 0x0e (e9e98c). The connector is empty:
 * status bit2=1 means no cable; bit7=0 means no accessory identity.
 * Revision 0x20 is a synthetic supported revision, not an OTP identity.
 * No events are fabricated, so GPIO IRQ remains deasserted. This lets
 * TriStar publish the dock's native IOFunctionParent mux functions;
 * baseband start waits for function-bb_usb_mux at f0da34. */
static bool i2c_is_tristar(A7Peripheral *p)
{
    /* On by default since the system ID, EHCI/OHCI/OTG and HSIC models let
     * the dock and baseband finish (probe-149..153: no panic, no busy IOKit
     * leaves at 45 s). VPHONE_TRISTAR=0 restores the old NACK for bisects. */
    static int enabled = -1;
    if (enabled < 0) {
        const char *env = getenv("VPHONE_TRISTAR");
        enabled = !env || strcmp(env, "0");
    }
    return enabled && i2c_index(p) == 0 && p->i2c_address == 0x1a;
}

static bool i2c_is_lm3534(A7Peripheral *p)
{
    return i2c_index(p) == 0 && p->i2c_address == 0x63;
}

/* AppleLMBacklight start f2095c..98 reads four bytes at 0; enable
 * f20b08..94 writes the same block with bit0 toggled. Byte-addressed
 * control storage is sufficient for the native function provider to start. */
static void board_i2c_command(A7Peripheral *p, uint32_t value)
{
    bool tristar = i2c_is_tristar(p);
    A7RegisterI2C *d = tristar ? &p->tristar : &p->lm3534;
    /* AppleTriStarBuiltIn::read (e9cc78) forwards the byte prefix unchanged
     * to AppleARMIICDevice::transfer (800bbc). getSystemID (e9ea10) reads
     * 74 00 00 at 0x20: marker followed by synthetic packed board ID 0.
     * No translation to 0x5d exists in that path. Retain the observed
     * probe139 alias for compatibility while the prefix/buffer discrepancy
     * is investigated; it is not a recovered CBTL hardware mapping. */
    unsigned id_base = tristar && d->pointer >= 0x5d ? 0x5d : 0x20;
    /* LM3534's four control bytes are registers 0x10..0x13 (f20960..6c:
     * register 0x10 unless the DT says islp8559); 0..3 alias the same bytes
     * for the synthetic fixtures. */
    unsigned lm_base = !tristar && d->pointer >= 0x10 ? 0x10 : 0;
    unsigned limit = tristar ? (d->pointer >= 0x20 ? id_base + 3 : 16) : lm_base + 4;
    if (value & 0x100) {
        if (!p->i2c_read) {
            d->prefix = true;
            d->pointer_valid = false;
        }
    } else if (!p->i2c_active) {
        return;
    } else if (value & 0x400) {
        unsigned count = value & 0xff;
        if (!p->i2c_read || !d->pointer_valid || !count ||
            d->pointer >= limit || count > limit - d->pointer || p->pmu_rx_count) {
            i2c_nack(p);
            return;
        }
        for (unsigned i = 0; i < count; i++) {
            unsigned reg = d->pointer++;
            p->pmu_rx[i] = tristar && reg == 0xf ? 0x20 :
                           tristar && reg == 0xc ? 4 :
                           tristar && reg == 0xd ? 0 :
                           /* 0x20 window (CBTL e9ea10): marker first. 0x5d
                            * window (runtime variant e9daa8..b00): ID low,
                            * ID high, then marker 0x74 in the third byte. */
                           tristar && id_base == 0x20 && reg == 0x20 ? 0x74 :
                           tristar && id_base == 0x5d && reg == 0x5f ? 0x74 :
                           tristar && reg >= id_base ? 0 :
                           !tristar ? d->regs[reg & 3] : d->regs[reg];
        }
        p->pmu_rx_head = 0;
        p->pmu_rx_count = count;
        stl_le_p(p->regs + 0x14, ldl_le_p(p->regs + 0x14) | (1u << 20));
    } else if (!p->i2c_read) {
        if (d->prefix) {
            d->pointer = value & 0xff;
            d->pointer_valid = d->pointer < (tristar ? 16u : 4u) ||
                               (!tristar && d->pointer >= 0x10 && d->pointer < 0x14) ||
                               (tristar && ((d->pointer >= 0x20 && d->pointer <= 0x22) ||
                                            (d->pointer >= 0x5d && d->pointer <= 0x5f)));
            d->prefix = false;
            if (!d->pointer_valid) {
                i2c_nack(p);
                return;
            }
        } else if (d->pointer_valid && d->pointer < limit) {
            unsigned reg = d->pointer++;
            if (!tristar) {
                d->regs[reg & 3] = value;
            } else if (reg < 0xc || reg == 0xe) {
                d->regs[reg] = value;
            }
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
            stl_le_p(p->regs + 0x14, ldl_le_p(p->regs + 0x14) | (1u << 27));
        }
    }
}

static bool i2c_is_d2045(A7Peripheral *p)
{
    return i2c_index(p) == 0 && p->i2c_address == 0x74;
}

static void d2045_command(A7Peripheral *p, uint32_t value)
{
    A7D2045 *d = &p->owner->pmu;
    if (value & 0x100) {
        p->pmu_rx_head = p->pmu_rx_count = 0;
        if (!p->i2c_read) {
            p->pmu_prefix_count = 0;
            p->pmu_pointer_valid = false;
            p->pmu_pointer = 0;
        }
    } else if (!p->i2c_active) {
        return;
    } else if (value & 0x400) {
        unsigned count = value & 0xff;
        if (!p->i2c_read || !p->pmu_pointer_valid || !count || p->pmu_rx_count) {
            i2c_nack(p);
            return;
        }
        /* Snapshot the counter once per burst, including across second rollover. */
        uint32_t seconds = d2045_seconds(d);
        uint16_t fraction = ((qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) -
                             d->rtc_start_ns) % 1000000000) * 65536 / 1000000000;
        for (unsigned i = 0; i < count; i++) {
            uint16_t reg = p->pmu_pointer++;
            uint8_t byte = d->regs[reg];
            if (reg >= 0x5c8 && reg <= 0x5cb) {
                byte = seconds >> ((reg - 0x5c8) * 8);
            } else if (reg == 0x5c6 || reg == 0x5c7) {
                byte = fraction >> ((reg - 0x5c6) * 8);
            }
            p->pmu_rx[i] = byte;
            if (reg >= 0x50 && reg < 0x5c) {
                d->regs[reg] = 0; /* native handler has no explicit acknowledge */
            }
        }
        p->pmu_rx_head = 0;
        p->pmu_rx_count = count;
        d2045_update_irq(p->owner);
        stl_le_p(p->regs + 0x14, ldl_le_p(p->regs + 0x14) | (1u << 20));
    } else if (!p->i2c_read) {
        if (p->pmu_prefix_count < 2) {
            p->pmu_pointer = (p->pmu_pointer << 8) | (value & 0xff);
            p->pmu_pointer_valid = ++p->pmu_prefix_count == 2;
        } else {
            d2045_store(p->owner, p->pmu_pointer++, value);
        }
    } else {
        i2c_nack(p);
        return;
    }
    if (value & 0x200) {
        p->i2c_active = false;
        if (!p->i2c_read) {
            if (!p->pmu_pointer_valid) {
                i2c_nack(p);
                return;
            }
            stl_le_p(p->regs + 0x14, ldl_le_p(p->regs + 0x14) | (1u << 27));
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
            p->i2c_rx_valid = false;
            p->pmu_rx_head = p->pmu_rx_count = 0;
            if (!i2c_is_chestnut(p) && !i2c_is_d2045(p) &&
                !i2c_is_tristar(p) && !i2c_is_lm3534(p)) {
                /* Other slave addresses retain the native NACK boundary. */
                i2c_nack(p);
                qemu_log_mask(LOG_UNIMP,
                              "A7 I2C%d address=0x%02x direction=%s NACK (unmodeled slave)\n",
                              i2c_index(p), p->i2c_address, p->i2c_read ? "read" : "write");
            }
        }
        if (i2c_is_chestnut(p)) {
            chestnut_command(p, value);
        } else if (i2c_is_tristar(p) || i2c_is_lm3534(p)) {
            board_i2c_command(p, value);
        } else if (i2c_is_d2045(p)) {
            d2045_command(p, value);
        } else if (value & 0x200) {
            p->i2c_active = false;
        }
        break;
    case 0x10:
        if (value & 0x80000000) {
            p->i2c_active = false;
            p->i2c_rx_valid = false;
            p->chestnut_prefix = false;
            p->tristar.prefix = p->tristar.pointer_valid = false;
            p->lm3534.prefix = p->lm3534.pointer_valid = false;
            p->pmu_rx_head = p->pmu_rx_count = p->pmu_prefix_count = 0;
            p->pmu_pointer_valid = false;
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

static uint32_t i2c_read(A7Peripheral *p, hwaddr off)
{
    if (off == 4) {
        if (p->pmu_rx_count) {
            p->pmu_rx_count--;
            return p->pmu_rx[p->pmu_rx_head++];
        }
        if (p->i2c_rx_valid) {
            uint8_t byte = p->i2c_rx_byte;
            p->i2c_rx_valid = false;
            return byte;
        }
        return 0x100; /* Native RX FIFO empty flag. */
    }
    if (off == 8) {
        return (p->pmu_rx_count + (p->i2c_rx_valid ? 1 : 0)) << 8;
    }
    if (off == 0x0c) { return 0; }
    return ldl_le_p(p->regs + off);
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

static void sio_spi_dma_kick(A7State *s);

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
    sio_spi_dma_kick(s);
    return msg;
}

/* SmartIODMA: 91d558/91d5cc use handle*12 and (handle+2)*12.
 * 91dff4 stores segment count at header+20. 91ed74 emits packed
 * {u64 physical, u32 bytes}. Every descriptor and payload is RAM-only.
 * Bound work per request and validate the entire list before any bus byte
 * or guest write, including segments beyond the controller count.
 */
#define SIO_SPI_DMA_LIMIT MiB

static bool sio_spi_dma_list(A7State *s, SioDMA *dma, uint8_t *buffer,
                             bool receive)
{
    uint8_t header[24], segments[0x8000];
    uint64_t table = s->sio.dma_seg_phys;
    uint32_t capacity = s->sio.dma_seg_count;
    uint32_t count, total = 0;
    if (!table || capacity > 0xaaa || dma->handle >= capacity ||
        capacity - dma->handle < 2 ||
        !ans_read_ram(table + (uint64_t)dma->handle * 12,
                      header, sizeof(header))) {
        return false;
    }
    count = ldl_le_p(header + 20);
    if (!count || count > capacity - dma->handle - 2) { return false; }
    if (!ans_read_ram(table + ((uint64_t)dma->handle + 2) * 12,
                      segments, (size_t)count * 12)) { return false; }
    for (uint32_t i = 0; i < count; i++) {
        const uint8_t *segment = segments + i * 12;
        uint64_t address = ldq_le_p(segment);
        uint32_t length = ldl_le_p(segment + 8);
        if (!length || length > SIO_SPI_DMA_LIMIT - total ||
            !ans_ram_range(address, length)) { return false; }
        total += length;
    }
    if (!buffer) { dma->length = total; return true; }
    if (total != dma->length) { return false; }
    total = 0;
    for (uint32_t i = 0; i < count; i++) {
        const uint8_t *segment = segments + i * 12;
        uint64_t address = ldq_le_p(segment);
        uint32_t length = ldl_le_p(segment + 8);
        bool ok = receive ? ans_write_ram(address, buffer + total, length) :
                            ans_read_ram(address, buffer + total, length);
        if (!ok) { return false; }
        total += length;
    }
    return true;
}

static void sio_spi_dma_complete(A7State *s, unsigned channel, bool ok)
{
    SioDMA *dma = &s->sio.spi_dma[channel];
    /* 91eee0..ef94: 68, param bit7, original tag, exact byte count.
     * 91db4c also consumes bit6 as channel idle (stop waiter).
     * Short count is reported as IOReturn underrun by the native client.
     */
    qemu_log_mask(LOG_UNIMP,
                  "SPI%u DMA EP=%u tag=%u bytes=%u complete=%s\n",
                  1 + channel / 2, 22 + channel, dma->tag, dma->length, ok ? "ok" : "short");
    dma->active = false;
    sio_outbox_push(s, sio_pack_msg(22 + channel, dma->tag, 0x68,
                                   0xc0, ok ? dma->length : 0));
}

static void sio_spi_dma_run(A7Peripheral *p)
{
    A7State *s = p->owner;
    A7SPI *spi = &p->spi;
    unsigned channel = 2 * spi_index(p);
    SioDMA *tx = &s->sio.spi_dma[channel], *rx = &s->sio.spi_dma[channel + 1];
    uint32_t clocks = MAX(ldl_le_p(p->regs + 0x34),
                          ldl_le_p(p->regs + 0x4c));
    unsigned completions = tx->active + rx->active;
    if (!(ldl_le_p(p->regs) & 1) || !completions || !clocks ||
        spi->shifted || s->sio.outbox_count > SIO_OUTBOX_CAPACITY - completions) {
        return;
    }
    bool ok = clocks <= SIO_SPI_DMA_LIMIT && clocks <= SPI_PACKET_SIZE;
    if (tx->active) { ok = sio_spi_dma_list(s, tx, NULL, false) && ok; }
    if (rx->active) { ok = sio_spi_dma_list(s, rx, NULL, false) && ok; }
    ok = ok && (!tx->active || tx->length <= clocks) &&
               (!rx->active || rx->length <= clocks);
    uint8_t *output = NULL, *input = NULL;
    if (ok && tx->active) {
        output = g_malloc(tx->length);
        ok = sio_spi_dma_list(s, tx, output, false);
    }
    if (ok && rx->active) { input = g_malloc(rx->length); }
    if (ok) {
        spi->total = clocks;
        for (uint32_t i = 0; i < clocks; i++) {
            uint8_t byte = output && i < tx->length ? output[i] : 0xff;
            /* SPI2 has no enrolled sensor/biometric firmware. Complete
             * real clocks on its idle-high MISO wire, as in PIO; let
             * the native Mesa driver reject the invalid response. */
            uint8_t reply = spi_index(p) == 0 ?
                touch_shift(&spi->touch, i, clocks, byte) : 0xff;
            if (input && i < rx->length) { input[i] = reply; }
        }
        spi->shifted = clocks;
        spi->causes |= 0x400000;
        /* DMA drains RX even for a TX-only transfer; no PIO FIFO residue. */
        spi->tx_count = spi->rx_count = 0;
        if (input) { ok = sio_spi_dma_list(s, rx, input, true); }
    }
    g_free(input);
    g_free(output);
    if (tx->active) { sio_spi_dma_complete(s, channel, ok); }
    if (rx->active) { sio_spi_dma_complete(s, channel + 1, ok); }
}

static void sio_spi_dma_kick(A7State *s)
{
    for (unsigned i = 0; i < NUM_PERIPHERALS; i++) {
        if (spi_index(&s->peripherals[i]) >= 0 &&
            spi_index(&s->peripherals[i]) <= 1) {
            spi_run(&s->peripherals[i]);
        }
    }
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
    } else if (ep >= 22 && ep <= 25) {
        SioDMA *dma = &s->sio.spi_dma[ep - 22];
        if (op == 5) {
            uint8_t config[20];
            bool ok = !dma->active && data < s->sio.dma_seg_count &&
                s->sio.dma_seg_count <= 0xaaa &&
                s->sio.dma_seg_count - data >= 2 &&
                s->sio.dma_seg_phys &&
                ans_read_ram(s->sio.dma_seg_phys + (uint64_t)data * 12,
                             config, sizeof(config));
            dma->configured = ok;
            sio_outbox_push(s, sio_pack_msg(ep, tag, ok ? 0x65 : 0x66, 0, 0));
        } else if (op == 6) {
            SioDMA next = { .handle = data, .tag = tag,
                           .configured = dma->configured, .active = true };
            if (!dma->configured || dma->active ||
                !sio_spi_dma_list(s, &next, NULL, false)) {
                sio_outbox_push(s, sio_pack_msg(ep, tag, 0x66, 0, 0));
            } else {
                /* Submission goes through synchronousMessage (91af64 panics
                 * on a missing reply): ACK now, op68 completes it later. */
                *dma = next;
                sio_outbox_push(s, sio_pack_msg(ep, tag, 0x65, 0, 0));
                sio_spi_dma_kick(s);
            }
        } else if (op == 7) {
            sio_outbox_push(s, sio_pack_msg(ep, tag, 0x69,
                                           dma->active ? dma->tag : 0, 0));
        } else if (op == 8 &&
                   s->sio.outbox_count <= SIO_OUTBOX_CAPACITY - 2) {
            sio_outbox_push(s, sio_pack_msg(ep, tag, 0x65, 0, 0));
            if (dma->active) { sio_spi_dma_complete(s, ep - 22, false); }
            else { sio_outbox_push(s, sio_pack_msg(ep, 0, 0x68, 0x40, 0)); }
        } else {
            sio_outbox_push(s, sio_pack_msg(ep, tag, 0x66, 0, 0));
        }
    } else if (ep >= 2 && ep < 0x50) {
        /* AppleSmartIODMA, same contract as the SPI1 endpoints above:
         * op06 is a synchronous submit (ACK now, op68 completes it), op07
         * reports the active tag, and op08 stops. An async op66 here is
         * fatal to the client ('unexpected asynchronous error', probe 113),
         * so a stop completes each armed submission with zero bytes
         * (param bit7 tagged report, bit6 idle on the last one). */
        uint8_t *pending = s->sio.dma_pending[ep];
        uint8_t *count = &s->sio.dma_pending_count[ep];
        if (op == 6) {
            if (*count < ARRAY_SIZE(s->sio.dma_pending[0])) {
                pending[(*count)++] = tag;
            }
            sio_outbox_push(s, sio_pack_msg(ep, tag, 0x65, 0, 0));
        } else if (op == 7) {
            sio_outbox_push(s, sio_pack_msg(ep, tag, 0x69,
                                           *count ? pending[0] : 0, 0));
        } else if (op == 8) {
            sio_outbox_push(s, sio_pack_msg(ep, tag, 0x65, 0, 0));
            if (!*count) {
                sio_outbox_push(s, sio_pack_msg(ep, 0, 0x68, 0x40, 0));
            }
            for (unsigned i = 0; i < *count; i++) {
                sio_outbox_push(s, sio_pack_msg(ep, pending[i], 0x68,
                                               i + 1 == *count ? 0xc0 : 0x80, 0));
            }
            *count = 0;
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
    if (s->sep.os_active_acked || (!s->sep.os_active_pending && !s->sep.os_active_sent)) {
        return;
    }
    if (s->sep.outbox_count == SEP_OUTBOX_CAPACITY) {
        timer_mod(s->sep.os_active_timer,
                  qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 2000000);
        return;
    }
    /* AppleSEPManager drops OS-active that arrives before it enters the
     * booting state ("received OS Active message in unexpected state 2")
     * and then panics "SEP/OS failed to boot". The AP starts endpoint
     * traffic once it accepts the message, so resend until it does. */
    if (s->sep.os_active_sent) {
        if (s->sep.os_active_retries >= 8) {
            return;
        }
        s->sep.os_active_retries++;
        qemu_log_mask(LOG_UNIMP, "SEP software model OS-active resend %u (no AP traffic yet)\n",
                      s->sep.os_active_retries);
    } else {
        qemu_log_mask(LOG_UNIMP, "SEP software model OS-active (not firmware execution)\n");
    }
    s->sep.os_active_pending = false;
    s->sep.os_active_sent = true;
    sep_outbox_push(s, sio_pack_msg(0, 0, 0x0d, 0, 0));
    timer_mod(s->sep.os_active_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 250000000);
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
        const char *delay = getenv("VPHONE_SEP_OSACTIVE_DELAY_NS"); /* race testing */
        timer_mod(s->sep.os_active_timer,
                  qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                  (delay ? strtoll(delay, NULL, 0) : 2000000));
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
    if (s->sep.os_active_sent && ep != 0xff && ep != 0xfe) {
        s->sep.os_active_acked = true;
    }
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
        if (cmd == 0x11 || cmd == 0x18 || cmd == 0x19) {
            /* Cmd 17 (0x11): get_lock_state.
             * Cmd 24 (0x18): table entry 0xffffff8000db4310 (struct 0x38,
             * serializer db1814) packs three u32, one u64 and an empty blob
             * (24 bytes) and unpacks two u64. Caller 0xffffff8000daa3ac passes
             * them to daa028: u64[0] is an action mask (bits 0-3) with a
             * timeout in bits 8+, u64[1] a companion value. A zero mask
             * returns at daa248 and maps to kIOReturnSuccess (db2fd0[8]).
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
            size_t reply_length = cmd == 0x19 ? 8 : 16; /* Cmd 24: two u64 */
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
        if (spi_index(p) >= 0 && size == 4) {
            return spi_read(p, off);
        }
        if (i2c_index(p) >= 0 && size == 4) {
            return i2c_read(p, off);
        }
        if (p->base == UINT64_C(0x201000000)) {
            if (off == 0x20) {
                return 0x00014600; /* PowerVR G6430 Silicon Core ID */
            }
            if (off == 0x18) {
                return 0x00000000; /* Core Revision */
            }
        }
        if (p->base == UINT64_C(0x206400000) && size == 4) {
            if (off == 0x0000) {
                /* Samsung DSIM STATUS, derived from the control registers
                 * that AppleSamsungMIPIDSIController::_waitStatus
                 * (0xffffff8000e91d20) polls after writing them:
                 * [3:0]/8 data/clock stop state, [7:4]/9 data/clock ULPS
                 * (ESCMODE 0x14: bit3/bit1 request, bit2/bit0 exit), 10
                 * TxReadyHsClk (CLKCTRL 0x08 bit31), 20 software reset
                 * released (SWRST 0x04 completes immediately), 31 PLL stable
                 * (PLLCTRL 0x4c bit23 PLL_EN). The PHY has no latency here.
                 */
                uint32_t clkctrl = ldl_le_p(p->regs + 0x08);
                uint32_t escmode = ldl_le_p(p->regs + 0x14);
                uint32_t pllctrl = ldl_le_p(p->regs + 0x4c);
                bool ulps_data = (escmode & 0x8) && !(escmode & 0x4);
                bool ulps_clock = (escmode & 0x2) && !(escmode & 0x1);
                uint32_t status = 1U << 20;
                status |= ulps_data ? 0xf0 : 0x0f;
                status |= ulps_clock ? 0x200 : 0x100;
                if (clkctrl & 0x80000000U) {
                    status |= 1U << 10;
                }
                if (pllctrl & (1U << 23)) {
                    status |= 1U << 31;
                }
                return status;
            }
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
        if (spi_index(p) >= 0 && size == 4) {
            spi_write(p, off, val);
            return;
        }
        if (i2c_index(p) >= 0 && size == 4) {
            i2c_write(p, off, val);
            return;
        }
        if (p->base == UINT64_C(0x201e00000) && off == 0x1000) {
            /* SGX power/reset register bit 4 self-clears upon reset completion */
            val &= ~UINT64_C(0x10);
        }
        if (p->base == UINT64_C(0x20e200000) && size == 4 &&
            (off == 0x10 || off == 0x20 || off == 0x40)) {
            /* DWI channel control: bit 0 (GO, written as 0x41/0x71) clears
             * once the backlight word at +4 has been shifted out, which
             * AppleS5L8940DWI::_waitTransaction (10ea5a4) polls for. Nothing
             * is attached to the wire, so the transfer completes at once. */
            val &= ~UINT64_C(1);
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
                            /* FIFO writes bypass the MMIO callback trace. */
                            uint32_t addr = s->disp_fifo[i] & 0xffff;
                            if (s->display_trace && (addr == 0x4044 || addr == 0x4070 || addr == 0x4098)) {
                                qemu_log_mask(LOG_UNIMP,
                                              "DISPLAY_FIFO ns=%" PRId64 " origin=guest region=%s op=write "
                                              "pa=0x%" HWADDR_PRIx " off=0x%x size=4 value=0x%x via=fifo\n",
                                              qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL),
                                              p->name, p->base + addr, addr,
                                              s->disp_fifo[i + 1]);
                            }
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
            if (off == 0x4098) {
                /* DPU0MMUPGADDR0: physical address >> 4 of the layer-0 plane's
                 * first page, programmed by AppleMobileADBE0::program_hardware
                 * (0xf28a98) from the IOSurface memory descriptor; 0x4044
                 * (DPU0SRCBASE) is only the plane offset within it. */
                A7State *s = p->owner;
                *(uint32_t *)(p->regs + off) = (uint32_t)val;
                /* The register holds the display MMU page table: one u32
                 * (page PA >> 4) | 1 per 4 KiB page of the plane. */
                s->disp_mmu_table = (uint64_t)(uint32_t)val << 4;
                s->disp_stride = *(uint32_t *)(p->regs + 0x4040);
                s->disp_offset = *(uint32_t *)(p->regs + 0x4044);
                if (val != 0) {
                    MemTxResult r;
                    uint32_t page0 = address_space_ldl_le(&address_space_memory, s->disp_mmu_table,
                                                          MEMTXATTRS_UNSPECIFIED, &r);
                    if (r == MEMTX_OK && (page0 & 1)) {
                        s->framebuffer = (uint64_t)(page0 & ~0xfU) << 4;
                    }
                    fprintf(stderr, "[DISP0] layer0 mmu table 0x%" PRIx64 " page0 0x%" PRIx64
                            " stride 0x%x\n", s->disp_mmu_table, s->framebuffer, s->disp_stride);
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
         p->base != UINT64_C(0x20a110000) &&
         p->base != UINT64_C(0x201000000) &&
         p->base != UINT64_C(0x201e00000))) {
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

/*
 * AGX META firmware host interface, high-level model. Only the boot
 * handshake is modeled; no GPU command stream is executed.
 *
 * Native AGXMetaFirmware start (0xffffff800118472c) writes a boot program
 * to the sgx1 sequencer at +0x10000 as {reg | mask << 16, value} pairs
 * ending in 0xffff0000, then runs it through sgx1+0x1000 bit 0. The program
 * sets RGX_CR_SOFT_RESET (0x100), RGX_CR_META_BOOT (0xbf8) and
 * RGX_CR_BIF_CAT_BASE0 (0x1200/0x1204), the GPU page catalogue.
 *
 * The firmware boot page at GPU VA 0xe1c0000000 (META 0x40000000) holds the
 * PIO list at +0x200, a (0, 0) terminator, and then the META address of
 * argv. META addresses map to GPU VA 0xe1c0000000 + (addr & 0x3fffffff)
 * (0xffffff800118588c). argv[1] is the shared control page. Firmware->host
 * ring 0 has its control block at shared+0x40 (read index +0, write index
 * +0x10, 256 entries) and its 0x28-byte entries at shared+0xc0
 * (0xffffff8001181c1c). Type 0 is "META alive" (0xffffff80011851a8).
 * The firmware raises AIC software IRQ 84, which the driver acknowledges
 * through AIC+0x4088 (0xffffff80011852c0).
 *
 * Host->firmware: the driver enqueues on host ring N (control at
 * shared+0x98c0+N*0x20, 0x18-byte entries at shared+0x9940+N*0x1800, argument
 * at +8) and writes sgx0+0xb00 = 0x10|N (0xffffff8001184fd8, 0x118504c,
 * 0x1185134). Ring 1 carries the META address of a channel queue block
 * (0xffffff8001186ab8): +4 control (read +0, write +0x20, size +0x30) and +8
 * a u32 array of command META addresses (0xffffff8001186cb4). Command type 4
 * is {4, stamp META address, stamp value, channel} (0xffffff800117e0ec); the
 * model stores the stamp and reports completion with a type-1 firmware event
 * (0xffffff800117505c). Render (type 1) and mapping (type 3) commands are
 * consumed without executing any GPU work.
 */
#define AGX_FW_HEAP_GVA       UINT64_C(0xe1c0000000)
#define AGX_HOST_IRQ          84
#define AGX_SEQ_BASE          0x10000
#define AGX_SEQ_END           0x20000
#define AGX_FW_RING0_CTRL     0x40
#define AGX_FW_RING0_ENTRIES  0xc0
#define AGX_FW_RING_STRIDE    0x28
#define AGX_HOST_RING_CTRL    0x98c0
#define AGX_HOST_RING_ENTRIES 0x9940
#define AGX_HOST_RING_STRIDE  0x18
#define AGX_HOST_RING_SPAN    0x1800
#define AGX_DOORBELL          0xb00

static bool agx_gpu_translate_ctx(A7State *s, uint64_t cat_base,
                                  uint64_t gva, hwaddr *pa)
{
    MemTxResult r;
    if (!cat_base) {
        return false;
    }
    uint32_t pce = address_space_ldl_le(&address_space_memory,
                                        cat_base + ((gva >> 30) & 0x3ff) * 4,
                                        MEMTXATTRS_UNSPECIFIED, &r);
    if (r != MEMTX_OK || !(pce & 1)) {
        return false;
    }
    uint64_t pde = address_space_ldq_le(&address_space_memory,
                                        ((uint64_t)(pce >> 4) << 12) + ((gva >> 21) & 0x1ff) * 8,
                                        MEMTXATTRS_UNSPECIFIED, &r);
    if (r != MEMTX_OK || !(pde & 1)) {
        return false;
    }
    uint64_t pte = address_space_ldq_le(&address_space_memory,
                                        (pde & UINT64_C(0xfffffff000)) + ((gva >> 12) & 0x1ff) * 8,
                                        MEMTXATTRS_UNSPECIFIED, &r);
    if (r != MEMTX_OK || !(pte & 1)) {
        return false;
    }
    *pa = (pte & UINT64_C(0xfffffff000)) | (gva & 0xfff);
    return true;
}

static bool agx_gpu_translate(A7State *s, uint64_t gva, hwaddr *pa)
{
    return agx_gpu_translate_ctx(s, s->agx_cat_base, gva, pa);
}

static bool agx_gpu_access(A7State *s, uint64_t gva, void *buf, size_t length, bool write)
{
    uint8_t *bytes = buf;
    while (length) {
        size_t chunk = MIN(length, 0x1000 - (gva & 0xfff));
        hwaddr pa;
        if (!agx_gpu_translate(s, gva, &pa) ||
            (write ? address_space_write(&address_space_memory, pa, MEMTXATTRS_UNSPECIFIED,
                                         bytes, chunk)
                   : address_space_read(&address_space_memory, pa, MEMTXATTRS_UNSPECIFIED,
                                        bytes, chunk)) != MEMTX_OK) {
            return false;
        }
        gva += chunk; bytes += chunk; length -= chunk;
    }
    return true;
}

static uint64_t agx_meta_gva(uint32_t addr)
{
    return AGX_FW_HEAP_GVA + (addr & 0x3fffffff);
}

static bool agx_post_fw_message(A7State *s, uint32_t type)
{
    uint64_t control = s->agx_shared_gva + AGX_FW_RING0_CTRL;
    uint32_t read_index, write_index;
    uint8_t entry[AGX_FW_RING_STRIDE] = { 0 };
    stl_le_p(entry, type);
    if (!agx_gpu_access(s, control, &read_index, 4, false) ||
        !agx_gpu_access(s, control + 0x10, &write_index, 4, false)) {
        return false;
    }
    if (((write_index + 1) & 0xff) == (read_index & 0xff)) {
        qemu_log_mask(LOG_GUEST_ERROR, "AGX META: firmware ring 0 full\n");
        return false;
    }
    if (!agx_gpu_access(s, s->agx_shared_gva + AGX_FW_RING0_ENTRIES +
                        (write_index & 0xff) * AGX_FW_RING_STRIDE, entry, sizeof(entry), true)) {
        return false;
    }
    smp_wmb();
    write_index = (write_index + 1) & 0xff;
    agx_gpu_access(s, control + 0x10, &write_index, 4, true);
    s->soft[AGX_HOST_IRQ / 32] |= 1U << (AGX_HOST_IRQ % 32);
    aic_update(s);
    return true;
}

static void agx_meta_alive_cb(void *opaque)
{
    A7State *s = opaque;
    uint32_t pio[(0x1000 - 0x200) / 4];
    if (!agx_gpu_access(s, AGX_FW_HEAP_GVA + 0x200, pio, sizeof(pio), false)) {
        qemu_log_mask(LOG_GUEST_ERROR, "AGX META: boot page unmapped (cat base 0x%" PRIx64 ")\n",
                      s->agx_cat_base);
        return;
    }
    unsigned i = 0;
    while (i + 2 < ARRAY_SIZE(pio) && (pio[i] || pio[i + 1])) {
        i += 2;
    }
    uint32_t argv[2];
    if (i + 2 >= ARRAY_SIZE(pio) ||
        !agx_gpu_access(s, AGX_FW_HEAP_GVA + (pio[i + 2] & 0x3fffffff), argv, sizeof(argv), false)) {
        qemu_log_mask(LOG_GUEST_ERROR, "AGX META: argv not found after %u PIO entries\n", i / 2);
        return;
    }
    s->agx_shared_gva = agx_meta_gva(argv[1]);
    s->agx_queue_count = 0;
    if (!agx_post_fw_message(s, 0)) { /* type 0: META alive */
        qemu_log_mask(LOG_GUEST_ERROR, "AGX META: ring 0 unmapped at shared GVA 0x%" PRIx64 "\n",
                      s->agx_shared_gva);
        return;
    }
    s->agx_meta_alive_count++;
    qemu_log_mask(LOG_UNIMP, "AGX META: alive posted (PIO %u, shared GVA 0x%" PRIx64 ")\n",
                  i / 2, s->agx_shared_gva);
}

/* Opt-in, read-only PBE discovery. Use exact encodings to avoid incidental
 * matches from masked low-byte comparisons in shader/state buffers.
 * Prefer a u64 hit over a u32 hit at the same offset; width is in bits.
 */
static void agx_pbe_scan(const uint8_t *raw, size_t length,
                         unsigned field, uint64_t gva)
{
    static const uint64_t surfaces[] = {
        UINT64_C(0x400000000), UINT64_C(0x400470000),
        UINT64_C(0x4008e0000),
    };
    static const unsigned shifts[] = { 0, 2, 4, 6, 8, 12 };

    for (unsigned off = 0; off + 4 <= length; off += 4) {
        uint64_t v32 = ldl_le_p(raw + off);
        bool have64 = off + 8 <= length;
        uint64_t v64 = have64 ? ldq_le_p(raw + off) : 0;
        uint64_t value = 0, surface = 0;
        unsigned width = 0, shift = 0;

        for (unsigned w = 0; w < 2 && !width; w++) {
            uint64_t v = w == 0 ? v64 : v32;
            if (!v || (w == 0 && !have64)) {
                continue;
            }
            for (unsigned i = 0; i < ARRAY_SIZE(surfaces) && !width; i++) {
                for (unsigned j = 0; j < ARRAY_SIZE(shifts); j++) {
                    if (v == (surfaces[i] >> shifts[j]) ||
                        (w == 0 && shifts[j] == 0 &&
                         (v & UINT64_C(0xffffffffff)) == surfaces[i])) {
                        value = v;
                        surface = surfaces[i];
                        shift = shifts[j];
                        width = w == 0 ? 64 : 32;
                        break;
                    }
                }
            }
        }
        if (width) {
            qemu_log_mask(LOG_UNIMP,
                          "AGX PBE hit cmd-field +0x%x buf gva 0x%llx off 0x%x width %u value 0x%llx surface 0x%llx shift %u\n",
                          field, (unsigned long long)gva, off, width,
                          (unsigned long long)value,
                          (unsigned long long)surface, shift);
        }
    }
}

static void agx_pbe_search(A7State *s, const uint8_t *raw, uint32_t address)
{
    static unsigned commands;
    uint64_t seen_gva[16], seen_cat[16];
    unsigned buffers = 0;
    uint32_t ctx = ldl_le_p(raw + 4);
    uint64_t ctx_cat = ctx < ARRAY_SIZE(s->agx_ctx_cat) - 3 ?
                       s->agx_ctx_cat[ctx + 3] : 0;
    uint8_t bytes[0x10000];

    if (!getenv("VPHONE_AGX_PBE_SEARCH") || commands >= 200) {
        return;
    }
    commands++;
    /* The command itself uses cmd-field +0x0 and its firmware GVA. */
    agx_pbe_scan(raw, 0x280, 0, agx_meta_gva(address));
    for (unsigned field = 0; field + 4 <= 0x280 && buffers < 16; field += 4) {
        for (unsigned width = 4; width <= 8 && buffers < 16; width += 4) {
            if (field + width > 0x280) {
                continue;
            }
            uint64_t gva = width == 4 ? ldl_le_p(raw + field) :
                                       ldq_le_p(raw + field);
            hwaddr pa;
            uint64_t cat = ctx_cat;
            if (!gva || gva >= (UINT64_C(1) << 40)) {
                continue;
            }
            if (!agx_gpu_translate_ctx(s, cat, gva, &pa)) {
                /* META pointers have 01 or 11 in their top two bits. */
                if (gva > UINT32_MAX || !(gva & 0x40000000)) {
                    continue;
                }
                gva = agx_meta_gva(gva);
                cat = s->agx_cat_base;
                if (!agx_gpu_translate_ctx(s, cat, gva, &pa)) {
                    continue;
                }
            }
            bool duplicate = false;
            for (unsigned i = 0; i < buffers; i++) {
                if (seen_gva[i] == gva && seen_cat[i] == cat) {
                    duplicate = true;
                    break;
                }
            }
            if (duplicate) {
                continue;
            }
            seen_gva[buffers] = gva;
            seen_cat[buffers++] = cat;
            size_t length = 0;
            while (length < sizeof(bytes)) {
                size_t chunk = MIN(sizeof(bytes) - length,
                                   0x1000 - ((gva + length) & 0xfff));
                if (!agx_gpu_translate_ctx(s, cat, gva + length, &pa) ||
                    address_space_read(&address_space_memory, pa,
                                       MEMTXATTRS_UNSPECIFIED, bytes + length,
                                       chunk) != MEMTX_OK) {
                    break;
                }
                length += chunk;
            }
            agx_pbe_scan(bytes, length, field, gva);
        }
    }
    if (commands == 200) {
        qemu_log_mask(LOG_UNIMP,
                      "AGX PBE search disabled after 200 commands\n");
    }
}

/* AGXF v1: 64-byte LE header (see agx_frame.py), two 0x280-byte
 * commands, then {u64 GVA, u64 PA, u8 page[4096]} records. No filtering:
 * aliases and every valid PC/PD/PT mapping are retained, up to 96 MiB total.
 */
/* The TA command whose parameter buffer (+0x14) matches a 3D command's
 * (+0x10), newest first; NULL when it has not been seen. */
static const uint8_t *agx_find_ta(A7State *s, uint32_t context, const uint8_t *render)
{
    uint32_t param = ldl_le_p(render + 0x10);
    if (context >= ARRAY_SIZE(s->agx_ta_ring)) {
        return NULL;
    }
    for (unsigned i = 1; i <= ARRAY_SIZE(s->agx_ta_ring[context]); i++) {
        const uint8_t *ta = s->agx_ta_ring[context][(uint8_t)(s->agx_ta_ring_next[context] - i)];
        if (ldl_le_p(ta) == 0 && ldl_le_p(ta + 0x14) == param && param) {
            return ta;
        }
    }
    return NULL;
}

static bool agx_ctx_read(A7State *s, uint64_t cat, uint64_t gva, void *buf, size_t n);

/* Count index-list (draw) blocks in a TA command's VDM stream. */
static unsigned agx_vdm_draw_count(A7State *s, uint64_t cat, const uint8_t *ta)
{
    uint64_t pc = ldq_le_p(ta + 0x68);
    unsigned draws = 0;
    for (unsigned guard = 0; guard < 4096; guard++) {
        uint32_t w[2];
        if (!agx_ctx_read(s, cat, pc, w, 8)) {
            break;
        }
        switch (w[0] >> 29) {
        case 6: return draws;
        case 4: pc = ((uint64_t)(w[0] & 0xff) << 32) | (w[1] & ~3u); break;
        case 2: pc += 4 * (1 + !!(w[0] & (1u << 28)) + !!(w[0] & (1u << 27)) +
                           3 * !!(w[0] & (1u << 26))); break;
        case 1: pc += 12; break;
        case 0: pc += 8; break;
        case 3: draws++; pc += 12; break;
        default: return draws;
        }
    }
    return draws;
}

/* Row-wise counterpart of agx_ctx_read: one page walk and one memory-API
 * write per page instead of a walk plus a store per pixel, which made the
 * replay hold the BQL for most of each frame (probe-162 host profile). */
static bool agx_ctx_write(A7State *s, uint64_t cat, uint64_t gva, const void *buf, size_t n)
{
    const uint8_t *in = buf;
    bool ok = true;
    while (n) {
        hwaddr pa;
        size_t chunk = MIN(n, 0x1000 - (gva & 0xfff));
        /* Like the old per-pixel stores, skip only the unmapped pages. */
        if (gva >= (UINT64_C(1) << 40) || !agx_gpu_translate_ctx(s, cat, gva, &pa) ||
            address_space_write(&address_space_memory, pa, MEMTXATTRS_UNSPECIFIED,
                                in, chunk) != MEMTX_OK) {
            ok = false;
        }
        in += chunk;
        gva += chunk;
        n -= chunk;
    }
    return ok;
}

static const uint8_t *agx_ta_snapshot_page(A7State *s, uint32_t context,
                                           const uint8_t *raw, uint64_t gva);

static void agx_frame_dump(A7State *s, uint32_t context, uint64_t cat,
                           uint64_t target, uint32_t queue, const uint8_t *raw)
{
    const uint8_t *dump_ta = agx_find_ta(s, context, raw);
    const uint8_t empty_ta[0x280] = {0};
    bool ta_valid = dump_ta != NULL;
    if (!dump_ta) {
        dump_ta = empty_ta;
    }
    const char *dir = getenv("VPHONE_AGX_FRAME_DUMP");
    const char *count_env = getenv("VPHONE_AGX_FRAME_DUMP_COUNT");
    unsigned limit = 3, number;
    int result, close_result;
    uint64_t parsed;
    uint8_t header[64] = {0}, pc[4096], pd[4096], pt[4096];
    uint8_t record[16 + 4096];
    uint64_t pages = 0;
    bool capped = false, ok = false;
    FILE *out = NULL, *json = NULL;
    g_autofree char *path = NULL;
    g_autofree char *json_path = NULL;
    g_autofree char *ta_hex = NULL;
    g_autofree char *render_hex = NULL;

    if (!dir || !*dir) {
        return;
    }
    /* Bound captures to the transition under investigation, independent of
     * how many animation passes boot timing happened to submit beforehand. */
    const char *after_env = getenv("VPHONE_AGX_FRAME_DUMP_AFTER_NS");
    if (after_env && qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) <
                     strtoull(after_env, NULL, 0)) {
        return;
    }
    if (count_env) {
        if (qemu_strtou64(count_env, NULL, 10, &parsed) || parsed > UINT_MAX) {
            qemu_log_mask(LOG_GUEST_ERROR, "AGX FRAME invalid dump count: %s\n", count_env);
            return;
        }
        limit = parsed;
    }
    if (s->agx_frame_dump_count >= limit) {
        return;
    }
    {
        /* SKIP leaves early boot frames out; EVERY spreads dumps over time. */
        const char *skip_env = getenv("VPHONE_AGX_FRAME_DUMP_SKIP");
        const char *every_env = getenv("VPHONE_AGX_FRAME_DUMP_EVERY");
        uint64_t skip = skip_env ? strtoull(skip_env, NULL, 0) : 0;
        uint64_t every = every_env ? MAX(strtoull(every_env, NULL, 0), 1) : 1;
        uint64_t seen = ++s->agx_frame_dump_seen;
        if (seen <= skip || (seen - skip - 1) % every) {
            return;
        }
    }
    number = ++s->agx_frame_dump_count;
    path = g_strdup_printf("%s/frame-%u.bin", dir, number);
    json_path = g_strdup_printf("%s/frame-%u.json", dir, number);
    if (g_mkdir_with_parents(dir, 0755) < 0 || !(out = fopen(path, "wb"))) {
        goto done;
    }
    memcpy(header, "AGXF", 4);
    stl_le_p(header + 4, 1);
    stl_le_p(header + 8, context);
    stq_le_p(header + 16, cat);
    stq_le_p(header + 24, target);
    stl_le_p(header + 32, 0x280);
    stl_le_p(header + 36, 0x280);
    stl_le_p(header + 40, queue);
    stl_le_p(header + 44, s->agx_last_ta_queue[context]);
    stq_le_p(header + 56, s->disp_mmu_table);
    if (fwrite(header, 1, sizeof(header), out) != sizeof(header) ||
        fwrite(raw, 1, 0x280, out) != 0x280 ||
        fwrite(dump_ta, 1, 0x280, out) != 0x280 ||
        address_space_read(&address_space_memory, cat, MEMTXATTRS_UNSPECIFIED,
                           pc, sizeof(pc)) != MEMTX_OK) {
        goto done;
    }
    for (unsigned i = 0; i < 1024; i++) {
        uint32_t pce = ldl_le_p(pc + i * 4);
        if (!(pce & 1)) {
            continue;
        }
        if (address_space_read(&address_space_memory, (uint64_t)(pce >> 4) << 12,
                               MEMTXATTRS_UNSPECIFIED, pd, sizeof(pd)) != MEMTX_OK) {
            goto done;
        }
        for (unsigned j = 0; j < 512; j++) {
            uint64_t pde = ldq_le_p(pd + j * 8);
            if (!(pde & 1)) {
                continue;
            }
            if (address_space_read(&address_space_memory, pde & UINT64_C(0xfffffff000),
                                   MEMTXATTRS_UNSPECIFIED, pt, sizeof(pt)) != MEMTX_OK) {
                goto done;
            }
            for (unsigned k = 0; k < 512; k++) {
                uint64_t pte = ldq_le_p(pt + k * 8);
                uint64_t gva = ((uint64_t)i << 30) | ((uint64_t)j << 21) |
                               ((uint64_t)k << 12);
                uint64_t pa = pte & UINT64_C(0xfffffff000);
                if (!(pte & 1)) {
                    continue;
                }
                if (sizeof(header) + 2 * 0x280 + (pages + 1) * sizeof(record) >
                    96 * MiB) {
                    capped = true;
                    goto finish;
                }
                stq_le_p(record, gva);
                stq_le_p(record + 8, pa);
                /* Offline replay sees the same TA-consumed geometry/state
                 * as live replay, even after the guest reuses upload pages. */
                const uint8_t *retained = agx_ta_snapshot_page(s, context, raw, gva);
                if (retained) {
                    memcpy(record + 16, retained, 4096);
                } else if (address_space_read(&address_space_memory, pa, MEMTXATTRS_UNSPECIFIED,
                                              record + 16, 4096) != MEMTX_OK) {
                    goto done;
                }
                if (fwrite(record, 1, sizeof(record), out) != sizeof(record)) { goto done; }
                pages++;
            }
        }
    }
finish:
    stl_le_p(header + 12, (ta_valid ? 1 : 0) | (capped ? 2 : 0));
    stq_le_p(header + 48, pages);
    if (fseek(out, 0, SEEK_SET) ||
        fwrite(header, 1, sizeof(header), out) != sizeof(header)) {
        goto done;
    }
    result = fclose(out);
    out = NULL;
    if (result || !(json = fopen(json_path, "w"))) {
        goto done;
    }
    ta_hex = g_malloc(0x280 * 2 + 1);
    render_hex = g_malloc(0x280 * 2 + 1);
    for (unsigned i = 0; i < 0x280; i++) {
        snprintf(ta_hex + i * 2, 3, "%02x", dump_ta[i]);
        snprintf(render_hex + i * 2, 3, "%02x", raw[i]);
    }
    result = fprintf(json,
                     "{\n  \"magic\": \"AGXF\", \"version\": \"0x1\", "
                     "\"context_id\": \"0x%x\", \"flags\": \"0x%x\",\n"
                     "  \"catalogue_pa\": \"0x%llx\", \"pbe_target\": \"0x%llx\",\n"
                     "  \"type1_size\": \"0x280\", \"ta_size\": \"0x280\", "
                     "\"queue\": \"0x%x\", \"ta_queue\": \"0x%x\",\n"
                     "  \"display_mmu_table\": \"0x%llx\", \"record_count\": %llu, "
                     "\"capped\": %s, \"ta_valid\": %s,\n"
                     "  \"type1_command\": \"%s\",\n  \"ta_command\": \"%s\"\n}\n",
                     context, (unsigned)ldl_le_p(header + 12),
                     (unsigned long long)cat, (unsigned long long)target,
                     queue, s->agx_last_ta_queue[context],
                     (unsigned long long)s->disp_mmu_table, (unsigned long long)pages,
                     capped ? "true" : "false", ta_valid ? "true" : "false",
                     render_hex, ta_hex);
    close_result = fclose(json);
    json = NULL;
    ok = result >= 0 && !close_result;
done:
    if (out) {
        fclose(out);
    }
    if (json) {
        fclose(json);
    }
    if (!ok) {
        unlink(path);
        unlink(json_path);
        qemu_log_mask(LOG_GUEST_ERROR, "AGX FRAME dump %u failed: %s (I/O or guest memory read)\n",
                      number, path);
        return;
    }
    if (capped) {
        qemu_log_mask(LOG_UNIMP, "AGX FRAME dump %u capped at 96 MiB\n", number);
    }
    qemu_log_mask(LOG_UNIMP, "AGX FRAME dump %u %s %llu pages\n",
                  number, path, (unsigned long long)pages);
}

static bool agx_replay_enabled(void)
{
    static int enabled = -1;
    if (enabled < 0) {
        /* On by default; VPHONE_AGX_REPLAY=0 turns it off for comparisons. */
        const char *v = getenv("VPHONE_AGX_REPLAY");
        enabled = !v || strcmp(v, "0");
    }
    return enabled;
}

static bool agx_replay_thread_enabled(void)
{
    static int enabled = -1;
    if (enabled < 0) {
        /* On by default; VPHONE_AGX_REPLAY_THREAD=0 turns it off for bisects. */
        const char *v = getenv("VPHONE_AGX_REPLAY_THREAD");
        enabled = !v || strcmp(v, "0");
    }
    return enabled;
}

/* Snapshot of a 3D pass and its completion requirements, handed to the
 * dedicated off-BQL render thread. */
typedef struct AGXRenderJob {
    A7State *s;
    uint8_t ta[0x280];
    AGXReplaySnapshot *snapshot;
    uint64_t cat_base;
    uint64_t target;
    uint32_t width;
    uint32_t pitch;
    uint32_t height;
    bool is_clear;
    uint64_t scissor_base;

    /* Stamp to store upon rasterisation completion */
    bool has_stamp;
    uint64_t stamp_gva;
    uint32_t stamp_meta_addr;
    uint32_t stamp_value;
    uint32_t stamp_channel;
    uint32_t queue_block;

    /* Queue flow control to advance read_index upon completion */
    bool has_queue_ctrl;
    uint64_t queue_ctrl_gva;
    uint32_t queue_read_index;

    /* Completion event and interrupt for the kick */
    bool post_completion;
    unsigned commands;
    unsigned stamps;

    struct AGXRenderJob *next;
} AGXRenderJob;

#define AGX_PDS_HEAP UINT64_C(0x600000000)

/* Immutable TA-consumed metadata. Texture pixels and render targets remain
 * live: native TA consumes geometry/state, while 3D samples the textures. */
struct AGXReplaySnapshot {
    gint refs;
    GHashTable *pages;
};
static __thread AGXReplaySnapshot *agx_read_snapshot;
static __thread bool agx_capture_snapshot;

static const uint8_t *agx_ta_snapshot_page(A7State *s, uint32_t context,
                                           const uint8_t *raw, uint64_t gva)
{
    const uint8_t *ta = agx_find_ta(s, context, raw);
    if (!ta) { return NULL; }
    unsigned slot = (ta - &s->agx_ta_ring[context][0][0]) / 0x280;
    AGXReplaySnapshot *snapshot = s->agx_ta_snapshot[context][slot];
    return snapshot ? g_hash_table_lookup(snapshot->pages, &gva) : NULL;
}

static AGXReplaySnapshot *agx_snapshot_ref(AGXReplaySnapshot *snapshot)
{
    if (snapshot) { g_atomic_int_inc(&snapshot->refs); }
    return snapshot;
}
static void agx_snapshot_unref(AGXReplaySnapshot *snapshot)
{
    if (snapshot && g_atomic_int_dec_and_test(&snapshot->refs)) {
        g_hash_table_destroy(snapshot->pages);
        g_free(snapshot);
    }
}

static bool agx_ctx_read(A7State *s, uint64_t cat, uint64_t gva, void *buf, size_t n)
{
    uint8_t *out = buf;
    while (n) {
        hwaddr pa;
        size_t chunk = MIN(n, 0x1000 - (gva & 0xfff));
        uint64_t base = gva & ~UINT64_C(0xfff);
        uint8_t *cached = agx_read_snapshot ? g_hash_table_lookup(agx_read_snapshot->pages, &base) : NULL;
        if (!cached && agx_read_snapshot && agx_capture_snapshot &&
            g_hash_table_size(agx_read_snapshot->pages) < 1024) {
            hwaddr page_pa;
            if (agx_gpu_translate_ctx(s, cat, base, &page_pa)) {
                cached = g_malloc(4096);
                if (address_space_read(&address_space_memory, page_pa, MEMTXATTRS_UNSPECIFIED,
                                       cached, 4096) == MEMTX_OK) {
                    uint64_t *key = g_new(uint64_t, 1);
                    *key = base;
                    g_hash_table_insert(agx_read_snapshot->pages, key, cached);
                } else {
                    g_free(cached);
                    cached = NULL;
                }
            }
        }
        if (cached) {
            memcpy(out, cached + (gva & 0xfff), chunk);
        } else if (gva >= (UINT64_C(1) << 40) || !agx_gpu_translate_ctx(s, cat, gva, &pa) ||
            address_space_read(&address_space_memory, pa, MEMTXATTRS_UNSPECIFIED,
                               out, chunk) != MEMTX_OK) {
            return false;
        }
        out += chunk;
        gva += chunk;
        n -= chunk;
    }
    return true;
}

/* GPU-rendered surface layout. Display surfaces are linear 2560-byte rows in
 * physical memory, mapped as 16-row strips (40 KiB) at 64 KiB-aligned GVAs,
 * which leaves an unmapped hole at base + 0xa000. Other render targets
 * (offscreen layers) are mapped densely with a 16-pixel aligned pitch. */
typedef struct AGXSurface {
    uint64_t base;
    uint32_t pitch;
    bool strip;
} AGXSurface;

static void agx_surface_layout(A7State *s, uint64_t cat, uint64_t base, uint32_t w,
                               AGXSurface *out)
{
    hwaddr pa;
    out->base = base;
    /* Only full-width, 64 KiB-aligned display surfaces use 16-row strips.
     * For small/offset glyph allocations these probes inspect unrelated
     * neighbours, whose holes must not select a 2560-byte display pitch. */
    out->strip = w == 640 && !(base & 0xffff) &&
                 agx_gpu_translate_ctx(s, cat, base + 0x9000, &pa) &&
                 !agx_gpu_translate_ctx(s, cat, base + 0xa000, &pa);
    out->pitch = out->strip ? 2560 : ((w + 15) & ~15u) * 4;
}

static uint64_t agx_surface_gva(const AGXSurface *sf, unsigned x, unsigned y)
{
    if (sf->strip) {
        return sf->base + (uint64_t)(y >> 4) * 0x10000 + (y & 15) * sf->pitch + x * 4;
    }
    return sf->base + (uint64_t)y * sf->pitch + x * 4;
}

typedef struct AGXTexture {
    uint64_t addr;
    uint32_t w, h, stride;
    bool strip;                 /* GPU-rendered surface: 16-row strips per 64 KiB */
    bool twiddled;              /* uploaded 2D texture, Y-first Morton order */
} AGXTexture;

enum { AGX_TEX_NONE, AGX_TEX_FOUND, AGX_TEX_VIBRANCY };

static bool agx_replay_decode_tsw(A7State *s, uint64_t cat, const uint32_t *tsw,
                                  AGXTexture *tex)
{
    uint64_t q = tsw[2] | ((uint64_t)tsw[3] << 32);
    hwaddr pa;

    if (tsw[0] >> 28 != 6) {
        return false;
    }
    tex->w = ((tsw[1] >> 2) & 0x3fff) + 1;
    tex->h = (tsw[1] >> 16) + 1;
    tex->addr = ((q >> 16) & UINT64_C(0x3ffffffff)) << 2;
    /* Layout 1 or display surfaces with hole at +0xa000 (agx_surface_layout);
     * otherwise uploaded linear textures. */
    AGXSurface sf;
    agx_surface_layout(s, cat, tex->addr, tex->w, &sf);
    tex->strip = sf.strip;
    tex->twiddled = !sf.strip && (tsw[0] & 0x8007) == 0x8001;
    if (sf.strip) {
        tex->stride = sf.pitch;
    } else if ((tsw[0] & 7) == 1) {
        tex->stride = ((tex->w + 15) & ~15u) * 4;
    } else {
        /* Linear textures carry (pitch in pixels - 1) in word 2 bits [15:0]. */
        tex->stride = ((tsw[2] & 0xffff) + 1) * 4;
        if (tex->stride < tex->w * 4) {
            tex->stride = ((tex->w + 15) & ~15u) * 4;
        }
    }
    return tex->addr && agx_gpu_translate_ctx(s, cat, tex->addr, &pa);
}

/* PPP words that point into the PDS heap reference fragment PDS data: u64
 * pointers to blocks holding sampler state, texture state words (TSW) and
 * shader constants. Draws whose constants are the Rec.709 luma weights use
 * CA's darkenSourceOver / lightenSourceOver legibility shaders (agy report
 * C8): the source is attenuated by the destination luminance before src-over. */
static int agx_replay_texture(A7State *s, uint64_t cat, const uint32_t *ppp,
                              unsigned n, AGXTexture *tex)
{
    static const uint32_t luma[3] = { 0x3d93a000, 0x3e59a000, 0x3f372000 };
    uint32_t blocks[4][12];
    unsigned count = 0;

    for (unsigned i = 0; i < n; i++) {
        uint32_t data[8];
        if ((ppp[i] & 0xc0000000) != 0x80000000 ||
            !agx_ctx_read(s, cat, AGX_PDS_HEAP | ppp[i], data, sizeof(data))) {
            continue;
        }
        for (unsigned k = 0; k < 8 && count < ARRAY_SIZE(blocks); k += 2) {
            uint64_t ptr = data[k] | ((uint64_t)data[k + 1] << 32);
            /* The pointer list is contiguous from offset 0. Stop at the first
             * non-pointer: the rest of the 8-word read can be the NEXT draw's
             * PDS data (status-bar blur backdrop borrowing the "Searching"
             * texture, agy B4). */
            if (ptr < 0x1000 || ptr >= (UINT64_C(1) << 40)) {
                break;
            }
            if (!agx_ctx_read(s, cat, ptr, blocks[count], sizeof(blocks[count]))) {
                break;
            }
            count++;
        }
    }
    for (unsigned b = 0; b < count; b++) {
        /* Descriptor blocks in the uniform heap are 16-byte aligned. A valid
         * TSW is located strictly at offset 0 (stand-alone TSW) or offset 4
         * (preceded by a 4-word header or sampler). Arbitrary offsets like
         * k = 6 overran into adjacent allocations and caused the status-bar
         * blur backdrop to borrow "Searching..." (agy B4/D7). The one
         * exception is the vibrancy block: luma weights, four sampler words,
         * then the TSW at word 7 (handover section 16), only accepted when
         * the block really starts with the luma weights. */
        static const unsigned offsets[] = { 0, 4, 7 };
        for (unsigned o = 0; o < ARRAY_SIZE(offsets); o++) {
            unsigned k = offsets[o];
            if (k == 7 && memcmp(blocks[b], luma, sizeof(luma))) {
                continue;
            }
            if (agx_replay_decode_tsw(s, cat, blocks[b] + k, tex)) {
                /* A stand-alone TSW is only 16 bytes. Constants following
                 * it can belong to the next draw; their luma weights must
                 * not turn an opaque wallpaper/icon draw into vibrancy. */
                return !memcmp(blocks[b], luma, sizeof(luma)) ?
                       AGX_TEX_VIBRANCY : AGX_TEX_FOUND;
            }
        }
    }
    return AGX_TEX_NONE;
}

static uint64_t agx_texel_gva(const AGXTexture *tex, uint32_t x, uint32_t y)
{
    if (tex->twiddled) {
        uint64_t offset = 0;
        unsigned bit = 0;
        /* Interleave through the smaller padded dimension, then append
         * the remaining coordinate bits for rectangular textures. */
        for (unsigned mask = 1; mask < tex->w || mask < tex->h; mask <<= 1) {
            if (mask < tex->h) { offset |= (uint64_t)!!(y & mask) << bit++; }
            if (mask < tex->w) { offset |= (uint64_t)!!(x & mask) << bit++; }
        }
        return tex->addr + offset * 4;
    }
    if (tex->strip) {
        return tex->addr + (uint64_t)(y >> 4) * 0x10000 + (y & 15) * tex->stride + x * 4;
    }
    return tex->addr + (uint64_t)y * tex->stride + x * 4;
}

/* A Morton row is scattered within 4 KiB tiles. Cache one tile while
 * gathering it, instead of walking the GPU tables for every pixel. */
static bool agx_texture_row(A7State *s, uint64_t cat, const AGXTexture *tex,
                            unsigned y, uint32_t *out)
{
    if (!tex->twiddled) {
        return agx_ctx_read(s, cat, agx_texel_gva(tex, 0, y), out, tex->w * 4);
    }
    uint8_t page[4096];
    uint64_t cached = UINT64_MAX;
    for (unsigned x = 0; x < tex->w; x++) {
        uint64_t gva = agx_texel_gva(tex, x, y), base = gva & ~UINT64_C(0xfff);
        if (base != cached) {
            if (!agx_ctx_read(s, cat, base, page, sizeof(page))) { return false; }
            cached = base;
        }
        out[x] = ldl_le_p(page + (gva & 0xfff));
    }
    return true;
}

/* darkenSourceOver / lightenSourceOver: scale the (premultiplied) source by
 * Y^4 of the destination luminance (dark shadows) or (1-Y)^4 (light halos),
 * then src-over. Light sources are recognised by their colour. */
static uint32_t agx_blend_vibrancy(uint32_t dst, uint32_t src)
{
    uint32_t db = dst & 0xff, dg = (dst >> 8) & 0xff, dr = (dst >> 16) & 0xff;
    uint32_t sb = src & 0xff, sg = (src >> 8) & 0xff, sr = (src >> 16) & 0xff, sa = src >> 24;
    uint32_t y = (54 * dr + 183 * dg + 19 * db) >> 8;
    uint32_t y1 = sr + sg + sb > 384 ? 255 - y : y;
    uint32_t y2 = y1 * y1 / 255, f = y2 * y2 / 255;
    uint32_t out = 0;

    sb = sb * f / 255; sg = sg * f / 255; sr = sr * f / 255; sa = sa * f / 255;
    const uint32_t s4[4] = { sb, sg, sr, sa }, d4[4] = { db, dg, dr, dst >> 24 };
    for (unsigned c = 0; c < 4; c++) {
        out |= MIN(s4[c] + d4[c] * (255 - sa) / 255, 255u) << (c * 8);
    }
    return out;
}

/* Premultiplied BGRA src-over, alpha included (offscreen layers keep it). */
static uint32_t agx_blend_over(uint32_t dst, uint32_t src)
{
    uint32_t a = src >> 24, out = 0;
    for (unsigned c = 0; c < 32; c += 8) {
        uint32_t v = ((src >> c) & 0xff) + ((dst >> c) & 0xff) * (255 - a) / 255;
        out |= MIN(v, 255u) << c;
    }
    return out;
}

/* CA fragment shaders recognised by an FNV-1a hash of the first 64 bytes of
 * their USC code (PPP word 7 -> state at 0x6_0000_0000 -> USC offset in the
 * 0x7_0000_0000 heap). The compiled code is identical across boots of this
 * iOS build. */
#define AGX_USC_DEST_IN UINT64_C(0x4ee01acc47551344)  /* mask: D *= texel alpha */

/* CoreAnimation narrow Gaussian blur shaders (sigma = 3.5, 6 bilinear tap pairs).
 * Recovered from CA::OGL::BlurState::narrow_blur / narrowBlurParameters in QuartzCore. */
#define AGX_USC_BLUR_1 UINT64_C(0x115ac3fe908dec8a)
#define AGX_USC_BLUR_2 UINT64_C(0x90d330877114e752)
#define AGX_USC_BLUR_3 UINT64_C(0xb443c1f6a714b429)
#define AGX_USC_BLUR_4 UINT64_C(0x21377cafd3277883)
#define AGX_USC_BLUR_5 UINT64_C(0x95631b61e7c1be31)
#define AGX_USC_BLUR_6 UINT64_C(0x145dbc3101d94f8c)
#define AGX_USC_BLUR_7 UINT64_C(0xf0c17d1e30ec2393)
#define AGX_USC_BLUR_8 UINT64_C(0x69f1605719a006b2)
#define AGX_USC_BLUR_9 UINT64_C(0xa4c025e0be54470a)

static inline bool agx_is_blur_program(uint64_t prog)
{
    return prog == AGX_USC_BLUR_1 || prog == AGX_USC_BLUR_2 ||
           prog == AGX_USC_BLUR_3 || prog == AGX_USC_BLUR_4 ||
           prog == AGX_USC_BLUR_5 || prog == AGX_USC_BLUR_6 ||
           prog == AGX_USC_BLUR_7 || prog == AGX_USC_BLUR_8 ||
           prog == AGX_USC_BLUR_9;
}

static const struct {
    float offset;
    float weight;
} agx_blur_taps[] = {
    { 0.6575f, 0.1666f },
    { 2.4492f, 0.1759f },
    { 4.4092f, 0.1005f },
    { 6.3704f, 0.0417f },
    { 8.3332f, 0.0126f },
    { 10.2979f, 0.0027f },
};

static inline uint32_t agx_bilinear_interpolate(uint32_t c00, uint32_t c10,
                                                uint32_t c01, uint32_t c11,
                                                float fx, float fy)
{
    uint32_t out = 0;
    for (unsigned c = 0; c < 32; c += 8) {
        float b00 = (float)((c00 >> c) & 0xff);
        float b10 = (float)((c10 >> c) & 0xff);
        float b01 = (float)((c01 >> c) & 0xff);
        float b11 = (float)((c11 >> c) & 0xff);
        float val = (b00 * (1.0f - fx) + b10 * fx) * (1.0f - fy) +
                    (b01 * (1.0f - fx) + b11 * fx) * fy;
        int iv = (int)lroundf(val);
        if (iv < 0) iv = 0;
        if (iv > 255) iv = 255;
        out |= (uint32_t)iv << c;
    }
    return out;
}

static uint32_t agx_sample_bilinear(const uint32_t *buf, uint32_t w, uint32_t h, float u, float v)
{
    float fu = u - 0.5f;
    float fv = v - 0.5f;
    if (fu < 0.0f) fu = 0.0f;
    if (fu > (float)(w - 1)) fu = (float)(w - 1);
    if (fv < 0.0f) fv = 0.0f;
    if (fv > (float)(h - 1)) fv = (float)(h - 1);

    uint32_t x0 = (uint32_t)fu;
    uint32_t y0 = (uint32_t)fv;
    uint32_t x1 = x0 + 1 < w ? x0 + 1 : x0;
    uint32_t y1 = y0 + 1 < h ? y0 + 1 : y0;

    float fx = fu - (float)x0;
    float fy = fv - (float)y0;

    uint32_t c00 = buf[y0 * w + x0];
    uint32_t c10 = buf[y0 * w + x1];
    uint32_t c01 = buf[y1 * w + x0];
    uint32_t c11 = buf[y1 * w + x1];

    return agx_bilinear_interpolate(c00, c10, c01, c11, fx, fy);
}

static void agx_blur_texture(uint32_t *buf_in, uint32_t *buf_tmp, uint32_t w, uint32_t h)
{
    /* Pass 1: Horizontal blur from buf_in to buf_tmp */
    for (uint32_t y = 0; y < h; y++) {
        for (uint32_t x = 0; x < w; x++) {
            float acc[4] = { 0.0f, 0.0f, 0.0f, 0.0f };
            for (size_t t = 0; t < ARRAY_SIZE(agx_blur_taps); t++) {
                float off = agx_blur_taps[t].offset;
                float weight = agx_blur_taps[t].weight;
                uint32_t s_pos = agx_sample_bilinear(buf_in, w, h, (float)x + 0.5f + off, (float)y + 0.5f);
                uint32_t s_neg = agx_sample_bilinear(buf_in, w, h, (float)x + 0.5f - off, (float)y + 0.5f);
                for (unsigned c = 0; c < 4; c++) {
                    float cp = (float)((s_pos >> (c * 8)) & 0xff);
                    float cn = (float)((s_neg >> (c * 8)) & 0xff);
                    acc[c] += (cp + cn) * weight;
                }
            }
            uint32_t out = 0;
            for (unsigned c = 0; c < 4; c++) {
                int iv = (int)lroundf(acc[c]);
                if (iv < 0) iv = 0;
                if (iv > 255) iv = 255;
                out |= (uint32_t)iv << (c * 8);
            }
            buf_tmp[y * w + x] = out;
        }
    }

    /* Pass 2: Vertical blur from buf_tmp to buf_in */
    for (uint32_t y = 0; y < h; y++) {
        for (uint32_t x = 0; x < w; x++) {
            float acc[4] = { 0.0f, 0.0f, 0.0f, 0.0f };
            for (size_t t = 0; t < ARRAY_SIZE(agx_blur_taps); t++) {
                float off = agx_blur_taps[t].offset;
                float weight = agx_blur_taps[t].weight;
                uint32_t s_pos = agx_sample_bilinear(buf_tmp, w, h, (float)x + 0.5f, (float)y + 0.5f + off);
                uint32_t s_neg = agx_sample_bilinear(buf_tmp, w, h, (float)x + 0.5f, (float)y + 0.5f - off);
                for (unsigned c = 0; c < 4; c++) {
                    float cp = (float)((s_pos >> (c * 8)) & 0xff);
                    float cn = (float)((s_neg >> (c * 8)) & 0xff);
                    acc[c] += (cp + cn) * weight;
                }
            }
            uint32_t out = 0;
            for (unsigned c = 0; c < 4; c++) {
                int iv = (int)lroundf(acc[c]);
                if (iv < 0) iv = 0;
                if (iv > 255) iv = 255;
                out |= (uint32_t)iv << (c * 8);
            }
            buf_in[y * w + x] = out;
        }
    }
}

static uint64_t agx_replay_program(A7State *s, uint64_t cat, const uint32_t *ppp, unsigned n)
{
    uint32_t off;
    uint8_t code[64];
    uint64_t h = UINT64_C(0xcbf29ce484222325);
    if (n < 8 || !ppp[7] ||
        !agx_ctx_read(s, cat, UINT64_C(0x600000000) | ppp[7], &off, 4) ||
        !agx_ctx_read(s, cat, UINT64_C(0x700000000) | off, code, sizeof(code))) {
        return 0;
    }
    for (unsigned i = 0; i < sizeof(code); i++) {
        h = (h ^ code[i]) * UINT64_C(0x100000001b3);
    }
    return h;
}

/* High-level replay of a CoreAnimation frame: walk the TA VDM stream and
 * draw each axis-aligned quad (solid vertex colour or one BGRA texture,
 * premultiplied src-over) into the 640x1136 colour target. */
/* The orthographic vertex matrix (column-major 4x4) referenced from the
 * vertex PDS data maps CA's screen-space positions to NDC; returns
 * {m00, m11, m30, m31}. */
static bool agx_replay_matrix(A7State *s, uint64_t cat, uint64_t vs_data, float m4[4])
{
    static const unsigned zeros[] = { 1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 14 };
    uint32_t words[20];
    if (!agx_ctx_read(s, cat, vs_data, words, sizeof(words))) {
        return false;
    }
    for (unsigned k = 4; k < 20; k += 2) {
        uint64_t ptr = words[k] | ((uint64_t)words[k + 1] << 32);
        float m[16];
        bool ortho = true;
        if (ptr < 0x1000 || ptr >= (UINT64_C(1) << 40) ||
            !agx_ctx_read(s, cat, ptr, m, sizeof(m))) {
            continue;
        }
        for (unsigned z = 0; z < ARRAY_SIZE(zeros); z++) {
            ortho &= m[zeros[z]] == 0;
        }
        if (ortho && fabsf(m[15] - 1) < 1e-6f && m[0] != 0 && m[5] != 0) {
            m4[0] = m[0]; m4[1] = m[5]; m4[2] = m[12]; m4[3] = m[13];
            return true;
        }
    }
    return false;
}

/* PPP viewport block: x scale, x offset, y scale, y offset, 0.5, 0.5. */
static bool agx_replay_viewport(const uint32_t *ppp, unsigned n, float vp[4])
{
    for (unsigned i = 0; i + 5 < n; i++) {
        if (ppp[i] == ppp[i + 1] && ppp[i + 2] == ppp[i + 3] &&
            ppp[i + 4] == 0x3f000000 && ppp[i + 5] == 0x3f000000 &&
            ppp[i] >= 0x3f800000 && ppp[i] < 0x46000000) {
            for (unsigned j = 0; j < 4; j++) {
                memcpy(&vp[j], &ppp[i + j], 4);
            }
            return true;
        }
    }
    return false;
}

/* Clip coverage, not the interpolation domain: an off-screen quad must keep
 * sampling the same texels as the corresponding crop of its full raster. */
static float agx_quad_coordinate(float pixel, float low, float high,
                                  float uv0, float uv1)
{
    return uv0 + (uv1 - uv0) * (pixel + 0.5f - low) / (high - low);
}

/* Preserve the association between vertex positions and texture coordinates.
 * CA also submits quads with U along screen Y (and even constant V). */
static bool agx_quad_affine(const float xy[3][2], const float uv[3][2], float out[6])
{
    float dx1 = xy[1][0] - xy[0][0], dy1 = xy[1][1] - xy[0][1];
    float dx2 = xy[2][0] - xy[0][0], dy2 = xy[2][1] - xy[0][1];
    float det = dx1 * dy2 - dx2 * dy1;
    if (!isfinite(det) || fabsf(det) < 1e-8f) {
        return false;
    }
    for (unsigned c = 0; c < 2; c++) {
        float d1 = uv[1][c] - uv[0][c], d2 = uv[2][c] - uv[0][c];
        out[c * 3] = (d1 * dy2 - d2 * dy1) / det;
        out[c * 3 + 1] = (dx1 * d2 - dx2 * d1) / det;
        out[c * 3 + 2] = uv[0][c] - out[c * 3] * xy[0][0] - out[c * 3 + 1] * xy[0][1];
        if (!isfinite(out[c * 3]) || !isfinite(out[c * 3 + 1]) || !isfinite(out[c * 3 + 2])) {
            return false;
        }
    }
    return true;
}

static AGXReplaySnapshot *agx_capture_ta(A7State *s, uint64_t cat, const uint8_t *ta)
{
    AGXReplaySnapshot *snapshot = g_new0(AGXReplaySnapshot, 1);
    snapshot->refs = 1;
    snapshot->pages = g_hash_table_new_full(g_int64_hash, g_int64_equal, g_free, g_free);
    agx_read_snapshot = snapshot;
    agx_capture_snapshot = true;
    uint64_t pc = ldq_le_p(ta + 0x68), vs_data = 0;
    uint32_t ppp[64] = {0}, n = 0;
    for (unsigned guard = 0; guard < 4096; guard++) {
        uint32_t w[5];
        if (!agx_ctx_read(s, cat, pc, w, 4)) { break; }
        unsigned kind = w[0] >> 29;
        if (kind == 6) { break; }
        if (kind == 4) {
            if (!agx_ctx_read(s, cat, pc + 4, w + 1, 4)) { break; }
            pc = ((uint64_t)(w[0] & 0xff) << 32) | (w[1] & ~3u);
        } else if (kind == 2) {
            unsigned extra = !!(w[0] & (1u << 28)) + !!(w[0] & (1u << 27)) + 3 * !!(w[0] & (1u << 26));
            if ((w[0] & (1u << 27)) &&
                agx_ctx_read(s, cat, pc + 4 + ((w[0] & (1u << 28)) ? 4 : 0), w + 1, 4)) {
                vs_data = AGX_PDS_HEAP | w[1];
            }
            pc += 4 * (1 + extra);
        } else if (kind == 1) {
            pc += 12;
        } else if (kind == 0) {
            if (!agx_ctx_read(s, cat, pc + 4, w + 1, 4)) { break; }
            uint64_t gva = ((uint64_t)(w[0] & 0xff) << 32) | (w[1] & ~3u);
            n = MIN((w[0] >> 8) & 0xfffff, ARRAY_SIZE(ppp));
            if (!agx_ctx_read(s, cat, gva, ppp, n * 4)) { n = 0; }
            pc += 8;
        } else if (kind == 3) {
            uint32_t data[20];
            uint8_t vertices[64 * 64];
            if (!agx_ctx_read(s, cat, pc + 4, w + 1, 8)) { break; }
            if (vs_data && agx_ctx_read(s, cat, vs_data, data, sizeof(data))) {
                unsigned stride = data[0], count = MIN(w[1], 64u);
                if (stride >= 12 && stride <= 64) {
                    agx_ctx_read(s, cat, data[2] | ((uint64_t)data[3] << 32), vertices, count * stride);
                }
                /* Probe matrix candidates without retaining unrelated pages. */
                for (unsigned k = 4; k < 20; k += 2) {
                    uint64_t ptr = data[k] | ((uint64_t)data[k + 1] << 32);
                    float m[16];
                    agx_capture_snapshot = false;
                    bool ok = ptr >= 0x1000 && ptr < (UINT64_C(1) << 40) && agx_ctx_read(s, cat, ptr, m, sizeof(m));
                    agx_capture_snapshot = true;
                    static const unsigned zeros[] = {1,2,3,4,6,7,8,9,10,11,14};
                    if (ok) {
                        for (unsigned j = 0; j < ARRAY_SIZE(zeros); j++) { ok &= m[zeros[j]] == 0; }
                        if (ok && fabsf(m[15] - 1) < 1e-6f && m[0] != 0 && m[5] != 0) {
                            agx_ctx_read(s, cat, ptr, m, sizeof(m));
                            break;
                        }
                    }
                }
                AGXTexture tex;
                agx_replay_texture(s, cat, ppp, n, &tex);
                agx_replay_program(s, cat, ppp, n);
            }
            pc += 12;
        } else { break; }
    }
    agx_read_snapshot = NULL;
    agx_capture_snapshot = false;
    return snapshot;
}

static unsigned agx_audit_clock(A7State *s, uint64_t cat, AGXSurface *sf)
{
    uint32_t pixels[400];
    unsigned bright = 0;
    AGXReplaySnapshot *saved = agx_read_snapshot;
    agx_read_snapshot = NULL;
    for (unsigned y = 96; y < 304; y += 4) {
        if (agx_ctx_read(s, cat, agx_surface_gva(sf, 120, y), pixels, sizeof(pixels))) {
            for (unsigned x = 0; x < 400; x += 4) {
                uint32_t p = pixels[x];
                bright += (p & 255) > 200 && ((p >> 8) & 255) > 200 && ((p >> 16) & 255) > 200;
            }
        }
    }
    agx_read_snapshot = saved;
    return bright;
}

static void agx_replay_frame(A7State *s, uint64_t cat, const uint8_t *ta,
                             uint64_t target, uint32_t width, uint32_t height,
                             bool is_clear, uint32_t pitch, uint64_t scissor_base)
{
    /* Thread-safety: row and texrow are thread-local (__thread) buffers,
     * ensuring safe re-entrancy between the dedicated off-BQL render thread
     * and the inline BQL fallback. Guest memory is accessed via
     * address_space_read/write on address_space_memory under RCU read lock. */
    static __thread uint32_t row[640 * 4], texrow[0x4000], texrow1[0x4000];
    uint64_t pc = ldq_le_p(ta + 0x68), vs_data = 0;
    uint32_t ppp[64];
    unsigned ppp_count = 0, draws = 0;

    width = MIN(width, (uint32_t)ARRAY_SIZE(row));
    height = MIN(height, 4096u);
    AGXSurface sf;
    agx_surface_layout(s, cat, target, width, &sf);
    if (!sf.strip && pitch >= width * 4) { sf.pitch = pitch; }
    bool audit = getenv("VPHONE_AGX_REPLAY_AUDIT") && target >= UINT64_C(0x400000000);
    unsigned before_clock = audit ? agx_audit_clock(s, cat, &sf) : 0;

    if (is_clear) {
        uint32_t zero_row[ARRAY_SIZE(row)];
        memset(zero_row, 0, sizeof(zero_row));
        for (uint32_t y = 0; y < height; y++) {
            agx_ctx_write(s, cat, agx_surface_gva(&sf, 0, y), zero_row, width * 4);
        }
    }

    /* Default viewport covers the pass; PPP state updates it. */
    float vp[4] = { width / 2.0f, width / 2.0f, height / 2.0f, height / 2.0f };
    bool scenable = false;
    uint16_t scindex = 0;
    bool has_rc = false;
    int rc_x0 = 0, rc_y0 = 0, rc_x1 = width, rc_y1 = height;
    for (unsigned guard = 0; guard < 4096; guard++) {
        uint32_t w[5];
        if (!agx_ctx_read(s, cat, pc, w, 4)) {
            break;
        }
        unsigned kind = w[0] >> 29;
        if (kind == 6) {
            break;
        } else if (kind == 4) {
            if (!agx_ctx_read(s, cat, pc + 4, w + 1, 4)) {
                break;
            }
            pc = ((uint64_t)(w[0] & 0xff) << 32) | (w[1] & ~3u);
        } else if (kind == 2) {
            /* bit 28: cut index; bit 27: vertex PDS data; bit 26: vertex PDS
             * code plus two state words. */
            unsigned extra = !!(w[0] & (1u << 28)) + !!(w[0] & (1u << 27)) +
                             3 * !!(w[0] & (1u << 26));
            if ((w[0] & (1u << 27)) &&
                agx_ctx_read(s, cat, pc + 4 + ((w[0] & (1u << 28)) ? 4 : 0), w + 1, 4)) {
                vs_data = AGX_PDS_HEAP | w[1];
            }
            pc += 4 * (1 + extra);
        } else if (kind == 1) {
            pc += 12;
        } else if (kind == 0) {
            uint64_t gva;
            if (!agx_ctx_read(s, cat, pc + 4, w + 1, 4)) {
                break;
            }
            gva = ((uint64_t)(w[0] & 0xff) << 32) | (w[1] & ~3u);
            ppp_count = MIN((w[0] >> 8) & 0xfffff, ARRAY_SIZE(ppp));
            if (!agx_ctx_read(s, cat, gva, ppp, ppp_count * 4)) {
                ppp_count = 0;
            }
            agx_replay_viewport(ppp, ppp_count, vp);
            if (ppp_count > 0) {
                uint32_t hdr = ppp[0];
                unsigned idx = 1;
                if (hdr & (1 << 0)) {
                    if (idx < ppp_count) {
                        scenable = (ppp[idx++] & (1 << 16)) != 0;
                    }
                }
                if ((hdr & (1 << 1)) && idx < ppp_count) { idx++; }
                if ((hdr & (1 << 2)) && idx < ppp_count) { idx++; }
                if ((hdr & (1 << 3)) && idx < ppp_count) { idx++; }
                if ((hdr & (1 << 4)) && idx < ppp_count) { idx++; }
                if (hdr & (1 << 5)) {
                    if (idx < ppp_count) {
                        scindex = ppp[idx++] & 0xffff;
                    }
                }
                if (hdr & (1 << 6)) { idx += 4; }
                if (hdr & (1 << 7)) { idx += 1; }
                if (hdr & (1 << 8)) { idx += 1; }
                if (hdr & (1 << 9)) { idx += 1; }
                if (hdr & (1 << 10)) {
                    if (idx + 1 < ppp_count) {
                        uint32_t rc0 = ppp[idx++];
                        uint32_t rc1 = ppp[idx++];
                        if ((rc0 >> 31) & 1) {
                            has_rc = true;
                            rc_x0 = ((rc0 >> 16) & 0x1ff) * 32;
                            rc_x1 = ((rc0 & 0x1ff) + 1) * 32;
                            rc_y0 = ((rc1 >> 16) & 0x1ff) * 32;
                            rc_y1 = ((rc1 & 0x1ff) + 1) * 32;
                        } else {
                            has_rc = false;
                        }
                    }
                }
            }
            pc += 8;
        } else if (kind == 3) {
            uint32_t stride, count;
            uint64_t buf;
            AGXTexture tex;

            if (!agx_ctx_read(s, cat, pc + 4, w + 1, 8)) {
                break;
            }
            pc += 12;
            count = MIN(w[1], 64u);
            if (!vs_data || !agx_ctx_read(s, cat, vs_data, &stride, 4) ||
                !agx_ctx_read(s, cat, vs_data + 8, &buf, 8) || stride < 12 || stride > 64) {
                continue;
            }

            int found = agx_replay_texture(s, cat, ppp, ppp_count, &tex);
            uint64_t prog = agx_replay_program(s, cat, ppp, ppp_count);
            bool is_blur_prog = agx_is_blur_program(prog);
            bool clip_space = stride == 16 && prog == UINT64_C(0x789b8f08a3f3a8cd);
            if ((stride == 16 && !clip_space) ||
                (found == AGX_TEX_NONE && stride != 20 && stride != 28 && !(stride == 12 && is_blur_prog)) ||
                (found != AGX_TEX_NONE && tex.w * 4 > sizeof(texrow))) {
                continue;
            }
            bool textured = found != AGX_TEX_NONE;
            bool vibrancy = found == AGX_TEX_VIBRANCY;
            bool dest_in = textured && prog == AGX_USC_DEST_IN;
            bool is_blur = textured && is_blur_prog;
            uint32_t *blurred_tex = NULL;
            if (is_blur && tex.w > 0 && tex.h > 0 && tex.w <= 2048 && tex.h <= 2048) {
                uint32_t *raw_tex = g_malloc((size_t)tex.w * tex.h * sizeof(uint32_t));
                uint32_t *tmp_tex = g_malloc((size_t)tex.w * tex.h * sizeof(uint32_t));
                if (raw_tex && tmp_tex) {
                    bool read_ok = true;
                    for (uint32_t ty = 0; ty < tex.h; ty++) {
                        if (!agx_texture_row(s, cat, &tex, ty, raw_tex + (size_t)ty * tex.w)) {
                            read_ok = false;
                            break;
                        }
                    }
                    if (read_ok) {
                        agx_blur_texture(raw_tex, tmp_tex, tex.w, tex.h);
                        blurred_tex = raw_tex;
                        raw_tex = NULL;
                    }
                }
                g_free(raw_tex);
                g_free(tmp_tex);
            }
            unsigned step = (count == 4) ? 4 : 6;
            bool any_drawn = false;
            int clip_x0 = 0, clip_y0 = 0, clip_x1 = (int)width, clip_y1 = (int)height;
            if (scenable && scissor_base) {
                uint32_t sc_words[2];
                if (agx_ctx_read(s, cat, scissor_base + (uint64_t)scindex * 8, sc_words, sizeof(sc_words))) {
                    clip_x0 = MAX(clip_x0, (int)(sc_words[0] >> 16));
                    clip_x1 = MIN(clip_x1, (int)(sc_words[0] & 0xffff));
                    clip_y0 = MAX(clip_y0, (int)(sc_words[1] >> 16));
                    clip_y1 = MIN(clip_y1, (int)(sc_words[1] & 0xffff));
                }
            }
            if (has_rc) {
                clip_x0 = MAX(clip_x0, rc_x0);
                clip_x1 = MIN(clip_x1, rc_x1);
                clip_y0 = MAX(clip_y0, rc_y0);
                clip_y1 = MIN(clip_y1, rc_y1);
            }
            for (unsigned q_start = 0; q_start + step <= count; q_start += step) {
                float minx = 1e9, miny = 1e9, maxx = -1e9, maxy = -1e9;
                float m4[4];
                bool transformed = (stride != 12) && agx_replay_matrix(s, cat, vs_data, m4);
                float quad_xy[3][2], quad_uv[3][2], uv_map[6];
                bool affine = false;
                unsigned quad_vertices = 0;

                for (unsigned i = q_start; i < q_start + step; i++) {
                    float xy[2];
                    if (!agx_ctx_read(s, cat, buf + (uint64_t)i * stride, xy, 8)) {
                        break;
                    }
                    if (clip_space) {
                        xy[0] = xy[0] * vp[0] + vp[1];
                        xy[1] = xy[1] * vp[2] + vp[3];
                    } else if (transformed) {
                        /* screen-space position -> NDC -> pass pixels */
                        xy[0] = (m4[0] * xy[0] + m4[2]) * vp[0] + vp[1];
                        xy[1] = (m4[1] * xy[1] + m4[3]) * vp[2] + vp[3];
                    }
                    minx = MIN(minx, xy[0]); maxx = MAX(maxx, xy[0]);
                    miny = MIN(miny, xy[1]); maxy = MAX(maxy, xy[1]);
                    unsigned sub_idx = i - q_start;
                    if (sub_idx < 3) {
                        quad_xy[sub_idx][0] = xy[0];
                        quad_xy[sub_idx][1] = xy[1];
                    }
                }

                /* Vertex layouts seen from CA: stride 20 = position + BGRA colour;
                 * 24 = position + normalised u,v; 28 = position + BGRA tint +
                 * u,v in texels. */
                float u0 = 0, u1 = 1, v0 = 0, v1 = 1;
                uint32_t tint = 0xffffffff;
                uint32_t colour = 0xff000000;
                if (stride == 20 || (stride == 28 && !textured)) {
                    agx_ctx_read(s, cat, buf + (uint64_t)q_start * stride + 16, &colour, 4);
                } else if (stride == 24 || stride == 28 || clip_space) {
                    unsigned uv = clip_space ? 8 : (stride == 28 ? 20 : 16);
                    u0 = v0 = 1e9; u1 = v1 = -1e9;
                    if (stride == 28) {
                        agx_ctx_read(s, cat, buf + (uint64_t)q_start * stride + 16, &tint, 4);
                    }
                    for (unsigned i = q_start; i < q_start + step; i++) {
                        float t[2];
                        if (!agx_ctx_read(s, cat, buf + (uint64_t)i * stride + uv, t, 8)) {
                            break;
                        }
                        u0 = MIN(u0, t[0]); u1 = MAX(u1, t[0]);
                        v0 = MIN(v0, t[1]); v1 = MAX(v1, t[1]);
                        unsigned sub_idx = i - q_start;
                        if (sub_idx < 3) {
                            quad_vertices++;
                            quad_uv[sub_idx][0] = t[0];
                            quad_uv[sub_idx][1] = t[1];
                            if (sub_idx == 2 && quad_vertices == 3) {
                                affine = agx_quad_affine(quad_xy, quad_uv, uv_map);
                            }
                        }
                    }
                }

                int x0 = MAX(clip_x0, (int)lroundf(minx)), y0 = MAX(clip_y0, (int)lroundf(miny));
                int x1 = MIN(clip_x1, (int)lroundf(maxx)), y1 = MIN(clip_y1, (int)lroundf(maxy));
                if (x0 >= x1 || y0 >= y1 || !(u1 >= u0) || !(v1 >= v0)) {
                    continue;
                }
                any_drawn = true;
                if (audit && y0 < 304 && y1 > 96) {
                    qemu_log_mask(LOG_UNIMP, "AGX AUDIT draw target 0x%llx stride %u prog 0x%llx rect %d,%d-%d,%d texture 0x%llx tint 0x%x uv %g,%g-%g,%g\n",
                                  (unsigned long long)target, stride, (unsigned long long)prog, x0,y0,x1,y1,
                                  (unsigned long long)(textured ? tex.addr : 0), tint, u0,v0,u1,v1);
                }

                if (stride == 12 && is_blur_prog && !textured) {
                    /* Untextured Gaussian blur backdrop pass on canvas */
                    uint32_t bw = (uint32_t)(x1 - x0), bh = (uint32_t)(y1 - y0);
                    if (bw > 0 && bh > 0 && bw <= 2048 && bh <= 2048) {
                        uint32_t *raw_tex = g_malloc((size_t)bw * bh * sizeof(uint32_t));
                        uint32_t *tmp_tex = g_malloc((size_t)bw * bh * sizeof(uint32_t));
                        if (raw_tex && tmp_tex) {
                            bool read_ok = true;
                            for (uint32_t y = 0; y < bh; y++) {
                                if (!agx_ctx_read(s, cat, agx_surface_gva(&sf, x0, y0 + y),
                                                  raw_tex + (size_t)y * bw, bw * 4)) {
                                    read_ok = false;
                                    break;
                                }
                            }
                            if (read_ok) {
                                agx_blur_texture(raw_tex, tmp_tex, bw, bh);
                                for (uint32_t y = 0; y < bh; y++) {
                                    agx_ctx_write(s, cat, agx_surface_gva(&sf, x0, y0 + y),
                                                  raw_tex + (size_t)y * bw, bw * 4);
                                }
                            }
                        }
                        g_free(raw_tex);
                        g_free(tmp_tex);
                    }
                    continue;
                }

                if (!textured) {
                    for (int y = y0; y < y1; y++) {
                        if (!agx_ctx_read(s, cat, agx_surface_gva(&sf, x0, y), row,
                                          (x1 - x0) * 4)) {
                            continue;
                        }
                        for (int x = x0; x < x1; x++) {
                            row[x - x0] = agx_blend_over(row[x - x0], colour);
                        }
                        agx_ctx_write(s, cat, agx_surface_gva(&sf, x0, y), row, (x1 - x0) * 4);
                    }
                    continue;
                }

                /* Texel-unit coordinates (rectangle textures) exceed 1. */
                float su = MAX(u1, v1) <= 1.0001f ? tex.w : 1;
                float sv = MAX(u1, v1) <= 1.0001f ? tex.h : 1;
                float sub_w = (u1 - u0) * su;
                float sub_h = (v1 - v0) * sv;
                bool is_scaled = (fabsf((maxx - minx) - sub_w) > 0.1f ||
                                  fabsf((maxy - miny) - sub_h) > 0.1f);
                if (affine) {
                    float step_x = hypotf(uv_map[0] * su, uv_map[3] * sv);
                    float step_y = hypotf(uv_map[1] * su, uv_map[4] * sv);
                    is_scaled = fabsf(step_x - 1.0f) > 0.1f || fabsf(step_y - 1.0f) > 0.1f;
                }
                /* A cross-axis V coordinate needs multiple source rows per
                 * output row: snapshot this bounded texture for the quad (the
                 * axis-aligned path keeps its two-row cache; blur already
                 * owns a snapshot). */
                uint32_t *sample_tex = blurred_tex, *quad_tex = NULL;
                if (affine && fabsf(uv_map[3]) > 1e-8f && !sample_tex) {
                    if ((uint64_t)tex.w * tex.h > 4096 * 4096) {
                        continue;
                    }
                    quad_tex = g_malloc((size_t)tex.w * tex.h * 4);
                    for (unsigned ty = 0; ty < tex.h; ty++) {
                        if (!agx_texture_row(s, cat, &tex, ty, quad_tex + (size_t)ty * tex.w)) {
                            g_free(quad_tex);
                            quad_tex = NULL;
                            break;
                        }
                    }
                    if (!quad_tex) {
                        continue;
                    }
                    sample_tex = quad_tex;
                }
                int cached_y0 = -1, cached_y1 = -1;

                for (int y = y0; y < y1; y++) {
                    /* Clip coverage, not the interpolation domain. */
                    float fv = affine ? uv_map[4] * (y + 0.5f) + uv_map[5] :
                                        agx_quad_coordinate(y, miny, maxy, v0, v1);
                    uint32_t ty0 = 0, ty1 = 0;
                    float fy = 0.0f;
                    if (!sample_tex) {
                        if (is_scaled) {
                            float v_sample = fv * sv - 0.5f;
                            if (v_sample < 0.0f) v_sample = 0.0f;
                            if (v_sample > (float)(tex.h - 1)) v_sample = (float)(tex.h - 1);
                            ty0 = (uint32_t)v_sample;
                            ty1 = ty0 + 1 < tex.h ? ty0 + 1 : ty0;
                            fy = v_sample - (float)ty0;

                            if ((int)ty0 != cached_y0) {
                                if (!agx_texture_row(s, cat, &tex, ty0, texrow)) {
                                    continue;
                                }
                                cached_y0 = (int)ty0;
                            }
                            if (ty1 != ty0 && (int)ty1 != cached_y1) {
                                if (!agx_texture_row(s, cat, &tex, ty1, texrow1)) {
                                    continue;
                                }
                                cached_y1 = (int)ty1;
                            }
                        } else {
                            ty0 = MIN(tex.h - 1, (uint32_t)MAX(0.0f, fv * sv));
                            if (!agx_texture_row(s, cat, &tex, ty0, texrow)) {
                                continue;
                            }
                        }
                    }
                    if (!agx_ctx_read(s, cat, agx_surface_gva(&sf, x0, y), row,
                                      (x1 - x0) * 4)) {
                        continue;
                    }
                    for (int x = x0; x < x1; x++) {
                        float fu = affine ? uv_map[0] * (x + 0.5f) + uv_map[1] * (y + 0.5f) + uv_map[2] :
                                            agx_quad_coordinate(x, minx, maxx, u0, u1);
                        float sample_v = affine ? fv + uv_map[3] * (x + 0.5f) : fv;
                        uint32_t src;
                        if (sample_tex) {
                            if (is_blur || is_scaled) {
                                src = agx_sample_bilinear(sample_tex, tex.w, tex.h,
                                                          fu * su, sample_v * sv);
                            } else {
                                unsigned tx = MIN(tex.w - 1, (unsigned)MAX(0.0f, fu * su));
                                unsigned ty = MIN(tex.h - 1, (unsigned)MAX(0.0f, sample_v * sv));
                                src = sample_tex[(size_t)ty * tex.w + tx];
                            }
                        } else if (is_scaled) {
                            float u_sample = fu * su - 0.5f;
                            if (u_sample < 0.0f) u_sample = 0.0f;
                            if (u_sample > (float)(tex.w - 1)) u_sample = (float)(tex.w - 1);
                            uint32_t tx0 = (uint32_t)u_sample;
                            uint32_t tx1 = tx0 + 1 < tex.w ? tx0 + 1 : tx0;
                            float fx = u_sample - (float)tx0;
                            uint32_t c00 = texrow[tx0];
                            uint32_t c10 = texrow[tx1];
                            uint32_t c01 = (ty1 == ty0) ? texrow[tx0] : texrow1[tx0];
                            uint32_t c11 = (ty1 == ty0) ? texrow[tx1] : texrow1[tx1];
                            src = agx_bilinear_interpolate(c00, c10, c01, c11, fx, fy);
                        } else {
                            uint32_t tx = MIN(tex.w - 1, (uint32_t)MAX(0.0f, fu * su));
                            src = texrow[tx];
                        }
                        uint32_t dst = row[x - x0], out = 0;
                        if (tint != 0xffffffff) {
                            uint32_t m = 0;
                            for (unsigned c = 0; c < 32; c += 8) {
                                m |= (((src >> c) & 0xff) * ((tint >> c) & 0xff) / 255) << c;
                            }
                            src = m;
                        }
                        if (dest_in) {
                            uint32_t a = src >> 24;
                            for (unsigned c = 0; c < 32; c += 8) {
                                out |= (((dst >> c) & 0xff) * a / 255) << c;
                            }
                            row[x - x0] = out;
                        } else if (vibrancy) {
                            row[x - x0] = agx_blend_vibrancy(dst, src);
                        } else {
                            row[x - x0] = agx_blend_over(dst, src);
                        }
                    }
                    agx_ctx_write(s, cat, agx_surface_gva(&sf, x0, y), row, (x1 - x0) * 4);
                }
                g_free(quad_tex);
            }
            g_free(blurred_tex);
            if (any_drawn) {
                draws++;
            }
        } else {
            qemu_log_mask(LOG_GUEST_ERROR, "AGX replay: unknown VDM block 0x%x at 0x%llx\n",
                          w[0], (unsigned long long)pc);
            break;
        }
    }
    if (audit) {
        unsigned after_clock = agx_audit_clock(s, cat, &sf);
        if (before_clock != after_clock || draws > 1) {
            qemu_log_mask(LOG_UNIMP, "AGX AUDIT result target 0x%llx draws %u clock %u -> %u clear %u strip %u pitch %u\n",
                          (unsigned long long)target, draws, before_clock, after_clock, is_clear, sf.strip, sf.pitch);
        }
    }
    qemu_log_mask(LOG_UNIMP, "AGX replay target 0x%llx draws %u\n",
                  (unsigned long long)target, draws);
}

/* Publish one finished job (main loop, BQL held). */
static void agx_replay_complete_job(AGXRenderJob *job)
{
    A7State *s = job->s;

    /* Thread-safety: agx_replay_done_bh runs this on the main loop context with
     * the BQL held. Stamp writes, queue control updates, and firmware completion
     * notifications occur strictly after all frame pixels have been committed to
     * guest RAM by the off-BQL render thread. */
    if (job->has_stamp) {
        agx_gpu_access(s, job->stamp_gva, &job->stamp_value, 4, true);
        qemu_log_mask(LOG_UNIMP, "AGX META: queue 0x%x stamp 0x%x <- %u (channel %u)\n",
                      job->queue_block, job->stamp_meta_addr, job->stamp_value,
                      job->stamp_channel);
    }
    if (job->has_queue_ctrl) {
        smp_wmb();
        agx_gpu_access(s, job->queue_ctrl_gva, &job->queue_read_index, 4, true);
        /* The guest has caught up with the host once the last outstanding
         * index for this queue is published. */
        for (unsigned i = 0; i < ARRAY_SIZE(s->agx_host_read); i++) {
            if (s->agx_host_read[i].valid &&
                s->agx_host_read[i].control == job->queue_ctrl_gva &&
                s->agx_host_read[i].index == job->queue_read_index) {
                s->agx_host_read[i].valid = false;
            }
        }
    }
    if (job->post_completion) {
        qemu_log_mask(LOG_UNIMP, "AGX META: consumed %u commands, %u stamps\n",
                      job->commands, job->stamps);
        agx_post_fw_message(s, 1); /* type 1: work completed */
    }

    if (s->agx_inflight_jobs > 0) {
        s->agx_inflight_jobs--;
    }
    agx_snapshot_unref(job->snapshot);
    g_free(job);
}

static void agx_replay_done_bh(void *opaque)
{
    A7State *s = opaque;
    for (;;) {
        qemu_mutex_lock(&s->agx_render_mutex);
        AGXRenderJob *job = s->agx_done_head;
        if (job) {
            s->agx_done_head = job->next;
            if (!s->agx_done_head) {
                s->agx_done_tail = NULL;
            }
        }
        qemu_mutex_unlock(&s->agx_render_mutex);
        if (!job) {
            return;
        }
        job->next = NULL;
        agx_replay_complete_job(job);
    }
}

/* Dedicated off-BQL render thread worker loop. */
static void *agx_render_thread_fn(void *opaque)
{
    A7State *s = opaque;

    rcu_register_thread();

    qemu_mutex_lock(&s->agx_render_mutex);
    while (!s->agx_render_thread_exit) {
        while (!s->agx_job_head && !s->agx_render_thread_exit) {
            qemu_cond_wait(&s->agx_render_cond, &s->agx_render_mutex);
        }
        if (s->agx_render_thread_exit) {
            break;
        }

        AGXRenderJob *job = s->agx_job_head;
        s->agx_job_head = job->next;
        if (!s->agx_job_head) {
            s->agx_job_tail = NULL;
        }
        job->next = NULL;

        qemu_mutex_unlock(&s->agx_render_mutex);

        /* Thread-safety: rasterise off the BQL under RCU read lock.
         * Guest RAM access (address_space_read/write on address_space_memory)
         * is permitted without the BQL under RCU. No shared emulator machine
         * state is modified here. */
        if (job->target != 0) {
            rcu_read_lock();
            agx_read_snapshot = job->snapshot;
            agx_replay_frame(job->s, job->cat_base, job->ta, job->target,
                             job->width, job->height, job->is_clear, job->pitch, job->scissor_base);
            agx_read_snapshot = NULL;
            rcu_read_unlock();
        }

        /* Stamps and the kick completion are published in order by
         * agx_replay_done_bh on the main loop with the BQL. */
        qemu_mutex_lock(&s->agx_render_mutex);
        if (s->agx_done_tail) {
            s->agx_done_tail->next = job;
        } else {
            s->agx_done_head = job;
        }
        s->agx_done_tail = job;
        qemu_bh_schedule(s->agx_done_bh);
    }
    qemu_mutex_unlock(&s->agx_render_mutex);

    rcu_unregister_thread();
    return NULL;
}

static void agx_render_ensure_thread(A7State *s)
{
    if (!s->agx_render_thread_running && agx_replay_thread_enabled() && agx_replay_enabled()) {
        s->agx_render_thread_running = true;
        s->agx_render_thread_exit = false;
        qemu_thread_create(&s->agx_render_thread, "agx-render",
                           agx_render_thread_fn, s, QEMU_THREAD_JOINABLE);
    }
}

static void agx_enqueue_render_jobs(A7State *s, AGXRenderJob *head, AGXRenderJob *tail)
{
    if (!head || !tail) {
        return;
    }
    for (AGXRenderJob *j = head; j; j = j->next) {
        s->agx_inflight_jobs++;
    }

    agx_render_ensure_thread(s);

    qemu_mutex_lock(&s->agx_render_mutex);
    if (s->agx_job_tail) {
        s->agx_job_tail->next = head;
    } else {
        s->agx_job_head = head;
    }
    s->agx_job_tail = tail;
    qemu_cond_signal(&s->agx_render_cond);
    qemu_mutex_unlock(&s->agx_render_mutex);
}

/* Diagnostic only: RT0 is a GPU VA, not a META pointer (B1 sections 2/5). */
static void agx_trace_rt(A7State *s, uint32_t block, uint32_t address,
                         AGXRenderJob **out_job)
{
    uint8_t raw[0x280];
    hwaddr pa = 0, last_pa = 0;
    bool mapped, last_mapped, contiguous;
    uint64_t first = UINT64_MAX, actual = 0;
    const char *fill = getenv("VPHONE_AGX_RT_FILL");

    if (!agx_gpu_access(s, agx_meta_gva(address), raw, sizeof(raw), false)) {
        qemu_log_mask(LOG_GUEST_ERROR,
                      "AGX RT unreadable queue 0x%x cmd 0x%x\n", block, address);
        return;
    }
    agx_pbe_search(s, raw, address);
    uint32_t context_id = ldl_le_p(raw + 4);
    uint64_t cat_base = s->agx_cat_base;
    /* Check before adding 3 so a malformed context ID cannot wrap. */
    bool context_cat = context_id < ARRAY_SIZE(s->agx_ctx_cat) - 3 &&
                       s->agx_ctx_cat[context_id + 3];
    if (context_cat) {
        cat_base = s->agx_ctx_cat[context_id + 3];
    }
    qemu_log_mask(LOG_UNIMP,
                  "AGX RT catalogue queue 0x%x cmd 0x%x context %u catalogue 0x%llx (%s)\n",
                  block, address, context_id, (unsigned long long)cat_base,
                  context_cat ? "context slot" : "CAT_BASE0 fallback");
    uint64_t pbe_target = 0;
    uint32_t pbe_pitch = 0;
    {
        /* End-of-tile PDS data (heap base 0x6_0000_0000 | cmd +0x1bc, size
         * +0x1c0) carries the PBE state words: W0 = A >> 2, W1[5:0] = A >> 34. */
        static unsigned eot_logged;
        uint64_t eot = UINT64_C(0x600000000) | (uint32_t)ldl_le_p(raw + 0x1bc);
        uint32_t eot_size = MIN(ldl_le_p(raw + 0x1c0), 0x4000u);
        uint32_t words[0x1000];
        unsigned n = 0;
        while (n * 4 < eot_size) {
            hwaddr page;
            uint64_t gva = eot + n * 4;
            uint32_t chunk = MIN(eot_size - n * 4, 0x1000 - (uint32_t)(gva & 0xfff));
            if (!agx_gpu_translate_ctx(s, cat_base, gva, &page)) {
                break;
            }
            address_space_read(&address_space_memory, page, MEMTXATTRS_UNSPECIFIED,
                               words + n, chunk);
            n += chunk / 4;
        }
        static unsigned eot_dumped;
        if (eot_dumped < 6 && ldl_le_p(raw + 4) == 1) {
            char line[0x200 * 9 + 1] = "";
            for (unsigned i = 0; i < MIN(n, 0x200u); i++) {
                snprintf(line + i * 9, 10, " %08x", words[i]);
            }
            eot_dumped++;
            qemu_log_mask(LOG_UNIMP, "AGX EOT dump cmd 0x%x eot 0x%llx size 0x%x read %u words%s\n",
                          address, (unsigned long long)eot, eot_size, n, line);
        }
        /* EOT data word 2 points at the PBE state words of colour target 0. */
        if (n > 2) {
            uint32_t pbe[2];
            hwaddr page, surface_pa;
            if (agx_gpu_translate_ctx(s, cat_base, words[2], &page)) {
                address_space_read(&address_space_memory, page, MEMTXATTRS_UNSPECIFIED,
                                   pbe, sizeof(pbe));
                uint64_t a = ((uint64_t)(pbe[1] & 0x3f) << 34) | ((uint64_t)pbe[0] << 2);
                if (agx_gpu_translate_ctx(s, cat_base, a, &surface_pa)) {
                    pbe_target = a;
                    /* PBE's following texture descriptor names the same
                     * surface and carries exact linear pitch (744-pixel
                     * CoreImage targets need not be aligned to 16 pixels). */
                    uint32_t tsw[4];
                    AGXTexture rt_tex;
                    if (agx_ctx_read(s, cat_base, words[2] + 8, tsw, sizeof(tsw)) &&
                        agx_replay_decode_tsw(s, cat_base, tsw, &rt_tex) &&
                        rt_tex.addr == a && !rt_tex.strip && !rt_tex.twiddled) {
                        pbe_pitch = rt_tex.stride;
                    }
                    qemu_log_mask(LOG_UNIMP,
                                  "AGX RT pbe target cmd 0x%x surface 0x%llx pa 0x%llx\n",
                                  address, (unsigned long long)a,
                                  (unsigned long long)surface_pa);
                }
            }
        }
        /* Follow pointers in the EOT data (e.g. 0x101a40xx) one level. */
        for (unsigned i = 0; i < MIN(n, 0x40u) && eot_logged < 400; i++) {
            uint64_t ptr = words[i];
            uint32_t sub[0x40];
            hwaddr page;
            if (!ptr || (ptr & 3) || !agx_gpu_translate_ctx(s, cat_base, ptr, &page)) {
                continue;
            }
            address_space_read(&address_space_memory, page, MEMTXATTRS_UNSPECIFIED, sub,
                               MIN(sizeof(sub), 0x1000 - (ptr & 0xfff)));
            for (unsigned j = 0; j + 1 < ARRAY_SIZE(sub) && j * 4 + 8 <= 0x1000 - (ptr & 0xfff); j++) {
                uint64_t a = ((uint64_t)(sub[j + 1] & 0x3f) << 34) | ((uint64_t)sub[j] << 2);
                hwaddr surface_pa;
                if (sub[j + 1] && a >= UINT64_C(0x400000000) && a < UINT64_C(0x440000000) &&
                    !(a & 0xfff) && agx_gpu_translate_ctx(s, cat_base, a, &surface_pa)) {
                    eot_logged++;
                    qemu_log_mask(LOG_UNIMP,
                                  "AGX EOT pbe-ptr cmd 0x%x eot word %u ptr 0x%llx word %u "
                                  "w0 0x%x w1 0x%x surface 0x%llx pa 0x%llx scanout 0x%llx\n",
                                  address, i, (unsigned long long)ptr, j, sub[j], sub[j + 1],
                                  (unsigned long long)a, (unsigned long long)surface_pa,
                                  (unsigned long long)s->framebuffer);
                }
            }
        }
        for (unsigned i = 0; i + 1 < n && eot_logged < 400; i++) {
            uint64_t a = ((uint64_t)(words[i + 1] & 0x3f) << 34) | ((uint64_t)words[i] << 2);
            hwaddr surface_pa;
            if (a >= UINT64_C(0x400000000) && a < UINT64_C(0x440000000) && !(a & 0xfff) &&
                agx_gpu_translate_ctx(s, cat_base, a, &surface_pa)) {
                eot_logged++;
                qemu_log_mask(LOG_UNIMP,
                              "AGX EOT pbe cmd 0x%x eot 0x%llx word %u w0 0x%x w1 0x%x "
                              "surface 0x%llx pa 0x%llx scanout 0x%llx\n",
                              address, (unsigned long long)eot, i, words[i], words[i + 1],
                              (unsigned long long)a, (unsigned long long)surface_pa,
                              (unsigned long long)s->framebuffer);
            }
        }
    }
    /* The default captures CA/display work; explicit context selection also
     * permits early wallpaper/upload passes before display MMU programming. */
    const char *dump_context = getenv("VPHONE_AGX_FRAME_DUMP_CONTEXT");
    bool capture_context = dump_context ?
        (!strcmp(dump_context, "all") || context_id == strtoul(dump_context, NULL, 0)) :
        (context_id == 1 && s->disp_mmu_table);
    if (capture_context && context_cat && pbe_target) {
        /* Optional: only dump frames with at least MIN_DRAWS draws (CA
         * redraws only dirty regions, so most frames are small). */
        const char *min_env = getenv("VPHONE_AGX_FRAME_DUMP_MIN_DRAWS");
        const uint8_t *dump_ta = agx_find_ta(s, context_id, raw);
        bool offscreen = (ldl_le_p(raw + 0x74) & 0xffff) != 639 ||
                         (ldl_le_p(raw + 0x70) & 0xffff) != 1135;
        if ((!getenv("VPHONE_AGX_FRAME_DUMP_OFFSCREEN") || offscreen) &&
            (!min_env || (dump_ta && agx_vdm_draw_count(s, cat_base, dump_ta) >=
                                     strtoul(min_env, NULL, 0)))) {
            agx_frame_dump(s, context_id, cat_base, pbe_target, block, raw);
        }
    }
    if (pbe_target) {
        static bool raw_logged;
        MemTxResult r;
        uint32_t pce = address_space_ldl_le(&address_space_memory,
                                            cat_base + ((pbe_target >> 30) & 0x3ff) * 4,
                                            MEMTXATTRS_UNSPECIFIED, &r);
        uint64_t pd = (uint64_t)(pce >> 4) << 12;
        uint64_t pde = address_space_ldq_le(&address_space_memory,
                                            pd + ((pbe_target >> 21) & 0x1ff) * 8,
                                            MEMTXATTRS_UNSPECIFIED, &r);
        if (!raw_logged) {
            char line[20 * 19 + 1] = "";
            raw_logged = true;
            for (unsigned i = 0; i < 20; i++) {
                uint64_t pte = address_space_ldq_le(&address_space_memory,
                                                    (pde & UINT64_C(0xfffffff000)) + i * 8,
                                                    MEMTXATTRS_UNSPECIFIED, &r);
                snprintf(line + i * 19, 20, " %018llx", (unsigned long long)pte);
            }
            qemu_log_mask(LOG_UNIMP, "AGX RT raw target 0x%llx pce 0x%x pde 0x%llx pte%s\n",
                          (unsigned long long)pbe_target, pce, (unsigned long long)pde, line);
        }
    }
    /* +0x70/+0x74 carry flags above the max x/y in bits [15:0]. */
    uint32_t pass_w = (ldl_le_p(raw + 0x74) & 0xffff) + 1;
    uint32_t pass_h = (ldl_le_p(raw + 0x70) & 0xffff) + 1;
    bool is_clear = ldl_le_p(raw + 0x178) != 0 && !(ldl_le_p(raw + 0x23c) & 1);
    /* Each 3D pass replays the TA that shares its parameter buffer; vertex
     * positions go through the pass's own matrix and viewport, so offscreen
     * passes (e.g. 608x64 backdrops) land in their own coordinate space. */
    const uint8_t *pass_ta = agx_find_ta(s, context_id, raw);
    if (pbe_target && context_cat && agx_replay_enabled() && pass_ta) {
        unsigned ta_slot = (pass_ta - &s->agx_ta_ring[context_id][0][0]) / 0x280;
        AGXReplaySnapshot *snapshot = s->agx_ta_snapshot[context_id][ta_slot];
        uint64_t scissor_base = ldl_le_p(raw + 0x50);
        if (snapshot && scissor_base) {
            uint8_t sc_page[4096];
            hwaddr sc_pa;
            uint64_t sc_base_page = scissor_base & ~UINT64_C(0xfff);
            if (!g_hash_table_lookup(snapshot->pages, &sc_base_page) &&
                agx_gpu_translate_ctx(s, cat_base, sc_base_page, &sc_pa) &&
                address_space_read(&address_space_memory, sc_pa, MEMTXATTRS_UNSPECIFIED, sc_page, 4096) == MEMTX_OK) {
                uint64_t *key = g_new(uint64_t, 1);
                *key = sc_base_page;
                g_hash_table_insert(snapshot->pages, key, g_memdup2(sc_page, 4096));
            }
        }
        if (!agx_replay_thread_enabled()) {
            agx_read_snapshot = snapshot;
            agx_replay_frame(s, cat_base, pass_ta, pbe_target, pass_w, pass_h, is_clear, pbe_pitch, scissor_base);
            agx_read_snapshot = NULL;
        } else if (out_job) {
            AGXRenderJob *job = g_new0(AGXRenderJob, 1);
            job->s = s;
            job->snapshot = agx_snapshot_ref(snapshot);
            job->cat_base = cat_base;
            job->target = pbe_target;
            job->width = pass_w;
            job->pitch = pbe_pitch;
            job->height = pass_h;
            job->is_clear = is_clear;
            job->scissor_base = scissor_base;
            memcpy(job->ta, pass_ta, sizeof(job->ta));
            *out_job = job;
        }
    }
    /* +0x68 is PBE register word 0 (swizzle/format), not an address; the
     * colour target comes from the end-of-tile PBE state when available. */
    uint64_t rt0 = pbe_target ? pbe_target : (uint32_t)ldl_le_p(raw + 0x68);
    uint32_t fmt = ldl_le_p(raw + 0x6c);
    uint32_t w = (ldl_le_p(raw + 0x74) & 0xffff) + 1;
    uint32_t h = (ldl_le_p(raw + 0x70) & 0xffff) + 1;
    mapped = agx_gpu_translate_ctx(s, cat_base, rt0, &pa);
    last_mapped = agx_gpu_translate_ctx(s, cat_base, rt0 + 0x2c5000, &last_pa);
    if (!mapped && cat_base) {
        /* Show where the walk stops and the populated catalogue entries. */
        uint32_t pc[0x400];
        address_space_read(&address_space_memory, cat_base, MEMTXATTRS_UNSPECIFIED,
                           pc, sizeof(pc));
        for (unsigned i = 0; i < ARRAY_SIZE(pc); i++) {
            if (pc[i]) {
                uint64_t pd = (uint64_t)(pc[i] >> 4) << 12, pde[0x200];
                unsigned used = 0, first_pde = 0x200;
                address_space_read(&address_space_memory, pd, MEMTXATTRS_UNSPECIFIED,
                                   pde, sizeof(pde));
                for (unsigned j = 0; j < ARRAY_SIZE(pde); j++) {
                    if (pde[j]) {
                        used++;
                        first_pde = MIN(first_pde, j);
                    }
                }
                qemu_log_mask(LOG_UNIMP,
                              "AGX RT walk catalogue 0x%llx pce[%u]=0x%x pd 0x%llx used %u "
                              "first pde[%u]=0x%llx want pde[%u]=0x%llx\n",
                              (unsigned long long)cat_base, i, pc[i],
                              (unsigned long long)pd, used, first_pde,
                              (unsigned long long)(first_pde < 0x200 ? pde[first_pde] : 0),
                              (unsigned)((rt0 >> 21) & 0x1ff),
                              (unsigned long long)pde[(rt0 >> 21) & 0x1ff]);
                /* Reverse lookup: which GVAs map the scanout surface page? */
                for (unsigned j = 0; j < ARRAY_SIZE(pde); j++) {
                    uint64_t pte[0x200];
                    if (!(pde[j] & 1)) {
                        continue;
                    }
                    address_space_read(&address_space_memory, pde[j] & UINT64_C(0xfffffff000),
                                       MEMTXATTRS_UNSPECIFIED, pte, sizeof(pte));
                    for (unsigned k = 0; k < ARRAY_SIZE(pte); k++) {
                        if ((pte[k] & 1) && s->framebuffer &&
                            (pte[k] & UINT64_C(0xfffffff000)) == (s->framebuffer & ~UINT64_C(0xfff))) {
                            qemu_log_mask(LOG_UNIMP,
                                          "AGX RT walk scanout page 0x%llx at gva 0x%llx pte 0x%llx\n",
                                          (unsigned long long)s->framebuffer,
                                          (unsigned long long)(((uint64_t)i << 30) |
                                                               ((uint64_t)j << 21) |
                                                               ((uint64_t)k << 12)),
                                          (unsigned long long)pte[k]);
                        }
                    }
                }
            }
        }
    }
    qemu_log_mask(LOG_UNIMP,
                  "AGX RT queue 0x%x cmd 0x%x ch %u rt0 gva 0x%llx pa 0x%llx fmt 0x%x w %u h %u load 0x%x\n",
                  block, address, ldl_le_p(raw + 4),
                  (unsigned long long)rt0, (unsigned long long)pa,
                  fmt, w, h, ldl_le_p(raw + 0x1d0));

    /* Fixed 0x2c6000-byte probe, independent of dimensions/encoded pitch.
     * Check each page boundary, including an unaligned first/last page. */
    contiguous = mapped;
    if (!mapped) {
        first = 0;
    }
    for (uint64_t off = 0x1000 - (rt0 & 0xfff);
         off < 0x2c6000; off += 0x1000) {
        hwaddr page_pa = 0;
        bool page_mapped = agx_gpu_translate_ctx(s, cat_base, rt0 + off, &page_pa);
        if ((!page_mapped || !mapped || page_pa != pa + off) &&
            first == UINT64_MAX) {
            first = off;
            actual = page_pa;
            contiguous = false;
        }
    }
    qemu_log_mask(LOG_UNIMP,
                  "AGX RT span queue 0x%x cmd 0x%x bytes 0x2c6000 last gva 0x%llx pa 0x%llx mapped %u contiguous %u\n",
                  block, address, (unsigned long long)(rt0 + 0x2c5000),
                  (unsigned long long)last_pa, (unsigned)last_mapped,
                  (unsigned)contiguous);
    if (!contiguous) {
        qemu_log_mask(LOG_UNIMP,
                      "AGX RT discontinuity queue 0x%x cmd 0x%x offset 0x%llx gva 0x%llx expected 0x%llx pa 0x%llx\n",
                      block, address, (unsigned long long)first,
                      (unsigned long long)(rt0 + first),
                      (unsigned long long)(mapped ? pa + first : 0),
                      (unsigned long long)actual);
    }
    if (fill) {
        const char *end;
        uint64_t value, row_bytes = (uint64_t)w * 4;
        uint64_t pitch = row_bytes;
        uint8_t pixels[0x1004];

        /* B1: format 0x0200 has pitch_pixels = (bits[13:2] + 1)*2.
         * Other formats use w*4 as a diagnostic fallback. This is a linear
         * row fill, not a twiddle/format converter. Preserve row padding.
         * Write the numeric hex word in little endian, with no RGBA swizzle. */
        if ((fmt >> 16) == 0x0200) {
            pitch = (((fmt & 0x3ffc) >> 2) + 1) * 8;
        }
        if (!*fill || (*fill == '-' || *fill == '+') ||
            qemu_strtou64(fill, &end, 16, &value) || *end ||
            value > UINT32_MAX || !w || !h || pitch < row_bytes ||
            pitch > 64 * MiB || h > 64 * MiB / pitch ||
            pitch * (h - 1) + row_bytes > 64 * MiB ||
            rt0 + pitch * (h - 1) + row_bytes > (UINT64_C(1) << 40)) {
            qemu_log_mask(LOG_GUEST_ERROR,
                          "AGX RT fill skipped: invalid hex word or dimensions/pitch (64 MiB limit)\n");
            return;
        }
        for (size_t i = 0; i < sizeof(pixels); i += 4) {
            stl_le_p(pixels + i, value);
        }
        for (uint32_t y = 0; y < h; y++) {
            for (uint64_t x = 0; x < row_bytes;) {
                uint64_t gva = rt0 + y * pitch + x;
                size_t chunk = MIN(row_bytes - x, 0x1000 - (gva & 0xfff));
                hwaddr dst;
                if (!agx_gpu_translate_ctx(s, cat_base, gva, &dst)) {
                    qemu_log_mask(LOG_GUEST_ERROR,
                                  "AGX RT fill stopped at unmapped gva 0x%llx\n",
                                  (unsigned long long)gva);
                    return;
                }
                /* x can be unaligned when the GPU VA crosses a page. */
                address_space_write(&address_space_memory, dst, MEMTXATTRS_UNSPECIFIED,
                                    pixels + (x & 3), chunk);
                x += chunk;
            }
        }
    }
}

static unsigned agx_drain_queue(A7State *s, uint32_t block, unsigned *stamps,
                                AGXRenderJob **local_head, AGXRenderJob **local_tail)
{
    static int dump_enabled = -1;
    uint32_t pointers[2], read_index, write_index, size;

    if (dump_enabled < 0) {
        dump_enabled = getenv("VPHONE_AGX_DUMP") != NULL;
    }
    if (!agx_gpu_access(s, agx_meta_gva(block) + 4, pointers, sizeof(pointers), false)) {
        return 0;
    }
    uint64_t control = agx_meta_gva(pointers[0]), entries = agx_meta_gva(pointers[1]);
    if (!agx_gpu_access(s, control, &read_index, 4, false) ||
        !agx_gpu_access(s, control + 0x20, &write_index, 4, false) ||
        !agx_gpu_access(s, control + 0x30, &size, 4, false) ||
        !size || size > 0x10000 || read_index >= size || write_index >= size) {
        return 0;
    }
    int host_slot = -1;
    for (unsigned i = 0; i < ARRAY_SIZE(s->agx_host_read); i++) {
        if (s->agx_host_read[i].valid && s->agx_host_read[i].control == control) {
            host_slot = i;
            read_index = s->agx_host_read[i].index;
            break;
        }
    }
    unsigned done = 0;
    AGXRenderJob *pending_job = NULL;
    bool queue_has_job = false;

    while (read_index != write_index) {
        uint32_t address, command[4];
        if (!agx_gpu_access(s, entries + read_index * 4, &address, 4, false) ||
            !agx_gpu_access(s, agx_meta_gva(address), command, sizeof(command), false)) {
            break;
        }
        if (command[0] == 3) {
            /* Native TA/3D queues use type-3 stamp waits to protect VDM and
             * surface reuse. Ignoring these lets TA run ahead and the guest
             * overwrite vertices before the corresponding 3D replay. */
            uint32_t value;
            if (!agx_gpu_access(s, agx_meta_gva(command[1]), &value, 4, false) ||
                (int32_t)(value - command[2]) < 0) {
                timer_mod(s->agx_kick_timer,
                          qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 1000000);
                break;
            }
        }
        if (command[0] == 4) {
            if (pending_job) {
                pending_job->has_stamp = true;
                pending_job->stamp_gva = agx_meta_gva(command[1]);
                pending_job->stamp_meta_addr = command[1];
                pending_job->stamp_value = command[2];
                pending_job->stamp_channel = command[3];
                pending_job->queue_block = block;
                (*stamps)++;
                read_index = (read_index + 1) % size;
                pending_job->has_queue_ctrl = true;
                pending_job->queue_ctrl_gva = control;
                pending_job->queue_read_index = read_index;
                if (!*local_head) {
                    *local_head = pending_job;
                } else {
                    (*local_tail)->next = pending_job;
                }
                *local_tail = pending_job;
                queue_has_job = true;
                pending_job = NULL;
                done++;
                continue;
            } else if (agx_replay_thread_enabled() && (s->agx_inflight_jobs > 0 || *local_head)) {
                /* Preserve FIFO ordering: queue stamp behind in-flight replay */
                AGXRenderJob *stamp_job = g_new0(AGXRenderJob, 1);
                stamp_job->s = s;
                stamp_job->has_stamp = true;
                stamp_job->stamp_gva = agx_meta_gva(command[1]);
                stamp_job->stamp_meta_addr = command[1];
                stamp_job->stamp_value = command[2];
                stamp_job->stamp_channel = command[3];
                stamp_job->queue_block = block;
                (*stamps)++;
                read_index = (read_index + 1) % size;
                stamp_job->has_queue_ctrl = true;
                stamp_job->queue_ctrl_gva = control;
                stamp_job->queue_read_index = read_index;
                if (!*local_head) {
                    *local_head = stamp_job;
                } else {
                    (*local_tail)->next = stamp_job;
                }
                *local_tail = stamp_job;
                queue_has_job = true;
                done++;
                continue;
            } else {
                if (agx_gpu_access(s, agx_meta_gva(command[1]), &command[2], 4, true)) {
                    (*stamps)++;
                    qemu_log_mask(LOG_UNIMP, "AGX META: queue 0x%x stamp 0x%x <- %u (channel %u)\n",
                                  block, command[1], command[2], command[3]);
                }
            }
        } else if (command[0] != 4) {
            if (pending_job) {
                pending_job->has_queue_ctrl = true;
                pending_job->queue_ctrl_gva = control;
                pending_job->queue_read_index = read_index;
                if (!*local_head) {
                    *local_head = pending_job;
                } else {
                    (*local_tail)->next = pending_job;
                }
                *local_tail = pending_job;
                queue_has_job = true;
                pending_job = NULL;
            }
            if (command[0] == 0 && (getenv("VPHONE_AGX_FRAME_DUMP") || agx_replay_enabled()) &&
                command[1] < ARRAY_SIZE(s->agx_last_ta)) {
                uint32_t context = command[1];
                uint8_t ta[0x280];
                if (agx_gpu_access(s, agx_meta_gva(address), ta, sizeof(ta), false)) {
                    memcpy(s->agx_last_ta[context], ta, sizeof(ta));
                    unsigned slot = s->agx_ta_ring_next[context]++;
                    memcpy(s->agx_ta_ring[context][slot], ta, sizeof(ta));
                    agx_snapshot_unref(s->agx_ta_snapshot[context][slot]);
                    uint64_t cat = context < ARRAY_SIZE(s->agx_ctx_cat) - 3 && s->agx_ctx_cat[context + 3] ?
                                   s->agx_ctx_cat[context + 3] : s->agx_cat_base;
                    s->agx_ta_snapshot[context][slot] = agx_capture_ta(s, cat, ta);
                    s->agx_last_ta_valid[context] = true;
                    s->agx_last_ta_queue[context] = block;
                }
            }
            if (command[0] == 1) {
                agx_trace_rt(s, block, address, &pending_job);
            }
            if (dump_enabled) {
                uint8_t raw[0x280];
                char hex[sizeof(raw) * 2 + 1];
                static const char digits[] = "0123456789abcdef";
                size_t length = command[0] == 3 ? 0x40 : sizeof(raw);

                if (agx_gpu_access(s, agx_meta_gva(address), raw, length, false)) {
                    for (size_t i = 0; i < length; i++) {
                        hex[i * 2] = digits[raw[i] >> 4];
                        hex[i * 2 + 1] = digits[raw[i] & 0xf];
                    }
                    hex[length * 2] = '\0';
                    qemu_log_mask(LOG_UNIMP, "AGX DUMP queue 0x%x cmd 0x%x len 0x%zx hex=%s\n",
                                  block, address, length, hex);
                }
            }
            qemu_log_mask(LOG_UNIMP, "AGX META: queue 0x%x cmd 0x%x type %u [1]=0x%x [2]=0x%x [3]=0x%x\n",
                          block, address, command[0], command[1], command[2], command[3]);
        }
        read_index = (read_index + 1) % size;
        done++;
    }
    if (pending_job) {
        pending_job->has_queue_ctrl = true;
        pending_job->queue_ctrl_gva = control;
        pending_job->queue_read_index = read_index;
        if (!*local_head) {
            *local_head = pending_job;
        } else {
            (*local_tail)->next = pending_job;
        }
        *local_tail = pending_job;
        queue_has_job = true;
        pending_job = NULL;
    }
    /* An empty poll must not publish the private cursor: its pixels/stamps
     * are still in flight. Doing so released guest resources before replay. */
    if (!queue_has_job && host_slot >= 0 && !done) {
        return 0;
    }
    if (!queue_has_job && host_slot >= 0 && done) {
        /* Older jobs of this queue are still in flight: publish this index
         * behind them, or their completion would move the guest's read
         * index backwards. */
        AGXRenderJob *ctrl_job = g_new0(AGXRenderJob, 1);
        ctrl_job->s = s;
        ctrl_job->has_queue_ctrl = true;
        ctrl_job->queue_ctrl_gva = control;
        ctrl_job->queue_read_index = read_index;
        if (!*local_head) {
            *local_head = ctrl_job;
        } else {
            (*local_tail)->next = ctrl_job;
        }
        *local_tail = ctrl_job;
        queue_has_job = true;
    }
    if (!queue_has_job) {
        smp_wmb();
        agx_gpu_access(s, control, &read_index, 4, true);
    } else {
        if (host_slot < 0) {
            for (unsigned i = 0; i < ARRAY_SIZE(s->agx_host_read); i++) {
                if (!s->agx_host_read[i].valid) {
                    host_slot = i;
                    break;
                }
            }
        }
        if (host_slot >= 0) {
            s->agx_host_read[host_slot].control = control;
            s->agx_host_read[host_slot].index = read_index;
            s->agx_host_read[host_slot].valid = true;
        }
    }
    return done;
}

/* Ring 3 has no doorbell: poll it before queue work and at each vblank. */
static void agx_drain_context_ring(A7State *s)
{
    uint64_t control = s->agx_shared_gva + AGX_HOST_RING_CTRL + 3 * 0x20;
    uint32_t read_index, write_index;

    if (!s->agx_shared_gva ||
        !agx_gpu_access(s, control, &read_index, 4, false) ||
        !agx_gpu_access(s, control + 0x10, &write_index, 4, false)) {
        return;
    }
    while ((read_index & 0xff) != (write_index & 0xff)) {
        uint8_t entry[AGX_HOST_RING_STRIDE];
        if (!agx_gpu_access(s, s->agx_shared_gva + AGX_HOST_RING_ENTRIES +
                            3 * AGX_HOST_RING_SPAN +
                            (read_index & 0xff) * AGX_HOST_RING_STRIDE,
                            entry, sizeof(entry), false)) {
            break;
        }
        uint32_t type = ldl_le_p(entry);
        uint32_t slot = ldl_le_p(entry + 8);
        if (type == 3 && slot < ARRAY_SIZE(s->agx_ctx_cat)) {
            uint64_t cat_base = ldq_le_p(entry + 0x10);
            s->agx_ctx_cat[slot] = cat_base;
            qemu_log_mask(LOG_UNIMP, "AGX META: bind slot %u catalogue 0x%llx\n",
                          slot, (unsigned long long)cat_base);
        } else {
            qemu_log_mask(LOG_UNIMP, "AGX META: host ring 3 skipped type %u slot %u\n",
                          type, slot);
        }
        read_index = (read_index + 1) & 0xff;
    }
    smp_wmb();
    agx_gpu_access(s, control, &read_index, 4, true);
}

static void agx_kick_cb(void *opaque)
{
    A7State *s = opaque;
    uint32_t rings = s->agx_pending_doorbells;
    s->agx_pending_doorbells = 0;
    agx_drain_context_ring(s);
    for (unsigned ring = 0; ring < 3; ring++) {
        if (!(rings & (1U << ring))) {
            continue;
        }
        uint64_t control = s->agx_shared_gva + AGX_HOST_RING_CTRL + ring * 0x20;
        uint32_t read_index, write_index;
        if (!agx_gpu_access(s, control, &read_index, 4, false) ||
            !agx_gpu_access(s, control + 0x10, &write_index, 4, false)) {
            continue;
        }
        while ((read_index & 0xff) != (write_index & 0xff)) {
            uint32_t entry[AGX_HOST_RING_STRIDE / 4];
            if (!agx_gpu_access(s, s->agx_shared_gva + AGX_HOST_RING_ENTRIES + ring * AGX_HOST_RING_SPAN +
                                (read_index & 0xff) * AGX_HOST_RING_STRIDE, entry, sizeof(entry), false)) {
                break;
            }
            qemu_log_mask(LOG_UNIMP, "AGX META: host ring %u type %u arg 0x%x\n", ring, entry[0], entry[2]);
            if (entry[2]) {
                bool known = false;
                for (unsigned q = 0; q < s->agx_queue_count; q++) {
                    known |= s->agx_queues[q] == entry[2];
                }
                if (!known && s->agx_queue_count < ARRAY_SIZE(s->agx_queues)) {
                    s->agx_queues[s->agx_queue_count++] = entry[2];
                }
            }
            read_index = (read_index + 1) & 0xff;
        }
        smp_wmb();
        agx_gpu_access(s, control, &read_index, 4, true);
    }
    unsigned commands = 0, stamps = 0;
    AGXRenderJob *local_head = NULL, *local_tail = NULL;
    for (unsigned q = 0; q < s->agx_queue_count; q++) {
        commands += agx_drain_queue(s, s->agx_queues[q], &stamps, &local_head, &local_tail);
    }
    if (local_tail) {
        local_tail->post_completion = true;
        local_tail->commands = commands;
        local_tail->stamps = stamps;
        agx_enqueue_render_jobs(s, local_head, local_tail);
    } else if (commands) {
        if (agx_replay_thread_enabled() && s->agx_inflight_jobs > 0) {
            AGXRenderJob *comp_job = g_new0(AGXRenderJob, 1);
            comp_job->s = s;
            comp_job->post_completion = true;
            comp_job->commands = commands;
            comp_job->stamps = stamps;
            agx_enqueue_render_jobs(s, comp_job, comp_job);
        } else {
            qemu_log_mask(LOG_UNIMP, "AGX META: consumed %u commands, %u stamps\n", commands, stamps);
            agx_post_fw_message(s, 1); /* type 1: work completed */
        }
    }
}

static void agx_doorbell(A7State *s, uint64_t value)
{
    if (!s->agx_shared_gva || (value & ~UINT64_C(3)) != 0x10) {
        return;
    }
    s->agx_pending_doorbells |= 1U << (value & 3);
    timer_mod(s->agx_kick_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 50000);
}

static void agx_sequencer_run(A7Peripheral *p)
{
    A7State *s = p->owner;
    uint64_t cat_base = 0;
    bool meta_boot = false;
    for (uint32_t off = AGX_SEQ_BASE; off + 8 <= AGX_SEQ_END; off += 8) {
        uint32_t command = ldl_le_p(p->regs + off);
        uint32_t value = ldl_le_p(p->regs + off + 4);
        if (command == 0xffff0000) {
            break;
        }
        switch (command & 0xffff) {
        case 0xbf8: meta_boot = value & 1; break;
        case 0x1200: cat_base = deposit64(cat_base, 0, 32, value); break;
        case 0x1204: cat_base = deposit64(cat_base, 32, 32, value); break;
        }
    }
    if (meta_boot && cat_base) {
        /* VPHONE_AGX_HLE=0 leaves META silent (no alive, no queue service,
         * no guest RAM writes), for A/B comparisons against the HLE. */
        const char *hle = getenv("VPHONE_AGX_HLE");
        s->agx_cat_base = cat_base;
        s->agx_shared_gva = 0;
        if (!hle || strcmp(hle, "0")) {
            timer_mod(s->agx_meta_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 100000);
        }
    }
}

static uint64_t peripheral_read(void *opaque, hwaddr off, unsigned size)
{
    A7Peripheral *p = opaque;
    uint64_t val = peripheral_read_impl(opaque, off, size);
    display_mmio_trace(opaque, "read", off, size, val);
    mmio_trace(p->name, "read", off, size, val);
    return val;
}

static void peripheral_write(void *opaque, hwaddr off, uint64_t val, unsigned size)
{
    A7Peripheral *p = opaque;
    display_mmio_trace(opaque, "write", off, size, val);
    mmio_trace(p->name, "write", off, size, val);
    peripheral_write_impl(opaque, off, val, size);
    if (p->base == UINT64_C(0x201e00000) && off == 0x1000 && (val & 1)) {
        agx_sequencer_run(p);
    }
    if (p->base == UINT64_C(0x201000000) && off == AGX_DOORBELL) {
        agx_doorbell(p->owner, val);
    }
}

static const MemoryRegionOps peripheral_ops = {
    .read = peripheral_read, .write = peripheral_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1, .valid.max_access_size = 8,
};

/* /arm-io reg[1:2], selected by the cpus' function-error_handler core
 * index. Error control/syndrome storage only; no fabricated fault status. */
static uint64_t cpu_mmio_read(void *opaque, hwaddr off, unsigned size)
{
    uint8_t *regs = opaque;
    return size == 8 ? ldq_le_p(regs + off) : ldl_le_p(regs + off);
}

static void cpu_mmio_write(void *opaque, hwaddr off, uint64_t val, unsigned size)
{
    uint8_t *regs = opaque;
    if (size == 8) {
        stq_le_p(regs + off, val);
    } else {
        stl_le_p(regs + off, val);
    }
}

static const MemoryRegionOps cpu_mmio_ops = {
    .read = cpu_mmio_read, .write = cpu_mmio_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 4, .valid.max_access_size = 8,
    .impl.min_access_size = 4, .impl.max_access_size = 8,
};

static void timer_irq(void *opaque, int n, int level)
{
    A7State *s = opaque;
    unsigned c = n / 2;
    s->timer_level[c][n % 2] = level;
    qemu_set_irq(qdev_get_gpio_in(DEVICE(s->ap_cpu[c]), ARM_CPU_FIQ),
                 s->timer_level[c][0] || s->timer_level[c][1]);
}

static uint64_t apple_c15_read(CPUARMState *env, const ARMCPRegInfo *ri)
{
    A7State *s = A7(qdev_get_machine());
    unsigned c = env_archcpu(env)->mp_affinity & 1;
    return s->apple_c15[c][ri->opc1 & 7][ri->crm & 15][ri->opc2 & 7];
}

static void apple_c15_write(CPUARMState *env, const ARMCPRegInfo *ri, uint64_t val)
{
    A7State *s = A7(qdev_get_machine());
    unsigned c = env_archcpu(env)->mp_affinity & 1;
    s->apple_c15[c][ri->opc1 & 7][ri->crm & 15][ri->opc2 & 7] = val;
    /* Log each register once: PMCPerfController rewrites the PMC set at
     * ~450 Hz, and logging every write put ~50k synchronous lines into a
     * probe's trace (agy D6), slowing MTTCG. */
    static uint8_t logged[8][16];
    uint8_t bit = 1U << (ri->opc2 & 7);
    if (!(qatomic_fetch_or(&logged[ri->opc1 & 7][ri->crm & 15], bit) & bit)) {
        qemu_log_mask(LOG_UNIMP, "A7 c15 reg opc1=%d crm=%d opc2=%d stored 0x%" PRIx64
                      "; hardware effects unimplemented (logged once)\n",
                      ri->opc1, ri->crm, ri->opc2, val);
    }
}

static void vblank_timer_cb(void *opaque)
{
    A7State *s = opaque;
    agx_drain_context_ring(s);
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
            if (!(s->mask[2] & 2U) && !(s->soft[2] & 2U)) {
                s->soft[2] |= 2U;
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
    for (unsigned c = 0; c < MACHINE(s)->smp.cpus; c++) {
        cpu_reset(CPU(s->ap_cpu[c]));
    }
    pstate_write(&s->cpu->env,
                 (s->boot_el == 3 ? PSTATE_MODE_EL3h : PSTATE_MODE_EL1h) |
                 PSTATE_D | PSTATE_A | PSTATE_I | PSTATE_F);
    s->cpu->env.pc = s->boot_entry;
    s->cpu->env.xregs[0] = s->boot_args;
    s->secondary_started = false;
    memset(s->mask, 0xff, sizeof(s->mask));
    memset(s->soft, 0, sizeof(s->soft));
    memset(s->device_irq, 0, sizeof(s->device_irq));
    memset(s->apple_c15, 0, sizeof(s->apple_c15));
    memset(s->cpu_regs, 0, sizeof(s->cpu_regs));
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
    if (s->agx_meta_timer) {
        timer_del(s->agx_meta_timer);
    }
    if (s->agx_kick_timer) {
        timer_del(s->agx_kick_timer);
    }
    s->agx_cat_base = 0;
    s->disp_mmu_table = 0;
    memset(s->agx_ctx_cat, 0, sizeof(s->agx_ctx_cat));
    memset(s->agx_last_ta, 0, sizeof(s->agx_last_ta));
    memset(s->agx_last_ta_queue, 0, sizeof(s->agx_last_ta_queue));
    for (unsigned c = 0; c < ARRAY_SIZE(s->agx_ta_snapshot); c++) {
        for (unsigned i = 0; i < ARRAY_SIZE(s->agx_ta_snapshot[c]); i++) {
            agx_snapshot_unref(s->agx_ta_snapshot[c][i]);
            s->agx_ta_snapshot[c][i] = NULL;
        }
    }
    memset(s->agx_ta_ring, 0, sizeof(s->agx_ta_ring));
    memset(s->agx_ta_ring_next, 0, sizeof(s->agx_ta_ring_next));
    memset(s->agx_last_ta_valid, 0, sizeof(s->agx_last_ta_valid));
    s->agx_frame_dump_count = 0;
    s->agx_frame_dump_seen = 0;
    s->agx_shared_gva = 0;
    s->agx_queue_count = 0;
    s->agx_pending_doorbells = 0;
    s->agx_meta_alive_count = 0;
    if (s->agx_render_thread_running) {
        qemu_mutex_lock(&s->agx_render_mutex);
        AGXRenderJob *job = s->agx_job_head;
        while (job) {
            AGXRenderJob *next = job->next;
            agx_snapshot_unref(job->snapshot);
            g_free(job);
            job = next;
        }
        s->agx_job_head = NULL;
        s->agx_job_tail = NULL;
        for (job = s->agx_done_head; job; ) {
            AGXRenderJob *next = job->next;
            agx_snapshot_unref(job->snapshot);
            g_free(job);
            job = next;
        }
        s->agx_done_head = NULL;
        s->agx_done_tail = NULL;
        qemu_mutex_unlock(&s->agx_render_mutex);
        s->agx_inflight_jobs = 0;
        memset(s->agx_host_read, 0, sizeof(s->agx_host_read));
    }
    s->ans_clock_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    s->ans_calendar_seconds = s->utility_epoch;
    s->ans_elapsed_seconds = 0;
    s->ans_timestamp_set = false;
    if (s->gpio_regs) {
        memset(s->gpio_regs, 0, GPIO_SIZE);
    }
    s->touch_irq_low = false;
    for (int i = 0; i < NUM_BRIDGES; i++) {
        if (s->bridges[i].regs) {
            memset(s->bridges[i].regs, 0, s->bridges[i].size);
        }
    }
    for (int i = 0; i < NUM_PERIPHERALS; i++) {
        memset(&s->peripherals[i].tristar, 0, sizeof(s->peripherals[i].tristar));
        memset(&s->peripherals[i].lm3534, 0, sizeof(s->peripherals[i].lm3534));
        s->peripherals[i].i2c_active = false;
        s->peripherals[i].pmu_rx_head = s->peripherals[i].pmu_rx_count = 0;
        s->peripherals[i].pmu_prefix_count = 0;
        s->peripherals[i].pmu_pointer_valid = false;
        memset(&s->peripherals[i].spi, 0, sizeof(s->peripherals[i].spi));
        s->peripherals[i].spi.touch.owner = s;
        s->peripherals[i].i2c_rx_valid = false;
        s->peripherals[i].chestnut_pointer_valid = false;
        s->peripherals[i].chestnut_prefix = false;
        if (s->peripherals[i].regs) {
            memset(s->peripherals[i].regs, 0, s->peripherals[i].size);
        }
    }
    timer_del(s->mouse_touch_timer);
    memset(&s->mouse_touch, 0, sizeof(s->mouse_touch));
    touch_script_reset(s);
    s->chestnut_control = 0;
    a7_otg_reset(s);
    for (unsigned i = 0; i < ARRAY_SIZE(s->usb_aux); i++) {
        memset(s->usb_aux[i].regs, 0, s->usb_aux[i].size);
    }
    d2045_reset(s);
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
    memset(s->ipi, 0, sizeof(s->ipi));
    memset(s->ipi_mask, 0xff, sizeof(s->ipi_mask));
    memset(s->timer_level, 0, sizeof(s->timer_level));
    memset(s->targets, 0, sizeof(s->targets));
    s->rx_count = 0;
    aic_update(s);
}

static bool display_update(void *opaque)
{
    A7State *s = opaque;
    DisplaySurface *surface = qemu_console_surface(s->con);
    uint8_t *ram = memory_region_get_ram_ptr(MACHINE(s)->ram);
    uint8_t *src = ram + s->framebuffer - RAM_BASE;
    if (s->disp_mmu_table) {
        /* Gather the plane through the display MMU page table. */
        /* Display surfaces are linear 640 * 4-byte rows (the /vram carveout
         * is exactly 2560 * 1136); 0x4040 (0x1000) is not the row pitch. */
        uint32_t stride = 2560;
        for (unsigned y = 0; y < 1136; y++) {
            uint8_t *dst = surface_data(surface) + y * surface_stride(surface);
            uint64_t o = s->disp_offset + (uint64_t)y * stride;
            for (unsigned x = 0; x < 2560;) {
                unsigned chunk = MIN(2560 - x, 0x1000 - ((o + x) & 0xfff));
                MemTxResult r;
                uint32_t e = address_space_ldl_le(&address_space_memory,
                                                  s->disp_mmu_table + ((o + x) >> 12) * 4,
                                                  MEMTXATTRS_UNSPECIFIED, &r);
                uint64_t pa = ((uint64_t)(e & ~0xfU) << 4) + ((o + x) & 0xfff);
                if (r == MEMTX_OK && (e & 1) && pa >= RAM_BASE && pa + chunk <= RAM_BASE + GiB) {
                    memcpy(dst + x, ram + pa - RAM_BASE, chunk);
                } else {
                    memset(dst + x, 0, chunk);
                }
                x += chunk;
            }
        }
    } else {
        for (unsigned y = 0; y < 1136; y++) {
            memcpy(surface_data(surface) + y * surface_stride(surface), src + y * 2560, 2560);
        }
    }
    qemu_console_update(s->con, 0, 0, 640, 1136);
    return true;
}
static const GraphicHwOps display_ops = { .gfx_update = display_update };

/* USB complex DT ranges: arm-io +0xc000000, EHCI +0x200000/+0x400000.
 * Use the EHCI engine, including real reset/run/halt, frame clock, W1C,
 * schedule and IRQ semantics. No USB device is attached by this board.
 * The root-port counts cover the DT's EHCI0 ports 1/3 and EHCI1 port 1;
 * these are a modeled topology, not recovered capability/OTP dumps.
 */
static void a7_ehci_class_init(ObjectClass *oc, const void *data)
{
    SysBusEHCIClass *sec = SYS_BUS_EHCI_CLASS(oc);
    sec->capsbase = 0;
    sec->opregbase = 0x10;
    sec->portscbase = 0x44;
    sec->portnr = *(const unsigned *)data;
}

static void a7_usb_irq(void *opaque, int irq, int level)
{
    A7State *s = opaque;
    uint32_t bit = 1U << (irq % 32);
    if (level) {
        s->device_irq[irq / 32] |= bit;
    } else {
        s->device_irq[irq / 32] &= ~bit;
    }
    aic_update(s);
}

static void a7_usb_init(A7State *s)
{
    /* The native schedule pointers are 32-bit DRAM bus offsets, whereas AP
     * RAM starts at 0x800000000. Keep this wiring private to USB: adding a
     * low alias to system memory would also change CPU and other DMA views.
     * No USB iommu-parent/DART exists in the N53 DT. */
    memory_region_init(&s->usb_dma_memory, OBJECT(s), "a7.usb-dma", UINT64_C(0x100000000));
    memory_region_init_alias(&s->usb_dma_ram, OBJECT(s), "a7.usb-dram",
                             MACHINE(s)->ram, 0, GiB);
    memory_region_add_subregion(&s->usb_dma_memory, 0, &s->usb_dma_ram);
    address_space_init(&s->usb_dma_as, &s->usb_dma_memory, "a7.usb-dma");
    static const char *const types[] = { "a7.ehci0", "a7.ehci1" };
    for (unsigned i = 0; i < ARRAY_SIZE(types); i++) {
        DeviceState *dev = qdev_new(types[i]);
        SysBusDevice *bus = SYS_BUS_DEVICE(dev);
        sysbus_realize_and_unref(bus, &error_fatal);
        SYS_BUS_EHCI(dev)->ehci.as = &s->usb_dma_as;
        sysbus_mmio_map(bus, 0, UINT64_C(0x20c200000) + i * 0x200000);
        sysbus_connect_irq(bus, 0, qemu_allocate_irq(a7_usb_irq, s, 163 + i * 2));
    }
}

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
        ms->ram_size != GiB || (ms->smp.cpus < 1 || ms->smp.cpus > 2) ||
        s->boot_entry < RAM_BASE || s->boot_entry >= RAM_BASE + GiB ||
        s->boot_args < RAM_BASE || s->boot_args >= RAM_BASE + GiB ||
        s->framebuffer < RAM_BASE || s->framebuffer + 2560 * 1136 > RAM_BASE + GiB) {
        error_report("experimental s5l8960x supports one or two CPUs, 1 GiB and in-RAM boot/display addresses"); exit(1);
    }
    const char *pmu_env = getenv("VPHONE_PMU");
    bool enable_pmu = pmu_env && !strcmp(pmu_env, "1");
    for (unsigned c = 0; c < ms->smp.cpus; c++) {
        ARMCPU *cpu = ARM_CPU(object_new(ms->cpu_type));
        s->ap_cpu[c] = cpu;
        object_property_set_int(OBJECT(cpu), "mp-affinity", c, &error_fatal);
        if (c) {
            object_property_set_int(OBJECT(cpu), "rvbar",
                                    s->boot_el == 3 ? s->boot_entry & ~UINT64_C(0x1fffff)
                                                    : s->boot_entry, &error_fatal);
        }
        object_property_set_bool(OBJECT(cpu), "start-powered-off", c != 0, &error_fatal);
        object_property_set_bool(OBJECT(cpu), "has_el2", s->boot_el == 3, &error_fatal);
        object_property_set_bool(OBJECT(cpu), "has_el3", s->boot_el == 3, &error_fatal);
        object_property_set_int(OBJECT(cpu), "cntfrq", 24000000, &error_fatal);
        object_property_set_bool(OBJECT(cpu), "pmu", enable_pmu, &error_fatal);
        qdev_realize(DEVICE(cpu), NULL, &error_fatal);
    }
    s->cpu = s->ap_cpu[0];
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
        for (unsigned c = 0; c < ms->smp.cpus; c++) {
            define_one_arm_cp_reg(s->ap_cpu[c], &reg);
        }
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
        object_property_set_int(OBJECT(s->iop_cpu), "mp-affinity", 0x100, &error_fatal);
        object_property_set_int(OBJECT(s->iop_cpu), "cntfrq", 24000000, &error_fatal);
        object_property_set_bool(OBJECT(s->iop_cpu), "pmu", enable_pmu, &error_fatal);
        qdev_realize(DEVICE(s->iop_cpu), NULL, &error_fatal);
        warn_report("ANS IOP diagnostic: Cortex-A7 and bootstrap scratch are provisional; peripheral accesses fail");
    }
    memory_region_init_io(&s->uart, OBJECT(s), &uart_ops, s, "a7.uart0", 0x4000);
    memory_region_add_subregion(get_system_memory(), UART_BASE, &s->uart);
    for (unsigned c = 0; c < 2; c++) {
        memory_region_init_io(&s->cpu_mmio[c], OBJECT(s), &cpu_mmio_ops,
                              s->cpu_regs[c], c ? "a7.cpu1-regs" : "a7.cpu0-regs", 0x1000);
        memory_region_add_subregion(get_system_memory(),
                                    UINT64_C(0x202050000) + c * 0x100000,
                                    &s->cpu_mmio[c]);
    }
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
        /* EHCI provider reg[1]: USB control/config, distinct from PHY at
         * +0x20. Native AppleS5L8960XUSBEHCI gates clocks and stores cfg-incr
         * here (111193c..1968, 1111bcc..1c20). No busy bits or cable events.
         */
        { UINT64_C(0x20c000000), 0x10 },
        /* Full DT reg[0] reservations. QEMU's EHCI container has holes even
         * inside its 0x1000 bytes: Synopsys writes the HSIC port mask at
         * +0x2c0 (110c7e4). Retain vendor configuration in those holes and
         * the reserved tail; standard EHCI registers always take precedence.
         * No vendor busy/command poll was found in the native init paths. */
        { UINT64_C(0x20c200000), 0x10000 },
        { UINT64_C(0x20c400000), 0x10000 },
    };
    for (int i = 0; i < NUM_BRIDGES; i++) {
        char name[32];
        snprintf(name, sizeof(name), "a7.bridge.%d", i);
        s->bridges[i].base = bridge_defs[i].base;
        s->bridges[i].size = bridge_defs[i].size;
        s->bridges[i].regs = g_malloc0(bridge_defs[i].size);
        memory_region_init_io(&s->bridges[i].mr, OBJECT(s), &bridge_ops,
                              &s->bridges[i], name, bridge_defs[i].size);
        if (i >= NUM_BRIDGES - 2) {
            memory_region_add_subregion_overlap(get_system_memory(), bridge_defs[i].base,
                                                &s->bridges[i].mr, -1);
        } else {
            memory_region_add_subregion(get_system_memory(), bridge_defs[i].base,
                                        &s->bridges[i].mr);
        }
    }
    a7_usb_init(s);
    /* All remaining USB reg resources; no complex-wide catch-all. */
    memory_region_init_io(&s->usb_otg, OBJECT(s), &a7_otg_ops, s,
                          "a7.usb-device", 0x10000);
    memory_region_add_subregion(get_system_memory(), UINT64_C(0x20c100000),
                                &s->usb_otg);
    static const struct { hwaddr base; uint32_t size; } usb_aux_defs[] = {
        { UINT64_C(0x20c000010), 0x10 }, /* usb-device reg[1] */
        { UINT64_C(0x20c000050), 0x10 }, /* hsic-baseband */
        { UINT64_C(0x20c000060), 0x10 }, /* wlan HSIC: DT /arm-io/usb-complex/usb-ehci1/wlan reg[0] */
        { UINT64_C(0x20c300000), 0x10000 }, /* OHCI reservation */
    };
    for (unsigned i = 0; i < ARRAY_SIZE(usb_aux_defs); i++) {
        A7Bridge *b = &s->usb_aux[i];
        b->base = usb_aux_defs[i].base; b->size = usb_aux_defs[i].size;
        b->regs = g_malloc0(b->size);
        memory_region_init_io(&b->mr, OBJECT(s), &bridge_ops, b,
                              "a7.usb-aux", b->size);
        memory_region_add_subregion_overlap(get_system_memory(), b->base,
                                            &b->mr, -1);
    }
    DeviceState *ohci = qdev_new("sysbus-ohci");
    qdev_prop_set_uint32(ohci, "num-ports", 1);
    sysbus_realize_and_unref(SYS_BUS_DEVICE(ohci), &error_fatal);
    SYSBUS_OHCI(ohci)->ohci.as = &s->usb_dma_as;
    sysbus_mmio_map(SYS_BUS_DEVICE(ohci), 0, UINT64_C(0x20c300000));
    sysbus_connect_irq(SYS_BUS_DEVICE(ohci), 0,
                       qemu_allocate_irq(a7_usb_irq, s, 164));
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
        /* Register storage only, no Oscar ROM/firmware on UART2. The loader
         * omits its oscar nub, as it omits the absent modem/HSIC endpoint. */
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
    for (unsigned c = 0; c < ms->smp.cpus; c++) {
        qdev_connect_gpio_out(DEVICE(s->ap_cpu[c]), GTIMER_PHYS,
                              qemu_allocate_irq(timer_irq, s, c * 2));
        qdev_connect_gpio_out(DEVICE(s->ap_cpu[c]), GTIMER_VIRT,
                              qemu_allocate_irq(timer_irq, s, c * 2 + 1));
    }
    DeviceState *display = qdev_new(TYPE_A7_TOUCH_DISPLAY);
    display->id = g_strdup(TYPE_A7_TOUCH_DISPLAY);
    A7_TOUCH_DISPLAY(display)->board = s;
    sysbus_realize_and_unref(SYS_BUS_DEVICE(display), &error_fatal);
    s->con = qemu_graphic_console_create(display, 0, &display_ops, s);
    qemu_console_resize(s->con, 640, 1136);
    s->sio.irq_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, sio_irq_timer_cb, s);
    s->sep.irq_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, sep_irq_timer_cb, s);
    s->sep.os_active_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL,
                                         sep_os_active_timer_cb, s);
    s->pmu.adc_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, d2045_adc_done, s);
    s->touch_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, touch_timer_cb, s);
    s->mouse_touch_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, mouse_touch_timer_cb, s);
    const char *mouse_touch = getenv("VPHONE_MOUSE_TOUCH");
    s->mouse_touch_enabled = !mouse_touch || strcmp(mouse_touch, "0");
    if (s->mouse_touch_enabled) {
        QemuInputHandlerState *input =
            qemu_input_handler_register(display, &mouse_touch_handler);
        qemu_input_handler_bind(input, display->id, 0, &error_fatal);
        qemu_input_handler_activate(input);
    }
    s->vblank_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, vblank_timer_cb, s);
    s->agx_meta_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, agx_meta_alive_cb, s);
    s->agx_kick_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, agx_kick_cb, s);
    qemu_mutex_init(&s->agx_render_mutex);
    qemu_cond_init(&s->agx_render_cond);
    s->agx_done_bh = qemu_bh_new(agx_replay_done_bh, s);
    s->agx_job_head = NULL;
    s->agx_job_tail = NULL;
    s->agx_render_thread_running = false;
    s->agx_render_thread_exit = false;
    s->agx_inflight_jobs = 0;
    agx_render_ensure_thread(s);
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
    if (s->agx_render_thread_running) {
        qemu_mutex_lock(&s->agx_render_mutex);
        s->agx_render_thread_exit = true;
        qemu_cond_signal(&s->agx_render_cond);
        qemu_mutex_unlock(&s->agx_render_mutex);
        qemu_thread_join(&s->agx_render_thread);
        s->agx_render_thread_running = false;
        qemu_mutex_destroy(&s->agx_render_mutex);
        qemu_cond_destroy(&s->agx_render_cond);
    }
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
    mc->default_ram_size = GiB; mc->default_ram_id = "a7.ram"; mc->max_cpus = 2;
    mc->no_cdrom = true; mc->no_floppy = true;
}

static const TypeInfo a7_type = {
    .name = TYPE_A7, .parent = TYPE_MACHINE, .instance_size = sizeof(A7State),
    .instance_init = a7_instance_init, .instance_finalize = a7_finalize,
    .class_init = a7_class_init,
    .interfaces = aarch64_machine_interfaces,
};
static const TypeInfo a7_touch_display_type = {
    .name = TYPE_A7_TOUCH_DISPLAY,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(A7TouchDisplay),
};
static void a7_register(void)
{
    static const unsigned ports[] = { 3, 1 };
    static const TypeInfo ehci_types[] = {
        { .name = "a7.ehci0", .parent = TYPE_SYS_BUS_EHCI,
          .class_init = a7_ehci_class_init, .class_data = &ports[0] },
        { .name = "a7.ehci1", .parent = TYPE_SYS_BUS_EHCI,
          .class_init = a7_ehci_class_init, .class_data = &ports[1] },
    };
    for (unsigned i = 0; i < ARRAY_SIZE(ehci_types); i++) {
        type_register_static(&ehci_types[i]);
    }
    type_register_static(&a7_touch_display_type);
    type_register_static(&a7_type);
}
type_init(a7_register)
