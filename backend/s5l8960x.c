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
#include "system/runstate.h"
#include "system/system.h"
#include "system/tcg.h"
#include "chardev/char-fe.h"
#include "crypto/aes.h"
#include "ui/console.h"
#include "ui/input.h"
#include <sys/file.h>
#include <math.h>

#include "wifi_dongle.c"

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
    /* A press that arrived while the previous contact was draining. */
    bool queued, queued_up;
    int queued_x, queued_y;
} MouseTouch;

/* One reported finger. Path IDs stay stable for a contact's lifetime. */
typedef struct TouchContact {
    int x, y;
    unsigned state, path;
} TouchContact;
#define TOUCH_MAX_CONTACTS 5
#define TOUCH_FRAME_MAX (26 + 30 * TOUCH_MAX_CONTACTS)

/* QEMU MTT slots (trackpad scroll, pinch) share the MouseTouch cadence:
 * per-finger start/down, one frame per tick carrying every live finger,
 * and settle frames once the last finger lifts. */
#define MULTI_TOUCH_SLOTS 2
typedef struct MultiTouchSlot {
    int host_x, host_y, x, y, start_x, start_y;
    bool host_down, pressed, blocked, restart;
    unsigned phase; /* as MouseTouch */
} MultiTouchSlot;
typedef struct MultiTouch {
    MultiTouchSlot slot[MULTI_TOUCH_SLOTS];
    unsigned release_frames;
    int64_t due;
} MultiTouch;

static int mouse_touch_scale(int value, int maximum, int pixels)
{
    value = MAX(0, MIN(value, maximum));
    return (int)((int64_t)value * pixels / maximum);
}

static void mouse_touch_start(MouseTouch *m, int x, int y, int64_t now)
{
    m->x = m->start_x = x;
    m->y = m->start_y = y;
    m->phase = 1;
    m->pressed = true;
    m->dirty = false;
    m->release_frames = 0;
    m->due = now;
}

static void mouse_touch_sync(MouseTouch *m, bool allowed, int64_t now)
{
    if (m->button && !m->last_button && !m->phase && allowed) {
        mouse_touch_start(m, m->host_x, m->host_y, now);
    } else if (m->button && !m->last_button && m->phase && !m->pressed &&
               allowed) {
        /* Back-to-back click while the last contact drains: lift it on the
         * next tick instead of dropping this press, then start it afresh. */
        m->queued = true;
        m->queued_up = false;
        m->queued_x = m->host_x;
        m->queued_y = m->host_y;
        m->release_frames = 0;
    } else if (m->queued && !m->button) {
        m->queued_up = true;
    } else if (m->queued && m->button) {
        m->queued_x = m->host_x;
        m->queued_y = m->host_y;
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
        if (m->queued) {
            m->queued = false;
            mouse_touch_start(m, m->queued_x, m->queued_y, now + TOUCH_TICK_NS);
            if (m->queued_up) {
                m->pressed = false;
                m->release_frames = 3;
            }
            return true;
        }
    }
    m->due = now + TOUCH_TICK_NS;
    return true;
}

static bool multi_touch_active(const MultiTouch *m)
{
    for (unsigned i = 0; i < MULTI_TOUCH_SLOTS; i++) {
        if (m->slot[i].phase) { return true; }
    }
    return false;
}

static void multi_touch_start(MultiTouchSlot *c)
{
    c->x = c->start_x = c->host_x;
    c->y = c->start_y = c->host_y;
    c->phase = 1;
    c->pressed = true;
    c->restart = false;
}

static void multi_touch_sync(MultiTouch *m, bool allowed, int64_t now)
{
    bool was_active = multi_touch_active(m);
    bool any_pressed = false, lifted = false;
    for (unsigned i = 0; i < MULTI_TOUCH_SLOTS; i++) {
        MultiTouchSlot *c = &m->slot[i];
        if (!c->host_down) {
            c->blocked = c->restart = false;
        } else if (!c->phase && !c->blocked) {
            /* Like MouseTouch, an ignored begin needs a fresh one. */
            if (allowed) { multi_touch_start(c); } else { c->blocked = true; }
        } else if (c->phase && !c->pressed) {
            /* Re-touch while the previous contact drains (quick repeated
             * flicks): lift it on the next tick, then start afresh. */
            c->restart = true;
        }
        if (c->pressed) {
            c->x = c->host_x;
            c->y = c->host_y;
            if (!c->host_down) {
                c->pressed = false;
                lifted = true;
            }
        }
        any_pressed |= c->pressed;
    }
    /* Trackpad scroll/pinch lifts with the finger's real velocity. Holding the
     * contact still for settle frames (the mouse-swipe unlock path needs them)
     * zeroes UIScrollView's release velocity and freezes the scroll until the
     * frames drain, then it jumps on. */
    if (lifted && !any_pressed) { m->release_frames = 0; }
    if (!was_active && multi_touch_active(m)) { m->due = now; }
}

static unsigned multi_touch_next(MultiTouch *m, int64_t now, bool ready,
                                 TouchContact *out)
{
    unsigned n = 0;
    bool any_pressed = false, draining = false;
    if (!multi_touch_active(m) || now < m->due || !ready) { return 0; }
    for (unsigned i = 0; i < MULTI_TOUCH_SLOTS; i++) {
        any_pressed |= m->slot[i].pressed;
    }
    for (unsigned i = 0; i < MULTI_TOUCH_SLOTS; i++) {
        MultiTouchSlot *c = &m->slot[i];
        if (!c->phase) { continue; }
        TouchContact *o = &out[n++];
        o->path = i + 1;
        o->x = c->x; o->y = c->y;
        if (c->phase <= 2) {
            o->state = c->phase == 1 ? 1 : 3;
            o->x = c->start_x; o->y = c->start_y;
            c->phase++;
        } else if (c->phase == 3) {
            o->state = 4;
            if (!c->pressed) {
                /* Only the last lift settles; a finger leaving a live
                 * pinch, or one being re-touched, goes up at once. */
                if (!any_pressed && !c->restart && m->release_frames) {
                    draining = true;
                } else {
                    c->phase = 4;
                }
            }
        } else {
            o->state = 7;
            c->phase = 0;
            if (c->restart && c->host_down) { multi_touch_start(c); }
        }
    }
    if (draining) { m->release_frames--; }
    m->due = now + TOUCH_TICK_NS;
    return n;
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
    uint8_t frames[64][TOUCH_FRAME_MAX], frame_len[64];
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
    WifiDongle *wifi;
    char *wifi_netdev, *wifi_ssid, *wifi_scan_file;
    uint64_t wifi_rom;
    bool wifi_mac_patched;
    MemoryRegion usb_otg;
    uint32_t usb_regs[0x10000 / 4];
    A7Bridge usb_aux[4];
    A7Peripheral peripherals[NUM_PERIPHERALS];
    CharFrontend chr;
    QemuConsole *con;
    uint64_t boot_entry, boot_args, framebuffer, boot_el, iop_diagnostic, system_writable, display_trace;
    uint64_t disp_mmu_table;          /* DPU0MMUPGADDR0 << 4, 0 = linear scanout */
    uint32_t disp_stride, disp_offset;
    uint64_t disp_cached_mmu_table;
    uint32_t disp_cached_offset;
    uint64_t disp_cached_pa_base;
    uint32_t disp_page_ram_offset[2048];
    bool disp_mapping_valid;
    bool disp_mapping_contiguous;
    bool iop_started, secondary_started;
    unsigned iop_unknown_count;
    uint64_t apple_c15[2][8][16][8]; /* Storage only: hardware effects are NOT emulated. */
    uint32_t uart_reg[16], mask[32], soft[32], device_irq[32], targets[NIRQ];
    uint32_t pmgr_reg[PMGR_SIZE / 4];
    uint32_t pmgr_ext_reg[PMGR_EXT_SIZE / 4];
    uint8_t *gpio_regs;
    bool touch_irq_low;
    bool home_pressed;
    QEMUTimer *home_timer;
    bool hold_pressed;          /* sleep/wake ("hold") button, GPIO3 */
    bool backlight_off;         /* LM3534 disabled: the guest display sleeps */
    int64_t hold_down_ns;
    QEMUTimer *hold_timer;
    QEMUTimer *touch_timer;
    QEMUTimer *mouse_touch_timer;
    MouseTouch mouse_touch;
    MultiTouch multi_touch;
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
    int64_t vblank_deadline_ns;
    /* Wall-clock interval histograms: 100 us bins, final bin overflows. */
    struct {
        int64_t last_ns, first_start_host_ns;
        uint64_t bins[1024], count, max_ns;
    } pacing[4]; /* vblank, completed swap, lateness, request-to-complete */
    int64_t disp_request_host_ns;
    int64_t pacing_report_ns;
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
    unsigned agx_frame_dump_window;   /* 1 + current DUMP_AT index, 0 before the first */
    unsigned agx_frame_dump_in_window;
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
    QEMUBH *agx_kick_bh;
    bool agx_stamp_wait;
    bool agx_async_enabled;
    bool agx_render_thread_running;
    bool agx_render_thread_exit;
    unsigned agx_inflight_jobs;
    /* Queue read index already consumed on the host but not yet published to
     * the guest (published by agx_replay_done_bh after the pixels land).
     * A drain before then must continue from here, not re-read the guest's
     * stale index, or it would replay and stamp the same commands twice. */
    struct { uint64_t control; uint32_t index; bool valid; } agx_host_read[16];
    /* AGX submission/latency perf stats (VPHONE_PERF_STATS=1) */
    bool agx_perf_enabled;
    QEMUTimer *agx_perf_timer;
    int64_t agx_doorbell_time_us;
    bool agx_doorbell_in_flight;
    uint64_t agx_perf_total_doorbells;
    uint64_t agx_perf_total_passes;
    uint64_t agx_perf_total_jobs;
    uint64_t agx_perf_total_repolls_1ms;
    uint64_t agx_perf_win_doorbells;
    uint64_t agx_perf_win_passes;
    uint64_t agx_perf_win_jobs;
    uint64_t agx_perf_win_repolls_1ms;
    uint64_t agx_perf_win_capture_ta_us;
    uint64_t agx_perf_win_queue_wait_us_sum;
    uint64_t agx_perf_win_queue_wait_count;
    uint64_t agx_perf_win_render_us_sum;
    uint64_t agx_perf_win_render_count;
    uint64_t agx_perf_win_bh_delay_us_sum;
    uint64_t agx_perf_win_bh_delay_count;
    uint64_t agx_perf_win_lat_us_sum;
    uint32_t agx_perf_win_lat_count;
    int64_t agx_perf_win_lat_samples[16384];
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
    /* ANS worker state is protected by ans_mutex; timers/DMA require BQL. */
    unsigned ans_dirty;
    QemuMutex ans_mutex;
    QemuCond ans_cond;
    QemuThread ans_thread;
    bool ans_worker_started, ans_worker_stop, ans_worker_pending, ans_worker_busy;
    unsigned ans_worker_errors;
    uint64_t ans_generation[3];
    uint64_t ans_hist[64], ans_handler_max_ns;
    uint64_t ans_window_hist[64], ans_window_max_ns;
    uint64_t ans_report_requests, ans_report_fsyncs;
    QEMUTimer *ans_flush_timer;
    Notifier ans_exit_notifier, ans_shutdown_notifier;
    bool ans_notifiers_registered, ans_perf_enabled;
    uint64_t ans_requests, ans_bytes, ans_fsyncs, ans_handler_ns;
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
    if (s->wifi) {
        config = ldl_le_p(s->gpio_regs + 167 * 4);
        group = (config >> 16) & 0xff;
        if (group < 7 && (config & 0xe) == 4) {
            uint8_t *status = s->gpio_regs + 0x800 + group * 0x40 + (167 / 32) * 4;
            stl_le_p(status, ldl_le_p(status) | (1u << (167 % 32)));
        }
    }
    /* Level-triggered push buttons: Home (GPIO2) and hold (GPIO3). */
    for (unsigned pin = 2; pin <= 3; pin++) {
        bool pressed = pin == 2 ? s->home_pressed : s->hold_pressed;
        config = ldl_le_p(s->gpio_regs + pin * 4);
        group = (config >> 16) & 0xff;
        type = config & 0xe;
        if (group < 7 && ((type == 6 && pressed) || (type == 4 && !pressed))) {
            uint8_t *status = s->gpio_regs + 0x800 + group * 0x40;
            stl_le_p(status, ldl_le_p(status) | (1u << pin));
        }
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

/* Push buttons are active-low: Home on GPIO2, hold (sleep/wake) on GPIO3.
 * Respect the guest's edge/level selection. */
static void button_level(A7State *s, unsigned pin, bool *state, bool pressed)
{
    uint32_t config = ldl_le_p(s->gpio_regs + pin * 4);
    unsigned group = (config >> 16) & 0xff, type = config & 0xe;
    bool edge = *state != pressed &&
        (type == 0xc || (type == 0xa && pressed) || (type == 8 && !pressed));
    if (group < 7 && edge) {
        uint8_t *status = s->gpio_regs + 0x800 + group * 0x40;
        stl_le_p(status, ldl_le_p(status) | (1u << pin));
    }
    qemu_log_mask(LOG_UNIMP, "A7 BUTTON pin=%u pressed=%d config=0x%08x edge=%d ns=%" PRId64 "\n",
                  pin, pressed, config, edge, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
    *state = pressed;
    touch_gpio_update(s);
}

static void home_level(A7State *s, bool pressed)
{
    button_level(s, 2, &s->home_pressed, pressed);
}

static void home_release(void *opaque)
{
    home_level(opaque, false);
}

static void hold_release(void *opaque)
{
    A7State *s = opaque;
    button_level(s, 3, &s->hold_pressed, false);
}

/* Sleep/wake key from the host. A click is a 100 ms press (lock or wake);
 * holding the key holds the button (slide to power off), with a 10 s guard
 * against a lost key-up. */
static void hold_key(A7State *s, bool down)
{
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (down && !s->hold_pressed) {
        button_level(s, 3, &s->hold_pressed, true);
        s->hold_down_ns = now;
        timer_mod(s->hold_timer, now + NANOSECONDS_PER_SECOND * 10);
    } else if (!down && s->hold_pressed) {
        timer_mod(s->hold_timer, MAX(now, s->hold_down_ns + 100000000));
    }
}

static bool touch_irq_latched(A7State *s)
{
    unsigned group = (ldl_le_p(s->gpio_regs + 22 * 4) >> 16) & 0xff;
    return group >= 7 || (ldl_le_p(s->gpio_regs + 0x800 + group * 0x40) & (1u << 22));
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
            if (off + i == 167 * 4 && s->wifi) {
                byte = (byte & ~1) | 1;
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
                    byte = (byte & ~1) | (pin == 2 ? !s->home_pressed :
                                          pin == 3 ? !s->hold_pressed : sample);
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
        if (s->wifi && off < 167 * 4 && off + size > 166 * 4) {
            wifi_host_ready(s->wifi, ldl_le_p(s->gpio_regs + 166 * 4) & 1);
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
    /* GPIO22 is edge-triggered. When MultitouchHID configures it while the
     * line is already low, nothing latches and the guest never reads, so the
     * FIFO only grows (queued=1,2,3... from the first tap; the unlock swipe
     * then never arrives). With frames waiting and no status pending, pulse
     * the line so the driver sees a fresh edge. */
    if (t->frame_count >= 2 && t->owner->touch_irq_low && !touch_irq_latched(t->owner)) {
        touch_gpio_level(t->owner, false);
    }
    touch_gpio_level(t->owner, true);
}

/* MultitouchSupport 187ccdfb0 -> 187ccf8e0: report 74, variable header
 * and contact stride; D3 endian=1 selects LE at 187ccf9a0. Full 30-byte
 * contact avoids the derived-force fallback used for 20-byte records.
 * Header 187ccf708: u16 at 12 is the TOTAL contact bytes and byte 14 the
 * count; the unpacker derives stride = total / count and rejects a rest. */
static void touch_contacts(A7Touch *t, const TouchContact *c, unsigned n)
{
    uint8_t f[TOUCH_FRAME_MAX] = { 0x74, t->sequence++, 26, 4 };
    uint32_t ts = (uint32_t)(qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) / 1000);
    n = MIN(n, TOUCH_MAX_CONTACTS);
    if (ts <= t->last_timestamp_us) {
        ts = t->last_timestamp_us + 1;
    }
    t->last_timestamp_us = ts;
    stl_le_p(f + 4, ts);
    f[10] = 4; /* contact frame, flags[1:0]=0 -> type 6 */
    stw_le_p(f + 12, 30 * n);
    f[14] = n;
    for (unsigned i = 0; i < n; i++) {
        uint8_t *p = f + 26 + 30 * i;
        bool up = c[i].state == 7;
        /* Binary+2 -> MTContact+18 is identity, not flags. Zero is
         * unidentified and fails MTParserPath::isFingerContact (HID a134).
         * Identity 2.. are fingers (1..5); path IDs stay stable per contact. */
        p[0] = c[i].path; p[1] = c[i].state; p[2] = 2 + i; p[3] = 1;
        stw_le_p(p + 4, c[i].x);
        stw_le_p(p + 6, 1136 - c[i].y); /* user syntax is top-left pixels */
        stw_le_p(p + 12, up ? 0 : 400); /* ellipse radii, 1/100 mm */
        stw_le_p(p + 14, up ? 0 : 300);
        stw_le_p(p + 18, up ? 0 : 256); /* force */
        /* Full-stride extensions are density and quality, NOT radii:
         * 187cd1da0..1dbc/1d78..1d8c scale these by 1/256. */
        stw_le_p(p + 20, up ? 0 : 256); /* density */
        stw_le_p(p + 22, up ? 0 : 256); /* related density */
        stw_le_p(p + 24, up ? 0 : 256); /* quality */
        stw_le_p(p + 26, up ? 0 : 256); /* signal */
    }
    touch_enqueue(t, f, 26 + 30 * n);
}

static void touch_contact(A7Touch *t, unsigned state, int x, int y)
{
    TouchContact c = { x, y, state, 1 };
    touch_contacts(t, &c, 1);
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
    for (unsigned i = 0; i < MULTI_TOUCH_SLOTS; i++) {
        MultiTouchSlot *c = &t->owner->multi_touch.slot[i];
        c->phase = 0;
        c->pressed = c->restart = false;
        c->blocked = c->host_down;
    }
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
    MultiTouch *mt = &s->multi_touch;
    A7Touch *t = touch_device(s);
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    TouchScriptFrame f;
    TouchContact c[MULTI_TOUCH_SLOTS];
    unsigned n;
    bool ready = t && (s->touch_force || touch_reporting_enabled(t)) &&
                 t->frame_count < ARRAY_SIZE(t->frames);
    if ((n = multi_touch_next(mt, now, ready, c))) {
        touch_contacts(t, c, n);
        qemu_log_mask(LOG_UNIMP, "A7 MTOUCH n=%u state=%u x=%d y=%d "
                      "rel=%u queued=%u ns=%" PRId64 "\n",
                      n, c[0].state, c[0].x, c[0].y, mt->release_frames,
                      t->frame_count, now);
    }
    if (mouse_touch_next(m, now, ready, &f)) {
        touch_contact(t, f.state, f.x, f.y);
        qemu_log_mask(LOG_UNIMP, "A7 MOUSE state=%u x=%d y=%d phase=%u rel=%u "
                      "queued=%u ns=%" PRId64 "\n",
                      f.state, f.x, f.y, m->phase, m->release_frames,
                      t->frame_count, now);
    }
    if (m->phase != 0 || multi_touch_active(mt)) {
        int64_t due = m->phase ? m->due : mt->due;
        timer_mod(s->mouse_touch_timer, due > now ? due : now + TOUCH_TICK_NS);
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
    if (evt->type == INPUT_EVENT_KIND_KEY) {
        if (qemu_input_linux_to_qcode(evt->key.key) == Q_KEY_CODE_HOME) {
            if (evt->key.down && !s->home_pressed) {
                home_level(s, true);
                /* Guest-time release also protects against lost host key-up. */
                timer_mod(s->home_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 100000000);
            }
            /* Release is timer-owned: a stalled host must not create a long press. */
        } else if (qemu_input_linux_to_qcode(evt->key.key) == Q_KEY_CODE_POWER) {
            hold_key(s, evt->key.down);
        }
    } else if (evt->type == INPUT_EVENT_KIND_ABS) {
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
    } else if (evt->type == INPUT_EVENT_KIND_MTT &&
               evt->mtt.slot >= 0 && evt->mtt.slot < MULTI_TOUCH_SLOTS) {
        MultiTouchSlot *c = &s->multi_touch.slot[evt->mtt.slot];
        if (evt->mtt.type == INPUT_MULTI_TOUCH_TYPE_BEGIN) {
            c->host_down = true;
        } else if (evt->mtt.type == INPUT_MULTI_TOUCH_TYPE_END ||
                   evt->mtt.type == INPUT_MULTI_TOUCH_TYPE_CANCEL) {
            c->host_down = false;
        } else if (evt->mtt.type == INPUT_MULTI_TOUCH_TYPE_DATA &&
                   evt->mtt.axis == INPUT_AXIS_X) {
            c->host_x = mouse_touch_scale(evt->mtt.value, INPUT_EVENT_ABS_MAX, 640);
        } else if (evt->mtt.type == INPUT_MULTI_TOUCH_TYPE_DATA &&
                   evt->mtt.axis == INPUT_AXIS_Y) {
            int y = mouse_touch_scale(evt->mtt.value, INPUT_EVENT_ABS_MAX, 1136);
            c->host_y = MIN(1136, y + 16); /* same fingertip shift as above */
        }
    }
}

static void mouse_touch_input_sync(DeviceState *dev)
{
    A7State *s = A7_TOUCH_DISPLAY(dev)->board;
    A7Touch *t = touch_device(s);
    bool allowed = s->mouse_touch_enabled && t &&
                   (s->touch_force || touch_reporting_enabled(t)) &&
                   s->touch_step >= s->touch_script.count;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    /* Mouse and MTT gestures own the digitizer one at a time. */
    bool multi = multi_touch_active(&s->multi_touch);
    mouse_touch_sync(&s->mouse_touch, allowed && !multi, now);
    multi_touch_sync(&s->multi_touch, allowed && !s->mouse_touch.phase, now);
    mouse_touch_timer_cb(s);
}

static const QemuInputHandler mouse_touch_handler = {
    .name = "A7 touchscreen mouse",
    .mask = INPUT_EVENT_MASK_KEY | INPUT_EVENT_MASK_ABS | INPUT_EVENT_MASK_BTN | INPUT_EVENT_MASK_MTT,
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
    if ((reg >= 0x70 && reg < 0x7c) || (reg >= 0x180 && reg < 0x190)) {
        qemu_log_mask(LOG_UNIMP, "PMU write reg=0x%x val=0x%02x ns=%" PRId64 "\n",
                      reg, value, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
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
                qemu_log_mask(LOG_UNIMP, "LM3534 reg 0x%x = 0x%02x\n", reg, value & 0xff);
                /* Control register bit 0 enables the backlight. iOS clears it
                 * (after the DWI fade) when the display sleeps and sets it on
                 * wake; the player shows a locked screen meanwhile. */
                if ((reg & 3) == 0) {
                    p->owner->backlight_off = !(value & 1);
                }
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
            if ((reg >= 0x180 && reg < 0x190) || (reg >= 0x50 && reg < 0x5c && byte)) {
                qemu_log_mask(LOG_UNIMP, "PMU read reg=0x%x val=0x%02x ns=%" PRId64 "\n",
                              reg, byte, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
            }
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

/* Return the prefix transferred, including on a short/erroring syscall. */
static size_t ans_file_transfer(int fd, uint8_t *buf, off_t offset,
                                size_t length, bool write)
{
    size_t done = 0;
    while (done < length) {
        ssize_t n = write ? pwrite(fd, buf + done, length - done, offset + done)
                          : pread(fd, buf + done, length - done, offset + done);
        if (n < 0 && errno == EINTR) { continue; }
        if (n <= 0) { break; }
        done += n;
    }
    return done;
}

static bool ans_file_io(int fd, uint8_t *buf, off_t offset, bool write)
{
    return ans_file_transfer(fd, buf, offset, ANS_SECTOR_SIZE, write) == ANS_SECTOR_SIZE;
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

#define ANS_WRITEBACK_MS 300

/* Worker never touches guest memory, timers, BQL, or error_report. */
static void *ans_worker(void *opaque)
{
    A7State *s = opaque;
    int fds[] = { s->data_fd, s->utility_fd, s->sys_fd };
    qemu_mutex_lock(&s->ans_mutex);
    for (;;) {
        while (!s->ans_worker_pending && !s->ans_worker_stop) {
            qemu_cond_wait(&s->ans_cond, &s->ans_mutex);
        }
        if (s->ans_worker_stop) { break; }
        s->ans_worker_pending = false;
        s->ans_worker_busy = true;
        for (unsigned i = 0; i < ARRAY_SIZE(fds); i++) {
            if (!(s->ans_dirty & (1U << i))) { continue; }
            uint64_t generation = s->ans_generation[i];
            int ret;
            do {
                s->ans_fsyncs++;
                qemu_mutex_unlock(&s->ans_mutex);
                ret = fsync(fds[i]);
                int saved_errno = errno;
                qemu_mutex_lock(&s->ans_mutex);
                if (ret >= 0 || saved_errno != EINTR) { break; }
            } while (true);
            if (ret < 0) { s->ans_worker_errors |= 1U << i; }
            else if (generation == s->ans_generation[i]) {
                s->ans_dirty &= ~(1U << i);
            }
        }
        s->ans_worker_busy = false;
        qemu_cond_broadcast(&s->ans_cond);
    }
    qemu_mutex_unlock(&s->ans_mutex);
    return NULL;
}

static void ans_worker_start(A7State *s)
{
    if (s->ans_worker_started) { return; }
    qemu_mutex_init(&s->ans_mutex);
    qemu_cond_init(&s->ans_cond);
    s->ans_worker_started = true;
    qemu_thread_create(&s->ans_thread, "ans-writeback", ans_worker, s,
                       QEMU_THREAD_JOINABLE);
}

static bool ans_flush(A7State *s)
{
    if (!s->ans_worker_started) { return true; }
    qemu_mutex_lock(&s->ans_mutex);
    while (s->ans_worker_busy || s->ans_worker_pending) {
        qemu_cond_wait(&s->ans_cond, &s->ans_mutex);
    }
    unsigned errors = s->ans_worker_errors;
    s->ans_worker_errors = 0;
    if (s->ans_dirty) {
        s->ans_worker_pending = true;
        qemu_cond_signal(&s->ans_cond);
        while (s->ans_worker_pending || s->ans_worker_busy) {
            qemu_cond_wait(&s->ans_cond, &s->ans_mutex);
        }
    }
    errors |= s->ans_worker_errors;
    s->ans_worker_errors = 0;
    bool clean = !s->ans_dirty;
    qemu_mutex_unlock(&s->ans_mutex);
    if (errors) { error_report("ANS fsync failed; barrier reports storage error"); }
    if (clean && s->ans_flush_timer) { timer_del(s->ans_flush_timer); }
    return clean && !errors;
}

static void ans_worker_stop(A7State *s)
{
    if (!s->ans_worker_started) { return; }
    ans_flush(s);
    qemu_mutex_lock(&s->ans_mutex);
    s->ans_worker_stop = true;
    qemu_cond_signal(&s->ans_cond);
    qemu_mutex_unlock(&s->ans_mutex);
    qemu_thread_join(&s->ans_thread);
    qemu_mutex_destroy(&s->ans_mutex);
    qemu_cond_destroy(&s->ans_cond);
    s->ans_worker_started = false;
}

static void ans_mark_dirty(A7State *s, int fd)
{
    ans_worker_start(s);
    unsigned i = fd == s->data_fd ? 0 : fd == s->utility_fd ? 1 : 2;
    qemu_mutex_lock(&s->ans_mutex);
    bool first = !s->ans_dirty;
    s->ans_generation[i]++;
    s->ans_dirty |= 1U << i;
    qemu_mutex_unlock(&s->ans_mutex);
    if (first && s->ans_flush_timer) {
        timer_mod(s->ans_flush_timer,
                  qemu_clock_get_ms(QEMU_CLOCK_REALTIME) + ANS_WRITEBACK_MS);
    }
}

static void ans_flush_timer_cb(void *opaque)
{
    A7State *s = opaque;
    if (!s->ans_worker_started) { return; }
    qemu_mutex_lock(&s->ans_mutex);
    if (s->ans_worker_errors) {
        error_report("ANS deferred fsync failed; error retained for barrier");
    }
    if (s->ans_dirty) {
        s->ans_worker_pending = true;
        qemu_cond_signal(&s->ans_cond);
    }
    bool active = s->ans_dirty || s->ans_worker_busy;
    qemu_mutex_unlock(&s->ans_mutex);
    if (active) {
        timer_mod(s->ans_flush_timer,
                  qemu_clock_get_ms(QEMU_CLOCK_REALTIME) + ANS_WRITEBACK_MS);
    }
}

/* Log2 buckets: percentile values are inclusive upper bounds in ns. */
static uint64_t ans_percentile(const uint64_t *hist, uint64_t count, unsigned pct)
{
    if (!count) { return 0; }
    uint64_t target = count / 100 * pct + (count % 100 * pct + 99) / 100;
    uint64_t seen = 0;
    for (unsigned i = 0; i < 64; i++) {
        seen += hist[i];
        if (seen >= target) { return i == 63 ? UINT64_MAX : (UINT64_C(1) << i) - 1; }
    }
    return UINT64_MAX;
}

/* Reset/close cannot return an ASP status: report failures, retain dirty bits. */
static void ans_drain(A7State *s)
{
    if (!ans_flush(s)) { error_report("ANS lifecycle fsync failed"); }
    if (s->ans_perf_enabled) {
        fprintf(stderr, "ANS perf: requests=%" PRIu64 " bytes=%" PRIu64
                " fsyncs=%" PRIu64 " handler_ns=%" PRIu64
                " handler_p50_ns_le=%" PRIu64 " handler_p95_ns_le=%" PRIu64
                " handler_max_ns=%" PRIu64 " background_fsync_bql_ns=0\n",
                s->ans_requests, s->ans_bytes, s->ans_fsyncs, s->ans_handler_ns,
                ans_percentile(s->ans_hist, s->ans_requests, 50),
                ans_percentile(s->ans_hist, s->ans_requests, 95),
                s->ans_handler_max_ns);
    }
}

static void ans_exit_notify(Notifier *n, void *data)
{
    A7State *s = container_of(n, A7State, ans_exit_notifier);
    (void)data;
    ans_drain(s);
}

static void ans_shutdown_notify(Notifier *n, void *data)
{
    A7State *s = container_of(n, A7State, ans_shutdown_notifier);
    (void)data;
    ans_drain(s);
}

/* A contiguous run ends at a backing boundary or a synthetic GPT sector. */
static int ans_map(A7State *s, const AnsUtilityRegion *region,
                   uint64_t current, off_t *offset, uint32_t *run)
{
    if (region) {
        *offset = (region->first_page + current) * ANS_SECTOR_SIZE;
        *run = MIN(*run, region->page_count - current);
        return s->utility_fd;
    }
    if (current >= ANS_SYS_START && current < s->data_start) {
        *offset = (current - ANS_SYS_START) * ANS_SECTOR_SIZE;
        *run = MIN(*run, s->data_start - current);
        return s->sys_fd;
    }
    if (current >= s->data_start && current < s->data_start + s->data_lbas) {
        *offset = (current - s->data_start) * ANS_SECTOR_SIZE;
        *run = MIN(*run, s->data_start + s->data_lbas - current);
        return s->data_fd;
    }
    *run = 1;
    return -1;
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

static uint32_t ans_execute_request(A7State *s, uint8_t tag)
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
    uint8_t ppns[2048 * 4];
    if (!ans_read_ram(cmd_addr + 0x30, ppns, count * 4)) { return 1; }
    for (uint32_t i = 0; i < count; i++) {
        pages[i] = (hwaddr)ldl_le_p(ppns + i * 4) << 12;
        if (!ans_ram_range(pages[i], ANS_SECTOR_SIZE)) { return 1; }
        uint64_t current = (uint64_t)lba + i;
        if (write && !region && !((current >= ANS_SYS_START && current < s->data_start && s->system_writable) ||
                       (current >= s->data_start && current < s->data_start + s->data_lbas))) {
            return 5; /* GPT/reserved sectors and read-only System cannot discard writes. */
        }
    }
    qemu_log_mask(LOG_UNIMP, "ANS modeled I/O opcode=0x%02x %s page=%u count=%u\n", opcode, write ? "WRITE" : "READ", lba, count);
    /* At most 8 MiB (tag31); gather/scatter preserves page ordering and aliases.
     * Only complete sectors of a failed read reach guest RAM, as before. */
    uint8_t *batch = g_malloc((size_t)count * ANS_SECTOR_SIZE);
    for (uint32_t i = 0; i < count;) {
        uint64_t current = (uint64_t)lba + i;
        uint32_t run = count - i;
        off_t offset = 0;
        int fd = ans_map(s, region, current, &offset, &run);
        size_t length = (size_t)run * ANS_SECTOR_SIZE;
        if (write) {
            for (uint32_t j = 0; j < run;) {
                uint32_t n = 1;
                while (j + n < run &&
                       pages[i + j + n] == pages[i + j] + n * ANS_SECTOR_SIZE) { n++; }
                if (!ans_read_ram(pages[i + j], batch + j * ANS_SECTOR_SIZE,
                                   n * ANS_SECTOR_SIZE)) { status = 5; break; }
                j += n;
            }
            if (status) { break; }
            size_t done = ans_file_transfer(fd, batch, offset, length, true);
            /* Publish after I/O, including failures that modified a prefix:
             * an overlapping worker fsync must not clear this generation. */
            ans_mark_dirty(s, fd);
            if (s->ans_perf_enabled) { s->ans_bytes += done; }
            if (done != length) { status = 5; break; }
        } else {
            size_t done = length;
            if (fd >= 0) {
                done = ans_file_transfer(fd, batch, offset, length, false);
                if (s->ans_perf_enabled) { s->ans_bytes += done; }
            } else {
                if (current == 0) { memcpy(buf, s->gpt_lba0, sizeof(buf)); }
                else if (current == 1) { memcpy(buf, s->gpt_lba1, sizeof(buf)); }
                else if (current == 2 || current == s->total_lbas - 2) { memcpy(buf, s->gpt_lba2, sizeof(buf)); }
                else if (current == s->total_lbas - 1) { memcpy(buf, s->gpt_alt_hdr, sizeof(buf)); }
                else { memset(buf, 0, sizeof(buf)); }
                memcpy(batch, buf, sizeof(buf));
            }
            for (uint32_t j = 0; j < done / ANS_SECTOR_SIZE;) {
                uint32_t n = 1;
                while (j + n < done / ANS_SECTOR_SIZE &&
                       pages[i + j + n] == pages[i + j] + n * ANS_SECTOR_SIZE) { n++; }
                if (!ans_write_ram(pages[i + j], batch + j * ANS_SECTOR_SIZE,
                                   n * ANS_SECTOR_SIZE)) { status = 5; break; }
                j += n;
            }
            if (done != length) { status = 5; }
            if (status) { break; }
        }
        i += run;
    }
    g_free(batch);
    /* Write completion means host page-cache acceptance. Opcode13 and lifecycle
     * drains are durability barriers; REALTIME write-back does not retime guest. */
    return status;
}

static uint32_t ans_execute(A7State *s, uint8_t tag)
{
    int64_t start = s->ans_perf_enabled ? qemu_clock_get_ns(QEMU_CLOCK_REALTIME) : 0;
    uint32_t status = ans_execute_request(s, tag);
    if (s->ans_perf_enabled) {
        s->ans_requests++;
        uint64_t elapsed = qemu_clock_get_ns(QEMU_CLOCK_REALTIME) - start;
        s->ans_handler_ns += elapsed;
        if (elapsed > s->ans_handler_max_ns) { s->ans_handler_max_ns = elapsed; }
        unsigned bucket = 0;
        for (uint64_t n = elapsed; n && bucket < 63; n >>= 1) { bucket++; }
        s->ans_hist[bucket]++;
        s->ans_window_hist[bucket]++;
        if (elapsed > s->ans_window_max_ns) { s->ans_window_max_ns = elapsed; }
    }
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
            qemu_log_mask(LOG_UNIMP, "DWI go off=0x%" HWADDR_PRIx " word=0x%08x\n",
                          off, ldl_le_p(p->regs + off + 4));
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
                        if (s->agx_perf_enabled && !s->disp_swap_pending) {
                            s->disp_request_host_ns = qemu_clock_get_ns(QEMU_CLOCK_REALTIME);
                        }
                        s->disp_swap_pending = true;
                        qemu_log_mask(LOG_UNIMP, "DPFIFO applied words=%u token=%u\n", count, s->disp_fifo_token);
                    }
                }
                return;
            }
            if (off == 0x1050) {
                qemu_log_mask(LOG_UNIMP, "[DISP0] swap commit off=0x1050 val=0x%" PRIx64 ", pending_token=%u\n",
                        val, p->owner->disp_pending_token);
                *(uint32_t *)(p->regs + 0x1044) = 0;
                if (p->owner->agx_perf_enabled && !p->owner->disp_swap_pending) {
                    p->owner->disp_request_host_ns = qemu_clock_get_ns(QEMU_CLOCK_REALTIME);
                }
                p->owner->disp_swap_pending = true;
                p->owner->disp_mapping_valid = false;
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
                p->owner->disp_mapping_valid = false;
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
                s->disp_mapping_valid = false;
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
#ifdef QEMU_OSDEP_H
    if (cat_base >= RAM_BASE && cat_base < RAM_BASE + GiB) {
        uint8_t *ram = memory_region_get_ram_ptr(MACHINE(s)->ram);
        uint64_t pce_addr = cat_base + ((gva >> 30) & 0x3ff) * 4;
        if (pce_addr + 4 <= RAM_BASE + GiB) {
            uint32_t pce = ldl_le_p(ram + pce_addr - RAM_BASE);
            if (pce & 1) {
                uint64_t pde_addr = ((uint64_t)(pce >> 4) << 12) + ((gva >> 21) & 0x1ff) * 8;
                if (pde_addr >= RAM_BASE && pde_addr + 8 <= RAM_BASE + GiB) {
                    uint64_t pde = ldq_le_p(ram + pde_addr - RAM_BASE);
                    if (pde & 1) {
                        uint64_t pte_addr = (pde & UINT64_C(0xfffffff000)) + ((gva >> 12) & 0x1ff) * 8;
                        if (pte_addr >= RAM_BASE && pte_addr + 8 <= RAM_BASE + GiB) {
                            uint64_t pte = ldq_le_p(ram + pte_addr - RAM_BASE);
                            if (pte & 1) {
                                *pa = (pte & UINT64_C(0xfffffff000)) | (gva & 0xfff);
                                return true;
                            }
                            return false;
                        }
                    }
                }
            }
        }
    }
#endif
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
    static int pbe_search_enabled = -1;
    uint64_t seen_gva[16], seen_cat[16];
    unsigned buffers = 0;
    uint32_t ctx = ldl_le_p(raw + 4);
    uint64_t ctx_cat = ctx < ARRAY_SIZE(s->agx_ctx_cat) - 3 ?
                       s->agx_ctx_cat[ctx + 3] : 0;
    uint8_t bytes[0x10000];

    if (__builtin_expect(pbe_search_enabled < 0, 0)) {
        pbe_search_enabled = getenv("VPHONE_AGX_PBE_SEARCH") != NULL;
    }
    if (!pbe_search_enabled || commands >= 200) {
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

#ifdef QEMU_OSDEP_H
typedef struct AGXTLBEntry {
    uint64_t gva_page;     /* (gva & ~0xfffULL) */
    uint64_t cat_base;     /* page table root */
    hwaddr pa_page;        /* physical address base */
    uint8_t *host_ptr;     /* host RAM pointer */
    uint32_t gen;          /* generation counter */
} AGXTLBEntry;

#define AGX_TLB_SIZE 1024
#define AGX_TLB_MASK (AGX_TLB_SIZE - 1)

static __thread AGXTLBEntry agx_tlb[AGX_TLB_SIZE];
static __thread uint32_t agx_tlb_gen;

static inline void agx_tlb_reset(void)
{
    agx_tlb_gen++;
    if (agx_tlb_gen == 0) {
        memset(agx_tlb, 0, sizeof(agx_tlb));
        agx_tlb_gen = 1;
    }
}

static inline AGXTLBEntry *agx_tlb_lookup(A7State *s, uint64_t cat, uint64_t gva_page)
{
    unsigned idx = (gva_page >> 12) & AGX_TLB_MASK;
    AGXTLBEntry *e = &agx_tlb[idx];
    if (e->gen == agx_tlb_gen && e->cat_base == cat && e->gva_page == gva_page) {
        return e;
    }
    hwaddr pa;
    if (gva_page >= (UINT64_C(1) << 40) || !agx_gpu_translate_ctx(s, cat, gva_page, &pa)) {
        return NULL;
    }
    if (pa < RAM_BASE || pa >= RAM_BASE + GiB) {
        return NULL;
    }
    uint8_t *ram = memory_region_get_ram_ptr(MACHINE(s)->ram);
    e->gva_page = gva_page;
    e->cat_base = cat;
    e->pa_page = pa;
    e->host_ptr = ram + (pa - RAM_BASE);
    e->gen = agx_tlb_gen;
    return e;
}
#endif

/* Row-wise counterpart of agx_ctx_read: one page walk and one memory-API
 * write per page instead of a walk plus a store per pixel, which made the
 * replay hold the BQL for most of each frame (probe-162 host profile). */
static bool agx_ctx_write(A7State *s, uint64_t cat, uint64_t gva, const void *buf, size_t n)
{
    const uint8_t *in = buf;
    bool ok = true;
    while (n) {
        size_t chunk = MIN(n, 0x1000 - (gva & 0xfff));
#ifdef QEMU_OSDEP_H
        uint64_t base = gva & ~UINT64_C(0xfff);
        AGXTLBEntry *tlb = agx_tlb_lookup(s, cat, base);
        if (tlb) {
            /* Reuse the cached translation but keep address_space_write: a
             * raw memcpy plus memory_region_set_dirty marks code pages dirty
             * without invalidating their TBs, so recycled pages ran stale
             * translated code (Settings launched seconds late). */
            if (address_space_write(&address_space_memory, tlb->pa_page + (gva & 0xfff),
                                    MEMTXATTRS_UNSPECIFIED, in, chunk) != MEMTX_OK) {
                ok = false;
            }
        } else
#endif
        {
            hwaddr pa;
            /* Like the old per-pixel stores, skip only the unmapped pages. */
            if (gva >= (UINT64_C(1) << 40) || !agx_gpu_translate_ctx(s, cat, gva, &pa) ||
                address_space_write(&address_space_memory, pa, MEMTXATTRS_UNSPECIFIED,
                                    in, chunk) != MEMTX_OK) {
                ok = false;
            }
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
        /* AT lists virtual-clock seconds, one per screen of a corpus run;
         * each opens a window for the next PER_AT qualifying passes. */
        const char *at_env = getenv("VPHONE_AGX_FRAME_DUMP_AT");
        if (at_env) {
            const char *per_env = getenv("VPHONE_AGX_FRAME_DUMP_PER_AT");
            unsigned per_at = per_env ? strtoul(per_env, NULL, 0) : 2;
            double now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) / 1e9;
            unsigned window = 0, index = 0;
            for (const char *p = at_env; *p; index++) {
                char *end;
                double at = strtod(p, &end);
                if (end == p) {
                    break;
                }
                if (at <= now) {
                    window = index + 1;
                }
                p = *end == ',' ? end + 1 : end;
            }
            if (!window) {
                return;
            }
            if (window != s->agx_frame_dump_window) {
                s->agx_frame_dump_window = window;
                s->agx_frame_dump_in_window = 0;
            }
            if (s->agx_frame_dump_in_window >= per_at) {
                return;
            }
            s->agx_frame_dump_in_window++;
        }
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
                     "  \"vclock_ns\": %lld, \"dump_window\": %u,\n"
                     "  \"type1_command\": \"%s\",\n  \"ta_command\": \"%s\"\n}\n",
                     context, (unsigned)ldl_le_p(header + 12),
                     (unsigned long long)cat, (unsigned long long)target,
                     queue, s->agx_last_ta_queue[context],
                     (unsigned long long)s->disp_mmu_table, (unsigned long long)pages,
                     capped ? "true" : "false", ta_valid ? "true" : "false",
                     (long long)qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL),
                     s->agx_frame_dump_window,
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

    /* Perf stats */
    int64_t doorbell_time_us;
    int64_t enqueue_time_us;
    int64_t render_start_us;
    int64_t render_end_us;
    int64_t done_enqueue_us;

    struct AGXRenderJob *next;
} AGXRenderJob;

static AGXRenderJob *agx_job_pool = NULL;

static inline AGXRenderJob *agx_job_alloc(void)
{
    AGXRenderJob *job;
    if (agx_job_pool) {
        job = agx_job_pool;
        agx_job_pool = job->next;
        memset(job, 0, sizeof(*job));
    } else {
        job = g_new0(AGXRenderJob, 1);
    }
    return job;
}

static inline void agx_job_free(AGXRenderJob *job)
{
    if (!job) {
        return;
    }
    job->next = agx_job_pool;
    agx_job_pool = job;
}

/* AGX_REPLAY_CORE_BEGIN: from here to AGX_REPLAY_CORE_END is also compiled
 * into the offline host harness (backend/agx_replay_host.c), which defines
 * AGX_REPLAY_SKIP to count the draws that replay leaves out. */
static inline bool agx_replay_audit_enabled(void)
{
    static int cached = -1;
    if (__builtin_expect(cached < 0, 0)) {
        cached = getenv("VPHONE_AGX_REPLAY_AUDIT") != NULL;
    }
    return cached;
}

#ifndef AGX_REPLAY_SKIP
#define AGX_REPLAY_SKIP(reason) do { \
    if (agx_replay_audit_enabled()) { \
        qemu_log_mask(LOG_GUEST_ERROR, "AGX replay skip: %s\n", reason); \
    } \
} while (0)
#endif

#define AGX_PDS_HEAP UINT64_C(0x600000000)

/* Immutable TA-consumed metadata. Texture pixels and render targets remain
 * live: native TA consumes geometry/state, while 3D samples the textures. */
struct AGXReplaySnapshot {
    gint refs;
    GHashTable *pages;
    struct AGXReplaySnapshot *next;
};
static __thread AGXReplaySnapshot *agx_read_snapshot;
static __thread bool agx_capture_snapshot;

typedef struct AGXPageNode {
    struct AGXPageNode *next;
} AGXPageNode;

static AGXPageNode *agx_page_pool = NULL;

static inline void *agx_page_alloc(void)
{
    if (agx_page_pool) {
        AGXPageNode *node = agx_page_pool;
        agx_page_pool = node->next;
        memset(node, 0, 4096);
        return node;
    }
    return g_malloc0(4096);
}

static inline void agx_page_free(void *ptr)
{
    if (!ptr) {
        return;
    }
    AGXPageNode *node = ptr;
    node->next = agx_page_pool;
    agx_page_pool = node;
}

typedef struct AGXKeyNode {
    struct AGXKeyNode *next;
} AGXKeyNode;

static AGXKeyNode *agx_key_pool = NULL;

static inline uint64_t *agx_key_alloc(void)
{
    if (agx_key_pool) {
        AGXKeyNode *node = agx_key_pool;
        agx_key_pool = node->next;
        return (uint64_t *)node;
    }
    return g_new(uint64_t, 1);
}

static inline void agx_key_free(void *ptr)
{
    if (!ptr) {
        return;
    }
    AGXKeyNode *node = ptr;
    node->next = agx_key_pool;
    agx_key_pool = node;
}

static AGXReplaySnapshot *agx_snapshot_pool = NULL;

static inline AGXReplaySnapshot *agx_snapshot_alloc(void)
{
    if (agx_snapshot_pool) {
        AGXReplaySnapshot *s = agx_snapshot_pool;
        agx_snapshot_pool = s->next;
        s->next = NULL;
        s->refs = 1;
        return s;
    }
    AGXReplaySnapshot *s = g_new0(AGXReplaySnapshot, 1);
    s->refs = 1;
    s->pages = g_hash_table_new_full(g_int64_hash, g_int64_equal, agx_key_free, agx_page_free);
    return s;
}

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
        g_hash_table_remove_all(snapshot->pages);
        snapshot->next = agx_snapshot_pool;
        agx_snapshot_pool = snapshot;
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
                cached = agx_page_alloc();
                if (address_space_read(&address_space_memory, page_pa, MEMTXATTRS_UNSPECIFIED,
                                       cached, 4096) == MEMTX_OK) {
                    uint64_t *key = agx_key_alloc();
                    *key = base;
                    g_hash_table_insert(agx_read_snapshot->pages, key, cached);
                } else {
                    agx_page_free(cached);
                    cached = NULL;
                }
            }
        }
        if (cached) {
            memcpy(out, cached + (gva & 0xfff), chunk);
        } else {
#ifdef QEMU_OSDEP_H
            AGXTLBEntry *tlb = agx_tlb_lookup(s, cat, base);
            if (tlb) {
                memcpy(out, tlb->host_ptr + (gva & 0xfff), chunk);
            } else
#endif
            if (gva >= (UINT64_C(1) << 40) || !agx_gpu_translate_ctx(s, cat, gva, &pa) ||
                address_space_read(&address_space_memory, pa, MEMTXATTRS_UNSPECIFIED,
                                   out, chunk) != MEMTX_OK) {
                return false;
            }
        }
        out += chunk;
        gva += chunk;
        n -= chunk;
    }
    return true;
}

/* GPU-rendered surface layout. Display surfaces are linear 2560-byte rows in
 * physical memory, mapped as 16-row strips (40 KiB) at 64 KiB-aligned GVAs,
 * which leaves an unmapped hole at base + 0xa000. Other targets use the
 * PBE layout: an explicit linear pitch or Y-first Morton order (layout 1).
 * The helper below initializes the display/linear fallback; replay supplies
 * the PBE-derived layout and pass dimensions for target-row access. */
typedef struct AGXSurface {
    uint64_t base;
    uint32_t pitch;
    bool strip;
    bool twiddled;
    uint32_t w, h;
    uint32_t *pixels;            /* NC target staging; NULL for established paths */
    uint8_t *loaded, *dirty;
} AGXSurface;

static void agx_surface_layout(A7State *s, uint64_t cat, uint64_t base, uint32_t w,
                               AGXSurface *out)
{
    hwaddr pa;
    out->base = base;
    out->twiddled = false;
    out->w = w;
    out->h = 0;
    out->pixels = NULL;
    out->loaded = out->dirty = NULL;
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
    uint32_t bpp;               /* bytes per pixel: 1 (A8 / L8) or 4 (BGRA) */
    bool strip;                 /* GPU-rendered surface: 16-row strips per 64 KiB */
    bool twiddled;              /* layout-1 texture/target, Y-first Morton order */
    bool layout1;               /* descriptor layout, before software-target overrides */
    bool linear_filter;         /* sampler word 1 bits [5:4] or [7:6] == 1 */
    bool nc_sparse;             /* proved sparse NC allocation, not general layout 1 */
    bool clamp_to_edge;         /* sampler word 1 bits [11:9] or [14:12] == 2 */
    uint32_t sampler_words[4];
} AGXTexture;

static bool agx_nc_sparse_layout(A7State *s, uint64_t cat, uint64_t base,
                                  uint32_t w, uint32_t h);

enum { AGX_TEX_NONE, AGX_TEX_FOUND, AGX_TEX_VIBRANCY };

static bool agx_replay_decode_tsw(A7State *s, uint64_t cat, const uint32_t *tsw,
                                  AGXTexture *tex)
{
    uint64_t q = tsw[2] | ((uint64_t)tsw[3] << 32);
    hwaddr pa;
    uint32_t fmt = tsw[0] >> 28;

    /* Captured WebKit U8 coverage atlas, with alpha-replicating swizzle.
     * Keep format-0 recognition narrow: arbitrary zero words are not TSWs. */
    bool coverage = fmt == 0 && (tsw[0] & ~7u) == 0x16d00;
    if (fmt != 6 && fmt != 3 && !coverage) {
        return false;
    }
    tex->bpp = (fmt == 3 || coverage) ? 1 : 4;
    tex->layout1 = (tsw[0] & 7) == 1;
    tex->w = ((tsw[1] >> 2) & 0x3fff) + 1;
    tex->h = (tsw[1] >> 16) + 1;
    tex->addr = ((q >> 16) & UINT64_C(0x3ffffffff)) << 2;
    tex->linear_filter = false;
    tex->clamp_to_edge = true;
    memset(tex->sampler_words, 0, sizeof(tex->sampler_words));
    /* Layout 1 or display surfaces with hole at +0xa000 (agx_surface_layout);
     * otherwise uploaded linear textures. */
    AGXSurface sf;
    agx_surface_layout(s, cat, tex->addr, tex->w, &sf);
    tex->strip = sf.strip;
    tex->nc_sparse = !sf.strip && (tsw[0] & 0x8007) == 1 &&
                     agx_nc_sparse_layout(s, cat, tex->addr, tex->w, tex->h);
    tex->twiddled = !sf.strip && ((tsw[0] & 0x8007) == 0x8001 || (tex->bpp == 1 && (tsw[0] & 7) == 1) ||
                                  ((tsw[0] >> 24) & 0xf) == 8 || tex->nc_sparse);
    if (sf.strip) {
        tex->stride = sf.pitch;
    } else if ((tsw[0] & 7) == 1) {
        tex->stride = tex->bpp == 1 ? ((tex->w + 15) & ~15u) : ((tex->w + 15) & ~15u) * 4;
    } else {
        /* Linear textures carry (pitch in pixels - 1) in word 2 bits [15:0]. */
        if (tex->bpp == 1) {
            tex->stride = (tsw[2] & 0xffff) + 1;
            if (tex->stride < tex->w) {
                tex->stride = (tex->w + 15) & ~15u;
            }
        } else {
            tex->stride = ((tsw[2] & 0xffff) + 1) * 4;
            if (tex->stride < tex->w * 4) {
                tex->stride = ((tex->w + 15) & ~15u) * 4;
            }
        }
    }
    return tex->addr && agx_gpu_translate_ctx(s, cat, tex->addr, &pa);
}

#define AGX_USC_COLOR_MATRIX UINT64_C(0x2353a3fe1ca2b71b)
#define AGX_USC_ROUNDED_CAPS UINT64_C(0x6c01d033fd937c7c)
#define AGX_USC_RADIAL_CLIP UINT64_C(0xe344a0f081cc4997)
#define AGX_USC_RADIAL_RESTORE UINT64_C(0xab6fdb6c250c32c0)
#define AGX_USC_STRIDE36_DUAL_TEX UINT64_C(0xb61824c33264c501)
#define AGX_USC_WEBKIT_GLYPHS UINT64_C(0x3064a456731fc3e3)

/* PPP words that point into the PDS heap reference fragment PDS data: u64
 * pointers to blocks holding sampler state, texture state words (TSW) and
 * shader constants. Draws whose constants are the Rec.709 luma weights use
 * CA's darkenSourceOver / lightenSourceOver legibility shaders (agy report
 * C8): the source is attenuated by the destination luminance before src-over. */
static int agx_replay_texture(A7State *s, uint64_t cat, const uint32_t *ppp,
                              unsigned n, uint64_t prog, AGXTexture *tex)
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

                const uint32_t *sw = NULL;
                if (k >= 4) {
                    sw = blocks[b] + k - 4;
                } else if ((blocks[b][4] & 0xfff) == 0xfff || ((blocks[b][5] >> 9) & 7) == 2) {
                    sw = blocks[b] + 4;
                } else {
                    for (unsigned sb = 0; sb < count; sb++) {
                        if ((blocks[sb][0] & 0xfff) == 0xfff &&
                            (((blocks[sb][1] >> 9) & 7) == 2 || (blocks[sb][1] & 0x2000) != 0)) {
                            sw = blocks[sb];
                            break;
                        }
                    }
                }
                if (sw) {
                    memcpy(tex->sampler_words, sw, sizeof(tex->sampler_words));
                    tex->linear_filter = ((sw[1] >> 4) & 3) == 1 || ((sw[1] >> 6) & 3) == 1;
                    tex->clamp_to_edge = ((sw[1] >> 9) & 7) == 2 || ((sw[1] >> 12) & 7) == 2 ||
                                         ((sw[1] >> 16) & 0xff) == 2 || ((sw[1] >> 24) & 0xff) == 2;
                }
                /* A stand-alone TSW is only 16 bytes. Constants following
                 * it can belong to the next draw; their luma weights must
                 * not turn an opaque wallpaper/icon draw into vibrancy. */
                return (!memcmp(blocks[b], luma, sizeof(luma)) ||
                        prog == AGX_USC_STRIDE36_DUAL_TEX) ?
                       AGX_TEX_VIBRANCY : AGX_TEX_FOUND;
            }
        }
        if (prog == AGX_USC_ROUNDED_CAPS && agx_replay_decode_tsw(s, cat, blocks[b] + 8, tex)) {

            const uint32_t *sw = blocks[b] + 4;
            memcpy(tex->sampler_words, sw, sizeof(tex->sampler_words));
            tex->linear_filter = ((sw[1] >> 4) & 3) == 1 || ((sw[1] >> 6) & 3) == 1;
            tex->clamp_to_edge = ((sw[1] >> 9) & 7) == 2 || ((sw[1] >> 12) & 7) == 2 ||
                                 ((sw[1] >> 16) & 0xff) == 2 || ((sw[1] >> 24) & 0xff) == 2;
            return AGX_TEX_FOUND;
        }
    }
    return AGX_TEX_NONE;
}

static bool agx_replay_decode_mask_tsw(A7State *s, uint64_t cat, const uint32_t *ppp,
                                      unsigned n, AGXTexture *mask_tex)
{
    for (unsigned i = 0; i < n; i++) {
        uint32_t data[8];
        if ((ppp[i] & 0xc0000000) != 0x80000000 ||
            !agx_ctx_read(s, cat, AGX_PDS_HEAP | ppp[i], data, sizeof(data))) {
            continue;
        }
        for (unsigned k = 0; k < 8; k += 2) {
            uint64_t ptr = data[k] | ((uint64_t)data[k + 1] << 32);
            if (ptr < 0x1000 || ptr >= (UINT64_C(1) << 40)) {
                break;
            }
            uint32_t block[12];
            if (agx_ctx_read(s, cat, ptr, block, sizeof(block))) {
                if (agx_replay_decode_tsw(s, cat, block + 4, mask_tex)) {
                    const uint32_t *sw = NULL;
                    if ((block[0] & 0xfff) == 0xfff || ((block[1] >> 9) & 7) == 2) {
                        sw = block;
                    } else if ((block[8] & 0xfff) == 0xfff || ((block[9] >> 9) & 7) == 2) {
                        sw = block + 8;
                    }
                    if (sw) {
                        memcpy(mask_tex->sampler_words, sw, sizeof(mask_tex->sampler_words));
                        mask_tex->linear_filter = ((sw[1] >> 4) & 3) == 1 || ((sw[1] >> 6) & 3) == 1;
                        mask_tex->clamp_to_edge = ((sw[1] >> 9) & 7) == 2 || ((sw[1] >> 12) & 7) == 2 ||
                                                 ((sw[1] >> 16) & 0xff) == 2 || ((sw[1] >> 24) & 0xff) == 2;
                    }
                    return true;
                }
            }
        }
    }
    return false;
}

static uint64_t agx_texel_gva(const AGXTexture *tex, uint32_t x, uint32_t y)
{
    unsigned bpp = tex->bpp ? tex->bpp : 4;
    if (tex->twiddled) {
        uint64_t offset = 0;
        unsigned bit = 0;
        /* Interleave through the smaller padded dimension, then append
         * the remaining coordinate bits for rectangular textures. */
        for (unsigned mask = 1; mask < tex->w || mask < tex->h; mask <<= 1) {
            if (mask < tex->h) { offset |= (uint64_t)!!(y & mask) << bit++; }
            if (mask < tex->w) { offset |= (uint64_t)!!(x & mask) << bit++; }
        }
        return tex->addr + offset * bpp;
    }
    if (tex->strip) {
        return tex->addr + (uint64_t)(y >> 4) * 0x10000 + (y & 15) * tex->stride + x * bpp;
    }
    return tex->addr + (uint64_t)y * tex->stride + x * bpp;
}

static __thread uint32_t agx_morton_x_lut[4096];
static __thread uint32_t agx_morton_cached_w = 0, agx_morton_cached_h = 0;

static void agx_update_morton_lut(uint32_t w, uint32_t h)
{
    if (agx_morton_cached_w == w && agx_morton_cached_h == h) {
        return;
    }
    for (uint32_t x = 0; x < w && x < 4096; x++) {
        uint32_t offset = 0;
        unsigned bit = 0;
        for (unsigned mask = 1; mask < w || mask < h; mask <<= 1) {
            if (mask < h) { bit++; }
            if (mask < w) {
                if (x & mask) { offset |= 1u << bit; }
                bit++;
            }
        }
        agx_morton_x_lut[x] = offset;
    }
    agx_morton_cached_w = w;
    agx_morton_cached_h = h;
}

/* A Morton row is scattered within 4 KiB tiles. Cache one tile while
 * gathering it, instead of walking the GPU tables for every pixel. */
static bool agx_texture_row(A7State *s, uint64_t cat, const AGXTexture *tex,
                            unsigned y, uint32_t *out)
{
    unsigned bpp = tex->bpp ? tex->bpp : 4;
    if (!tex->twiddled) {
        if (bpp == 1) {
            uint8_t row_bytes[4096];
            if (tex->w > sizeof(row_bytes) ||
                !agx_ctx_read(s, cat, agx_texel_gva(tex, 0, y), row_bytes, tex->w)) {
                return false;
            }
            for (unsigned x = 0; x < tex->w; x++) {
                out[x] = (uint32_t)row_bytes[x] << 24;
            }
            return true;
        }
        return agx_ctx_read(s, cat, agx_texel_gva(tex, 0, y), out, tex->w * 4);
    }
    if (tex->w <= 4096) {
        agx_update_morton_lut(tex->w, tex->h);
        uint64_t y_offset = 0;
        unsigned bit = 0;
        for (unsigned mask = 1; mask < tex->w || mask < tex->h; mask <<= 1) {
            if (mask < tex->h) {
                if (y & mask) { y_offset |= (uint64_t)1 << bit; }
                bit++;
            }
            if (mask < tex->w) { bit++; }
        }
        uint8_t page[4096];
        uint64_t cached = UINT64_MAX;
        uint64_t base_addr = tex->addr;
        if (bpp == 1) {
            for (unsigned x = 0; x < tex->w; x++) {
                uint64_t gva = base_addr + (y_offset | (uint64_t)agx_morton_x_lut[x]);
                uint64_t base = gva & ~UINT64_C(0xfff);
                if (base != cached) {
                    if (!agx_ctx_read(s, cat, base, page, sizeof(page))) { return false; }
                    cached = base;
                }
                out[x] = (uint32_t)page[gva & 0xfff] << 24;
            }
        } else {
            for (unsigned x = 0; x < tex->w; x++) {
                uint64_t gva = base_addr + ((y_offset | (uint64_t)agx_morton_x_lut[x]) << 2);
                uint64_t base = gva & ~UINT64_C(0xfff);
                if (base != cached) {
                    if (!agx_ctx_read(s, cat, base, page, sizeof(page))) { return false; }
                    cached = base;
                }
                out[x] = ldl_le_p(page + (gva & 0xfff));
            }
        }
        return true;
    }
    uint8_t page[4096];
    uint64_t cached = UINT64_MAX;
    for (unsigned x = 0; x < tex->w; x++) {
        uint64_t gva = agx_texel_gva(tex, x, y), base = gva & ~UINT64_C(0xfff);
        if (base != cached) {
            if (!agx_ctx_read(s, cat, base, page, sizeof(page))) { return false; }
            cached = base;
        }
        if (bpp == 1) {
            out[x] = (uint32_t)page[gva & 0xfff] << 24;
        } else {
            out[x] = ldl_le_p(page + (gva & 0xfff));
        }
    }
    return true;
}

/* NC and folder backdrops allocate these scratch extents sparsely. Require
 * BOTH absent linear row pages and a completely mapped 32x32 Morton-tile footprint. Dense
 * glyph caches and every other extent retain the established addressing. */
static __thread struct {
    uint64_t cat, base;
    uint32_t w, h;
    bool sparse;
} agx_nc_layout_cache[8];
static __thread unsigned agx_nc_layout_count;
static __thread bool agx_nc_layout_active;

static bool agx_nc_sparse_layout(A7State *s, uint64_t cat, uint64_t base,
                                  uint32_t w, uint32_t h)
{
    if ((base & 4095) || !((w == 128 && h == 96) ||
        (w == 640 && (h == 224 || h == 288 || h == 320 || h == 640)))) { return false; }
    if (agx_nc_layout_active) {
        for (unsigned i = 0; i < agx_nc_layout_count; i++) {
            if (agx_nc_layout_cache[i].cat == cat && agx_nc_layout_cache[i].base == base &&
                agx_nc_layout_cache[i].w == w && agx_nc_layout_cache[i].h == h) {
                return agx_nc_layout_cache[i].sparse;
            }
        }
    }
    hwaddr pa;
    bool missing_linear = false, complete_morton = true;
    uint32_t pitch = ((w + 15) & ~15u) * 4;
    for (unsigned y = 0; y < h; y++) {
        if (!agx_gpu_translate_ctx(s, cat, base + (uint64_t)y * pitch, &pa)) {
            missing_linear = true;
            break;
        }
    }
    if (missing_linear) {
        AGXTexture tex = {.addr = base, .w = w, .h = h, .twiddled = true};
        for (unsigned y = 0; y < h && complete_morton; y += 32) {
            for (unsigned x = 0; x < w; x += 32) {
                if (!agx_gpu_translate_ctx(s, cat, agx_texel_gva(&tex, x, y), &pa)) {
                    complete_morton = false;
                    break;
                }
            }
        }
    }
    bool sparse = missing_linear && complete_morton;
    if (agx_nc_layout_active && agx_nc_layout_count < ARRAY_SIZE(agx_nc_layout_cache)) {
        unsigned i = agx_nc_layout_count++;
        agx_nc_layout_cache[i].cat = cat; agx_nc_layout_cache[i].base = base;
        agx_nc_layout_cache[i].w = w; agx_nc_layout_cache[i].h = h;
        agx_nc_layout_cache[i].sparse = sparse;
    }
    return sparse;
}


/* Layout 1 is Morton ordered for GPU render targets too. Bit 15 is not
 * the layout selector: render-target descriptors omit it. A row read/write
 * must gather/scatter pixels rather than walking through unmapped padding. */
static bool agx_nc_transfer_region(A7State *s, uint64_t cat, uint64_t base,
                                    uint32_t w, uint32_t h, uint32_t *pixels, bool write,
                                    unsigned x0, unsigned y0, unsigned x1, unsigned y1)
{
    AGXTexture tex = {.addr = base, .w = w, .h = h, .twiddled = true};
    uint32_t page[1024];
    static __thread uint16_t tile_offsets[1024];
    static __thread bool ready;
    if (!ready) {
        for (unsigned y = 0; y < 32; y++) {
            for (unsigned x = 0; x < 32; x++) {
                unsigned off = 0;
                for (unsigned b = 0; b < 5; b++) {
                    off |= ((y >> b) & 1) << (b * 2);
                    off |= ((x >> b) & 1) << (b * 2 + 1);
                }
                tile_offsets[y * 32 + x] = off;
            }
        }
        ready = true;
    }
    for (unsigned ty = y0 & ~31u; ty < y1; ty += 32) {
        for (unsigned tx = x0 & ~31u; tx < x1; tx += 32) {
            uint64_t gva = agx_texel_gva(&tex, tx, ty);
            if (!write && !agx_ctx_read(s, cat, gva, page, sizeof(page))) { return false; }
            for (unsigned y = 0; y < 32; y++) {
                for (unsigned x = 0; x < 32; x++) {
                    unsigned off = tile_offsets[y * 32 + x];
                    uint32_t *pixel = pixels + (size_t)(ty + y) * w + tx + x;
                    if (write) { page[off] = *pixel; }
                    else { *pixel = page[off]; }
                }
            }
            if (write && !agx_ctx_write(s, cat, gva, page, sizeof(page))) { return false; }
        }
    }
    return true;
}

static bool agx_nc_transfer(A7State *s, uint64_t cat, uint64_t base,
                             uint32_t w, uint32_t h, uint32_t *pixels, bool write)
{
    return agx_nc_transfer_region(s, cat, base, w, h, pixels, write, 0, 0, w, h);
}

/* Load each touched destination tile once and commit only dirty tiles. */
static bool agx_nc_target_span(A7State *s, uint64_t cat, const AGXSurface *sf,
                                unsigned x, unsigned y, uint32_t *pixels,
                                unsigned count, bool write)
{
    unsigned columns = sf->w / 32;
    for (unsigned tx = x & ~31u; tx < x + count; tx += 32) {
        unsigned index = (y / 32) * columns + tx / 32;
        if (!sf->loaded[index]) {
            unsigned ty = y & ~31u;
            if (!agx_nc_transfer_region(s, cat, sf->base, sf->w, sf->h, sf->pixels,
                                        false, tx, ty, tx + 32, ty + 32)) { return false; }
            sf->loaded[index] = 1;
        }
        if (write) { sf->dirty[index] = 1; }
    }
    uint32_t *row = sf->pixels + (size_t)y * sf->w + x;
    if (write) { memcpy(row, pixels, count * 4); }
    else { memcpy(pixels, row, count * 4); }
    return true;
}

static bool agx_target_row(A7State *s, uint64_t cat, const AGXSurface *sf,
                            unsigned x, unsigned y, uint32_t *pixels,
                            unsigned count, bool write)
{
    if (sf->pixels) { return agx_nc_target_span(s, cat, sf, x, y, pixels, count, write); }
    if (!sf->twiddled) {
        uint64_t gva = agx_surface_gva(sf, x, y);
        return write ? agx_ctx_write(s, cat, gva, pixels, count * 4)
                     : agx_ctx_read(s, cat, gva, pixels, count * 4);
    }
    AGXTexture tex = { .addr = sf->base, .w = sf->w, .h = sf->h,
                       .twiddled = true };
    /* A row touches each tile once. Preserve the other rows in its pages. */
    agx_update_morton_lut(sf->w, sf->h);
    uint64_t y_offset = agx_texel_gva(&tex, 0, y) - sf->base;
    uint8_t page[4096];
    uint64_t cached = UINT64_MAX;
    bool ok = true;
    for (unsigned i = 0; i < count; i++) {
        uint64_t gva = sf->base + (y_offset | ((uint64_t)agx_morton_x_lut[x + i] << 2));
        uint64_t base = gva & ~UINT64_C(0xfff);
        if (base != cached) {
            if (write && cached != UINT64_MAX) {
                ok &= agx_ctx_write(s, cat, cached, page, sizeof(page));
            }
            if (!agx_ctx_read(s, cat, base, page, sizeof(page))) { return false; }
            cached = base;
        }
        if (write) { memcpy(page + (gva & 0xfff), pixels + i, 4); }
        else { pixels[i] = ldl_le_p(page + (gva & 0xfff)); }
    }
    if (write && cached != UINT64_MAX) {
        ok &= agx_ctx_write(s, cat, cached, page, sizeof(page));
    }
    return ok;
}

static inline __attribute__((always_inline)) bool agx_target_read(A7State *s, uint64_t cat, const AGXSurface *sf,
                             unsigned x, unsigned y, uint32_t *pixels, unsigned count)
{
    if (sf->pixels) { return agx_nc_target_span(s, cat, sf, x, y, pixels, count, false); }
    if (!sf->twiddled) { return agx_ctx_read(s, cat, agx_surface_gva(sf, x, y), pixels, count * 4); }
    return agx_target_row(s, cat, sf, x, y, pixels, count, false);
}

static inline __attribute__((always_inline)) bool agx_target_write(A7State *s, uint64_t cat, const AGXSurface *sf,
                              unsigned x, unsigned y, uint32_t *pixels, unsigned count)
{
    if (sf->pixels) { return agx_nc_target_span(s, cat, sf, x, y, pixels, count, true); }
    if (!sf->twiddled) { return agx_ctx_write(s, cat, agx_surface_gva(sf, x, y), pixels, count * 4); }
    return agx_target_row(s, cat, sf, x, y, pixels, count, true);
}


static inline __attribute__((always_inline)) uint32_t agx_div255(uint32_t v)
{
    return ((v + 1) * 257) >> 16;
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
    uint32_t y2 = agx_div255(y1 * y1), f = agx_div255(y2 * y2);
    uint32_t out = 0;

    sb = agx_div255(sb * f); sg = agx_div255(sg * f); sr = agx_div255(sr * f); sa = agx_div255(sa * f);
    const uint32_t s4[4] = { sb, sg, sr, sa }, d4[4] = { db, dg, dr, dst >> 24 };
    for (unsigned c = 0; c < 4; c++) {
        out |= MIN(s4[c] + agx_div255(d4[c] * (255 - sa)), 255u) << (c * 8);
    }
    return out;
}

/* Premultiplied BGRA src-over, alpha included (offscreen layers keep it). */
static inline __attribute__((always_inline)) uint32_t agx_blend_over(uint32_t dst, uint32_t src)
{
    uint32_t a = src >> 24;
    if (a == 255) {
        return src;
    }
    if (src == 0) {
        return dst;
    }
    uint32_t inv_a = 255 - a;
    uint32_t b = (src & 0xff) + agx_div255((dst & 0xff) * inv_a);
    uint32_t g = ((src >> 8) & 0xff) + agx_div255(((dst >> 8) & 0xff) * inv_a);
    uint32_t r = ((src >> 16) & 0xff) + agx_div255(((dst >> 16) & 0xff) * inv_a);
    uint32_t oa = a + agx_div255((dst >> 24) * inv_a);
    return (MIN(b, 255u)) |
           (MIN(g, 255u) << 8) |
           (MIN(r, 255u) << 16) |
           (MIN(oa, 255u) << 24);
}

/* CA fragment shaders recognised by an FNV-1a hash of the first 64 bytes of
 * their USC code (PPP word 7 -> state at 0x6_0000_0000 -> USC offset in the
 * 0x7_0000_0000 heap). The compiled code is identical across boots of this
 * iOS build. */
#define AGX_USC_DEST_IN UINT64_C(0x4ee01acc47551344)  /* mask: D *= texel alpha */
/* CA padding clear: observed eight zero-colour quads around the slider mask.
 * Its USC program replaces D with S; transparent source-over is a no-op. */
#define AGX_USC_COPY UINT64_C(0x99d4fcf52a8c2ef3)
#define AGX_USC_SPANS UINT64_C(0x1a20cc29e8fcc15b)
#define AGX_USC_WEBKIT_SPANS UINT64_C(0x84102d511f2a41bb)
#define AGX_USC_WEBKIT_STRIPS UINT64_C(0x4eb8837dc2079f95)
#define AGX_USC_PLUS_L UINT64_C(0x5b133ef7e0c3428c)
#define AGX_USC_PLUS_L2 UINT64_C(0xa225a18725e48d5c)
#define AGX_USC_SOLID_PLUS_L UINT64_C(0xf137b90c25ab5471)
#define AGX_USC_PILL_BORDER UINT64_C(0x62a99af8d8beb332)
#define AGX_USC_CORNER_RESTORE UINT64_C(0x03a2edd2a4d84437)

/* Additive blend (CoreAnimation plusL / plusLighter): clamp(S + D, 0, 255). */
static inline uint32_t agx_blend_plus_lighter(uint32_t dst, uint32_t src)
{
    uint32_t b = (src & 0xff) + (dst & 0xff);
    uint32_t g = ((src >> 8) & 0xff) + ((dst >> 8) & 0xff);
    uint32_t r = ((src >> 16) & 0xff) + ((dst >> 16) & 0xff);
    uint32_t a = (src >> 24) + (dst >> 24);
    return MIN(b, 255u) | (MIN(g, 255u) << 8) | (MIN(r, 255u) << 16) | (MIN(a, 255u) << 24);
}

__attribute__((unused))
static uint32_t agx_blend_plus_darker(uint32_t dst, uint32_t src)
{
    uint32_t out = 0;
    for (unsigned c = 0; c < 32; c += 8) {
        int v = (int)((src >> c) & 0xff) + (int)((dst >> c) & 0xff) - 255;
        out |= (uint32_t)MAX(v, 0) << c;
    }
    return out;
}

/* CoreAnimation narrow Gaussian blur shaders.
 * Recovered from CA::OGL::BlurState::narrow_blur / narrowBlurParameters in QuartzCore. */
#define AGX_USC_BLUR_4TAP UINT64_C(0x0cda5e479d40815f)
#define AGX_USC_BLUR_7TAP UINT64_C(0xf82f9a96aff1e034)
#define AGX_USC_DOWNSAMPLE_8 UINT64_C(0x00e3b3d44520e23f)

/* Historical blur signature: also the uniform-colour export shader used
 * by stride-12 region clears. Only the 4/7-pair hashes identify convolution. */
static const uint8_t agx_blur_preamble[41] = {
    0x2a, 0x0a, 0x94, 0x99, 0x99, 0xaf, 0x1b, 0x80,
    0x00, 0x33, 0x00, 0x00, 0x80, 0x40, 0x80, 0x01,
    0x88, 0x00, 0x20, 0xff, 0x3a, 0x8a, 0x80, 0x94,
    0x99, 0x99, 0xaf, 0x1b, 0x80, 0x00, 0x33, 0x00,
    0x00, 0x82, 0x40, 0x80, 0x03, 0x88, 0x00, 0x21,
    0x00
};

static inline bool agx_is_blur_code(const uint8_t *code, size_t len)
{
    return len >= sizeof(agx_blur_preamble) &&
           memcmp(code, agx_blur_preamble, sizeof(agx_blur_preamble)) == 0;
}

static inline bool agx_is_blur_program(uint64_t prog)
{
    return prog == AGX_USC_BLUR_4TAP || prog == AGX_USC_BLUR_7TAP;
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

static const struct {
    float offset;
    float weight;
} agx_blur_taps_4[] = {
    { 0.638336f, 0.275844f },
    { 2.348645f, 0.185744f },
    { 4.245085f, 0.035760f },
    { 6.164516f, 0.002652f },
};

static const struct {
    float offset;
    float weight;
} agx_blur_taps_7[] = {
    { 0.662207f, 0.118916f },
    { 2.475021f, 0.141265f },
    { 4.455121f, 0.107065f },
    { 6.435364f, 0.069256f },
    { 8.415810f, 0.038236f },
    { 10.396517f, 0.018016f },
    { 12.377541f, 0.007245f },
};

static inline uint32_t agx_bilinear_interpolate(uint32_t c00, uint32_t c10,
                                                uint32_t c01, uint32_t c11,
                                                float fx, float fy);

static inline __attribute__((always_inline)) uint32_t agx_bilinear_interpolate_wy(uint32_t c00, uint32_t c10,
                                                                                    uint32_t c01, uint32_t c11,
                                                                                    float fx, int wy)
{
    if (c00 == c10 && c00 == c01 && c00 == c11) {
        return c00;
    }
    int wx = (int)(fx * 256.0f + 0.5f);
    if ((wx | wy) == 0) {
        return c00;
    }
    if (wy == 0 || (c00 == c01 && c10 == c11)) {
        if (wx == 0) {
            return c00;
        }
        int w0 = 256 - wx, w1 = wx;
        uint32_t b = ((c00 & 0xff) * w0 + (c10 & 0xff) * w1 + 128) >> 8;
        uint32_t g = (((c00 >> 8) & 0xff) * w0 + ((c10 >> 8) & 0xff) * w1 + 128) >> 8;
        uint32_t r = (((c00 >> 16) & 0xff) * w0 + ((c10 >> 16) & 0xff) * w1 + 128) >> 8;
        uint32_t a = ((c00 >> 24) * w0 + (c10 >> 24) * w1 + 128) >> 8;
        return b | (g << 8) | (r << 16) | (a << 24);
    }
    int wy0 = 256 - wy;
    int w00 = (256 - wx) * wy0;
    int w10 = wx * wy0;
    int w01 = (256 - wx) * wy;
    int w11 = wx * wy;

    uint32_t b = ((c00 & 0xff) * w00 + (c10 & 0xff) * w10 +
                  (c01 & 0xff) * w01 + (c11 & 0xff) * w11 + 32768) >> 16;
    uint32_t g = (((c00 >> 8) & 0xff) * w00 + ((c10 >> 8) & 0xff) * w10 +
                  ((c01 >> 8) & 0xff) * w01 + ((c11 >> 8) & 0xff) * w11 + 32768) >> 16;
    uint32_t r = (((c00 >> 16) & 0xff) * w00 + ((c10 >> 16) & 0xff) * w10 +
                  ((c01 >> 16) & 0xff) * w01 + ((c11 >> 16) & 0xff) * w11 + 32768) >> 16;
    uint32_t a = ((c00 >> 24) * w00 + (c10 >> 24) * w10 +
                  (c01 >> 24) * w01 + (c11 >> 24) * w11 + 32768) >> 16;
    return b | (g << 8) | (r << 16) | (a << 24);
}

static inline uint32_t agx_bilinear_interpolate(uint32_t c00, uint32_t c10,
                                                uint32_t c01, uint32_t c11,
                                                float fx, float fy)
{
    return agx_bilinear_interpolate_wy(c00, c10, c01, c11, fx, (int)(fy * 256.0f + 0.5f));
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

static uint32_t agx_sample_downsample(const uint32_t *buf, uint32_t w, uint32_t h,
                                        float u, float v, const float offsets[4][2])
{
    unsigned acc[4] = {0};
    for (unsigned i = 0; i < 4; i++) {
        for (int sign = -1; sign <= 1; sign += 2) {
            uint32_t sample = agx_sample_bilinear(buf, w, h,
                u + sign * offsets[i][0] * w, v + sign * offsets[i][1] * h);
            for (unsigned c = 0; c < 4; c++) { acc[c] += (sample >> (c * 8)) & 255; }
        }
    }
    uint32_t src = 0;
    for (unsigned c = 0; c < 4; c++) { src |= ((acc[c] + 4) / 8) << (c * 8); }
    return src;
}

#if defined(__ARM_NEON) || defined(__ARM_NEON__)
#include <arm_neon.h>
#endif

typedef struct AGXBlurTapX {
    uint32_t x0_p, x1_p;
    uint32_t x0_n, x1_n;
    int w_p, w_n;
} AGXBlurTapX;

typedef struct AGXBlurTapY {
    size_t row0_p, row1_p;
    size_t row0_n, row1_n;
    int w_p, w_n;
} AGXBlurTapY;

static __thread AGXBlurTapX *agx_blur_taps_x = NULL;
static __thread size_t agx_blur_taps_x_cap = 0;

static inline __attribute__((always_inline)) uint32_t agx_sample_1d_fast(uint32_t c0, uint32_t c1, int w)
{
    if (w == 0 || c0 == c1) {
        return c0;
    }
    int w0 = 256 - w, w1 = w;
    uint32_t b = ((c0 & 0xff) * w0 + (c1 & 0xff) * w1 + 128) >> 8;
    uint32_t g = (((c0 >> 8) & 0xff) * w0 + ((c1 >> 8) & 0xff) * w1 + 128) >> 8;
    uint32_t r = (((c0 >> 16) & 0xff) * w0 + ((c1 >> 16) & 0xff) * w1 + 128) >> 8;
    uint32_t a = ((c0 >> 24) * w0 + (c1 >> 24) * w1 + 128) >> 8;
    return b | (g << 8) | (r << 16) | (a << 24);
}

static inline uint32_t agx_sample_1d(uint32_t c0, uint32_t c1, float frac)
{
    int w = (int)(frac * 256.0f + 0.5f);
    return agx_sample_1d_fast(c0, c1, w);
}

static void agx_blur_1d(uint32_t *buf_in, uint32_t *buf_tmp, uint32_t w, uint32_t h,
                        const float *offsets, const float *weights, unsigned n_taps,
                        bool is_vertical)
{
    if (!n_taps || !w || !h) return;
    if (is_vertical) {
        for (uint32_t y = 0; y < h; y++) {
            AGXBlurTapY taps[8];
            for (unsigned t = 0; t < n_taps; t++) {
                float off = offsets[t];
                float fv_p = (float)y + off;
                if (fv_p < 0.0f) fv_p = 0.0f;
                if (fv_p > (float)(h - 1)) fv_p = (float)(h - 1);
                uint32_t y0_p = (uint32_t)fv_p;
                uint32_t y1_p = y0_p + 1 < h ? y0_p + 1 : y0_p;
                taps[t].row0_p = (size_t)y0_p * w;
                taps[t].row1_p = (size_t)y1_p * w;
                taps[t].w_p = (int)((fv_p - (float)y0_p) * 256.0f + 0.5f);

                float fv_n = (float)y - off;
                if (fv_n < 0.0f) fv_n = 0.0f;
                if (fv_n > (float)(h - 1)) fv_n = (float)(h - 1);
                uint32_t y0_n = (uint32_t)fv_n;
                uint32_t y1_n = y0_n + 1 < h ? y0_n + 1 : y0_n;
                taps[t].row0_n = (size_t)y0_n * w;
                taps[t].row1_n = (size_t)y1_n * w;
                taps[t].w_n = (int)((fv_n - (float)y0_n) * 256.0f + 0.5f);
            }
            uint32_t *row_out = buf_tmp + (size_t)y * w;
            for (uint32_t x = 0; x < w; x++) {
#if defined(__ARM_NEON) || defined(__ARM_NEON__)
                float32x4_t acc = vdupq_n_f32(0.0f);
                for (unsigned t = 0; t < n_taps; t++) {
                    uint32_t s_pos = agx_sample_1d_fast(buf_in[taps[t].row0_p + x],
                                                        buf_in[taps[t].row1_p + x],
                                                        taps[t].w_p);
                    uint32_t s_neg = agx_sample_1d_fast(buf_in[taps[t].row0_n + x],
                                                        buf_in[taps[t].row1_n + x],
                                                        taps[t].w_n);
                    uint8x8_t p = vcreate_u8((uint64_t)s_pos | ((uint64_t)s_neg << 32));
                    uint16x8_t p16 = vmovl_u8(p);
                    uint16x4_t sum16 = vadd_u16(vget_low_u16(p16), vget_high_u16(p16));
                    float32x4_t sum_f = vcvtq_f32_u32(vmovl_u16(sum16));
                    acc = vmlaq_n_f32(acc, sum_f, weights[t]);
                }
                int32x4_t s32 = vcvtaq_s32_f32(acc);
                uint16x4_t u16 = vqmovun_s32(s32);
                uint8x8_t u8 = vqmovn_u16(vcombine_u16(u16, u16));
                row_out[x] = vget_lane_u32(vreinterpret_u32_u8(u8), 0);
#else
                float acc[4] = { 0.0f, 0.0f, 0.0f, 0.0f };
                for (unsigned t = 0; t < n_taps; t++) {
                    uint32_t s_pos = agx_sample_1d_fast(buf_in[taps[t].row0_p + x],
                                                        buf_in[taps[t].row1_p + x],
                                                        taps[t].w_p);
                    uint32_t s_neg = agx_sample_1d_fast(buf_in[taps[t].row0_n + x],
                                                        buf_in[taps[t].row1_n + x],
                                                        taps[t].w_n);
                    acc[0] += (float)((s_pos & 0xff) + (s_neg & 0xff)) * weights[t];
                    acc[1] += (float)(((s_pos >> 8) & 0xff) + ((s_neg >> 8) & 0xff)) * weights[t];
                    acc[2] += (float)(((s_pos >> 16) & 0xff) + ((s_neg >> 16) & 0xff)) * weights[t];
                    acc[3] += (float)((s_pos >> 24) + (s_neg >> 24)) * weights[t];
                }
                int iv0 = (int)lroundf(acc[0]);
                int iv1 = (int)lroundf(acc[1]);
                int iv2 = (int)lroundf(acc[2]);
                int iv3 = (int)lroundf(acc[3]);
                row_out[x] = (uint32_t)(iv0 < 0 ? 0 : (iv0 > 255 ? 255 : iv0)) |
                             ((uint32_t)(iv1 < 0 ? 0 : (iv1 > 255 ? 255 : iv1)) << 8) |
                             ((uint32_t)(iv2 < 0 ? 0 : (iv2 > 255 ? 255 : iv2)) << 16) |
                             ((uint32_t)(iv3 < 0 ? 0 : (iv3 > 255 ? 255 : iv3)) << 24);
#endif
            }
        }
    } else {
        if ((size_t)w * n_taps > agx_blur_taps_x_cap) {
            agx_blur_taps_x_cap = (size_t)w * n_taps + 128;
            agx_blur_taps_x = (AGXBlurTapX *)realloc(agx_blur_taps_x, agx_blur_taps_x_cap * sizeof(AGXBlurTapX));
        }
        for (uint32_t x = 0; x < w; x++) {
            AGXBlurTapX *entry_base = &agx_blur_taps_x[x * n_taps];
            for (unsigned t = 0; t < n_taps; t++) {
                float off = offsets[t];
                float fu_p = (float)x + off;
                if (fu_p < 0.0f) fu_p = 0.0f;
                if (fu_p > (float)(w - 1)) fu_p = (float)(w - 1);
                uint32_t x0_p = (uint32_t)fu_p;
                uint32_t x1_p = x0_p + 1 < w ? x0_p + 1 : x0_p;

                float fu_n = (float)x - off;
                if (fu_n < 0.0f) fu_n = 0.0f;
                if (fu_n > (float)(w - 1)) fu_n = (float)(w - 1);
                uint32_t x0_n = (uint32_t)fu_n;
                uint32_t x1_n = x0_n + 1 < w ? x0_n + 1 : x0_n;

                AGXBlurTapX *entry = &entry_base[t];
                entry->x0_p = x0_p;
                entry->x1_p = x1_p;
                entry->w_p = (int)((fu_p - (float)x0_p) * 256.0f + 0.5f);
                entry->x0_n = x0_n;
                entry->x1_n = x1_n;
                entry->w_n = (int)((fu_n - (float)x0_n) * 256.0f + 0.5f);
            }
        }
        for (uint32_t y = 0; y < h; y++) {
            const uint32_t *row_in = buf_in + (size_t)y * w;
            uint32_t *row_out = buf_tmp + (size_t)y * w;
            for (uint32_t x = 0; x < w; x++) {
                const AGXBlurTapX *entry = &agx_blur_taps_x[x * n_taps];
#if defined(__ARM_NEON) || defined(__ARM_NEON__)
                float32x4_t acc = vdupq_n_f32(0.0f);
                for (unsigned t = 0; t < n_taps; t++) {
                    uint32_t s_pos = agx_sample_1d_fast(row_in[entry[t].x0_p],
                                                        row_in[entry[t].x1_p],
                                                        entry[t].w_p);
                    uint32_t s_neg = agx_sample_1d_fast(row_in[entry[t].x0_n],
                                                        row_in[entry[t].x1_n],
                                                        entry[t].w_n);
                    uint8x8_t p = vcreate_u8((uint64_t)s_pos | ((uint64_t)s_neg << 32));
                    uint16x8_t p16 = vmovl_u8(p);
                    uint16x4_t sum16 = vadd_u16(vget_low_u16(p16), vget_high_u16(p16));
                    float32x4_t sum_f = vcvtq_f32_u32(vmovl_u16(sum16));
                    acc = vmlaq_n_f32(acc, sum_f, weights[t]);
                }
                int32x4_t s32 = vcvtaq_s32_f32(acc);
                uint16x4_t u16 = vqmovun_s32(s32);
                uint8x8_t u8 = vqmovn_u16(vcombine_u16(u16, u16));
                row_out[x] = vget_lane_u32(vreinterpret_u32_u8(u8), 0);
#else
                float acc[4] = { 0.0f, 0.0f, 0.0f, 0.0f };
                for (unsigned t = 0; t < n_taps; t++) {
                    uint32_t s_pos = agx_sample_1d_fast(row_in[entry[t].x0_p],
                                                        row_in[entry[t].x1_p],
                                                        entry[t].w_p);
                    uint32_t s_neg = agx_sample_1d_fast(row_in[entry[t].x0_n],
                                                        row_in[entry[t].x1_n],
                                                        entry[t].w_n);
                    acc[0] += (float)((s_pos & 0xff) + (s_neg & 0xff)) * weights[t];
                    acc[1] += (float)(((s_pos >> 8) & 0xff) + ((s_neg >> 8) & 0xff)) * weights[t];
                    acc[2] += (float)(((s_pos >> 16) & 0xff) + ((s_neg >> 16) & 0xff)) * weights[t];
                    acc[3] += (float)((s_pos >> 24) + (s_neg >> 24)) * weights[t];
                }
                int iv0 = (int)lroundf(acc[0]);
                int iv1 = (int)lroundf(acc[1]);
                int iv2 = (int)lroundf(acc[2]);
                int iv3 = (int)lroundf(acc[3]);
                row_out[x] = (uint32_t)(iv0 < 0 ? 0 : (iv0 > 255 ? 255 : iv0)) |
                             ((uint32_t)(iv1 < 0 ? 0 : (iv1 > 255 ? 255 : iv1)) << 8) |
                             ((uint32_t)(iv2 < 0 ? 0 : (iv2 > 255 ? 255 : iv2)) << 16) |
                             ((uint32_t)(iv3 < 0 ? 0 : (iv3 > 255 ? 255 : iv3)) << 24);
#endif
            }
        }
    }
    for (size_t i = 0; i < (size_t)w * h; i++) {
        buf_in[i] = buf_tmp[i];
    }
}

static void agx_blur_texture(uint32_t *buf_in, uint32_t *buf_tmp, uint32_t w, uint32_t h)
{
    float offs[6], ws[6];
    for (size_t t = 0; t < ARRAY_SIZE(agx_blur_taps); t++) {
        offs[t] = agx_blur_taps[t].offset;
        ws[t] = agx_blur_taps[t].weight;
    }
    agx_blur_1d(buf_in, buf_tmp, w, h, offs, ws, 6, false);
    agx_blur_1d(buf_in, buf_tmp, w, h, offs, ws, 6, true);
}



struct A7State;
static uint64_t agx_replay_program_ext(struct A7State *s, uint64_t cat, const uint32_t *ppp, unsigned n, bool *is_blur);

static uint64_t agx_replay_program(A7State *s, uint64_t cat, const uint32_t *ppp, unsigned n)
{
    return agx_replay_program_ext((struct A7State *)s, cat, ppp, n, NULL);
}


/* A tap-radius halo makes every interior value identical to prefiltering
 * the full source. Only unused allocation padding is excluded from the work. */
static void agx_nc_blur_roi(uint32_t *pixels, uint32_t w, uint32_t h,
                            unsigned x0, unsigned y0, unsigned x1, unsigned y1,
                            const float *offsets, const float *weights,
                            unsigned taps, bool vertical)
{
    unsigned rw = x1 - x0, rh = y1 - y0;
    uint32_t *crop = g_new(uint32_t, (size_t)rw * rh);
    uint32_t *tmp = g_new(uint32_t, (size_t)rw * rh);
    for (unsigned y = 0; y < rh; y++) {
        memcpy(crop + (size_t)y * rw, pixels + (size_t)(y0 + y) * w + x0, rw * 4);
    }
    agx_blur_1d(crop, tmp, rw, rh, offsets, weights, taps, vertical);
    for (unsigned y = 0; y < rh; y++) {
        memcpy(pixels + (size_t)(y0 + y) * w + x0, crop + (size_t)y * rw, rw * 4);
    }
    g_free(crop); g_free(tmp);
}

/* PPP packet bit-to-slot mapping (32 state words total).
 * Bits [5:0]: 1 word each (slots 0..5)
 * Bit 6: 4 words (slots 6..9; slot 6 = fragment program PDS pointer)
 * Bit 7: 1 word (slot 10)
 * Bit 8: 1 word (slot 11)
 * Bit 9: 1 word (slot 12; fragment PDS heap pointer: TSW & samplers)
 * Bit 10: 2 words (slots 13..14; region clip)
 * Bit 11: 6 words (slots 15..20; viewport)
 * Bit 16: 1 word (slot 21)
 * Bits [20:17]: 1 word each (slots 22..25)
 * Bits [22:21]: 1 word each (slots 26..27)
 * Bit 23: 2 words (slots 28..29; blend state & colour)
 * Bit 25: 2 words (slots 30..31; termination)
 */
static const uint8_t agx_ppp_bit_words[32] = {
    [0] = 1, [1] = 1, [2] = 1, [3] = 1, [4] = 1, [5] = 1,
    [6] = 4, [7] = 1, [8] = 1, [9] = 1, [10] = 2, [11] = 6,
    [16] = 1, [17] = 1, [18] = 1, [19] = 1, [20] = 1, [21] = 1, [22] = 1,
    [23] = 2, [25] = 2
};

static void agx_update_ppp_state(uint32_t *retained, const uint32_t *packet, unsigned count)
{
    if (!packet || count == 0) {
        return;
    }
    if (count == 1) {
        if ((packet[0] & 0xc0000000) == 0x80000000) {
            retained[12] = packet[0];
        } else if (packet[0] == 0) {
            retained[12] = 0;
        }
        return;
    }
    uint32_t hdr = packet[0];
    unsigned idx = 1;
    unsigned slot = 0;
    for (unsigned b = 0; b < 32; b++) {
        unsigned sz = agx_ppp_bit_words[b];
        if (!sz) {
            continue;
        }
        if (hdr & (1u << b)) {
            for (unsigned s = 0; s < sz; s++) {
                if (idx < count && slot + s < 32) {
                    retained[slot + s] = packet[idx++];
                }
            }
        }
        slot += sz;
    }
}

static uint64_t agx_replay_program_ext(A7State *s, uint64_t cat, const uint32_t *ppp, unsigned n, bool *is_blur)
{
    uint32_t off;
    uint8_t code[64];
    uint64_t h = UINT64_C(0xcbf29ce484222325);
    if (is_blur) {
        *is_blur = false;
    }
    uint32_t pds_ptr = (n == 32) ? ppp[6] : (n > 6 ? ((ppp[0] & (1u << 6)) ? (n > 7 ? ppp[7] : ppp[6]) : ppp[6]) : 0);
    if (!pds_ptr && n >= 8) {
        pds_ptr = ppp[7];
    }
    if (!pds_ptr && n > 6) {
        pds_ptr = ppp[6];
    }
    if (!pds_ptr ||
        !agx_ctx_read(s, cat, UINT64_C(0x600000000) | pds_ptr, &off, 4) ||
        !agx_ctx_read(s, cat, UINT64_C(0x700000000) | off, code, sizeof(code))) {
        return 0;
    }
    for (unsigned i = 0; i < sizeof(code); i++) {
        h = (h ^ code[i]) * UINT64_C(0x100000001b3);
    }
    if (is_blur) {
        if (h == AGX_USC_BLUR_4TAP || h == AGX_USC_BLUR_7TAP || agx_is_blur_code(code, sizeof(code))) {
            *is_blur = true;
        }
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
/* Vertex position. Strides of 20 and up carry a vec4 (x, y, z, w). CA leaves
 * w at 1 for flat layers, but 3D layer transforms (the unlock icon fly-in
 * zooms icons in perspective) are pre-multiplied, so x and y must be divided
 * by w; ignoring it drew the icons at normal size, bunched together. */
static bool agx_vertex_xy(A7State *s, uint64_t cat, uint64_t gva, unsigned stride, float xy[2])
{
    float p[4];
    if (stride < 20) {
        return agx_ctx_read(s, cat, gva, xy, 8);
    }
    if (!agx_ctx_read(s, cat, gva, p, 16)) {
        return false;
    }
    xy[0] = p[0];
    xy[1] = p[1];
    if (isfinite(p[3]) && p[3] > 1e-6f && fabsf(p[3] - 1.0f) > 1e-6f) {
        xy[0] /= p[3];
        xy[1] /= p[3];
    }
    return true;
}

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

/* The short USC preamble used by stride-12 draws exports an uploaded
 * uniform colour. PDS 0x90000004 describes four shared words (the next word carries flags);
 * constants in following allocations are NOT blur parameters for this draw. */
/* QuartzCore's eight-sample reduction uses +/- offset[0,3,5,6],
 * each with weight 1/8. The PDS uploads two vec4-aligned uniform chunks
 * starting at array entries 0 and 3 (descriptor pointer slots 2 and 3). */
static bool agx_extract_downsample(A7State *s, uint64_t cat,
                                    const uint32_t ppp[32], float offsets[4][2])
{
    uint32_t data[8];
    float first[12], rest[16];
    if (!agx_ctx_read(s, cat, AGX_PDS_HEAP | ppp[12], data, sizeof(data))) { return false; }
    uint64_t p0 = data[4] | ((uint64_t)data[5] << 32);
    uint64_t p3 = data[6] | ((uint64_t)data[7] << 32);
    if (p0 < 0x1000 || p0 >= (UINT64_C(1) << 40) ||
        p3 < 0x1000 || p3 >= (UINT64_C(1) << 40) ||
        !agx_ctx_read(s, cat, p0, first, sizeof(first)) ||
        !agx_ctx_read(s, cat, p3, rest, sizeof(rest))) { return false; }
    for (unsigned i = 0; i < 4; i++) {
        unsigned index = i == 0 ? 0 : i == 1 ? 0 : i == 2 ? 8 : 12;
        for (unsigned c = 0; c < 2; c++) {
            float v = i == 0 ? first[c] : rest[index + c];
            if (!isfinite(v) || fabsf(v) > 1) { return false; }
            offsets[i][c] = v;
        }
    }
    return true;
}

/* The normalized-copy VS uploads texmat0 after its matrix/padding. Some
 * subsequent PDS packets reuse that upload. Repeated U can exceed 1 while
 * V remains normalized, so max(U,V) cannot determine the coordinate units. */
static bool agx_nc_copy_texmat(A7State *s, uint64_t cat, uint64_t vs_data,
                                uint32_t *last_code, bool *last_normalized,
                                bool *have_last)
{
    uint32_t data[16];
    float uniforms[28];
    if (!agx_ctx_read(s, cat, vs_data, data, sizeof(data))) { return false; }
    uint64_t ptr = data[12] | ((uint64_t)data[13] << 32);
    if (ptr >= 0x1000 && ptr < (UINT64_C(1) << 40) &&
        agx_ctx_read(s, cat, ptr, uniforms, sizeof(uniforms))) {
        *last_code = data[10];
        *have_last = true;
        *last_normalized = uniforms[24] == 1 && uniforms[25] == 1 &&
                           uniforms[26] == 0 && uniforms[27] == 0;
    }
    return *have_last && *last_code == data[10] && *last_normalized;
}

static bool agx_extract_region_clear(A7State *s, uint64_t cat,
                                      const uint32_t ppp[32], uint32_t *colour)
{
    uint32_t data[4];
    float rgba[4];
    uint64_t base = AGX_PDS_HEAP | ppp[12];
    if (!agx_ctx_read(s, cat, base, data, sizeof(data)) ||
        data[2] != 0x90000004 || (data[3] & 0x0fffffff) != 0) { return false; }
    uint64_t ptr = data[0] | ((uint64_t)data[1] << 32);
    if (ptr < 0x1000 || ptr >= (UINT64_C(1) << 40) ||
        !agx_ctx_read(s, cat, ptr, rgba, sizeof(rgba))) { return false; }
    uint32_t channels[4];
    for (unsigned i = 0; i < 4; i++) {
        if (!isfinite(rgba[i]) || rgba[i] < 0 || rgba[i] > 1) { return false; }
        channels[i] = lroundf(rgba[i] * 255.0f);
    }
    *colour = channels[2] | (channels[1] << 8) | (channels[0] << 16) | (channels[3] << 24);
    return true;
}

static bool agx_extract_blur_params(A7State *s, uint64_t cat, uint64_t vs_data,
                                    const uint32_t *ppp, unsigned ppp_count,
                                    uint64_t prog, float *offsets, float *weights,
                                    unsigned *n_taps, bool *is_vertical);

static AGXReplaySnapshot *agx_capture_ta(A7State *s, uint64_t cat, const uint8_t *ta)
{
    AGXReplaySnapshot *snapshot = agx_snapshot_alloc();
#ifdef QEMU_OSDEP_H
    /* The translation cache is per thread; the main loop must not reuse
     * entries across passes after the guest remaps GPU pages. */
    agx_tlb_reset();
#endif
    agx_read_snapshot = snapshot;
    agx_capture_snapshot = true;
    uint64_t pc = ldq_le_p(ta + 0x68), vs_data = 0;
    uint32_t ppp[64] = {0}, n = 0, retained_ppp[32] = {0};
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
            agx_update_ppp_state(retained_ppp, ppp, n);
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
                /* Vertex uniform blocks include offsets and texmat after
                 * the matrix; retaining just its 64 bytes was insufficient. */
                uint64_t uniforms = data[12] | ((uint64_t)data[13] << 32);
                uint8_t uniform_bytes[192];
                if (uniforms >= 0x1000 && uniforms < (UINT64_C(1) << 40)) {
                    agx_ctx_read(s, cat, uniforms, uniform_bytes, sizeof(uniform_bytes));
                }
                AGXTexture tex;
                bool blur = false;
                uint64_t prog = agx_replay_program_ext(s, cat, retained_ppp, 32, &blur);
                if (!prog && n > 0) {
                    prog = agx_replay_program_ext(s, cat, ppp, n, &blur);
                }
                int found = agx_replay_texture(s, cat, retained_ppp, 32, prog, &tex);
                if (found == AGX_TEX_NONE && n > 0) {
                    found = agx_replay_texture(s, cat, ppp, n, prog, &tex);
                }
                uint32_t colour;
                bool clear = data[0] == 12 && blur &&
                             agx_extract_region_clear(s, cat, retained_ppp, &colour);
                if (blur && !clear) {
                    float offsets[8], weights[8];
                    unsigned taps;
                    bool vertical;
                    agx_extract_blur_params(s, cat, vs_data, retained_ppp, 32,
                                            prog, offsets, weights, &taps, &vertical);
                }
                /* Snapshot fragment metadata/parameters, not render-target
                 * texels: those are produced by queued 3D passes after TA.
                 * Uploaded texels and GPU input lifetime need live validation. */
                for (unsigned i = 0; i < 32; i++) {
                    if ((retained_ppp[i] & 0xc0000000) != 0x80000000) { continue; }
                    uint32_t desc[16], block[128];
                    if (!agx_ctx_read(s, cat, AGX_PDS_HEAP | retained_ppp[i], desc, sizeof(desc))) { continue; }
                    for (unsigned k = 0; k < 16; k += 2) {
                        uint64_t ptr = desc[k] | ((uint64_t)desc[k + 1] << 32);
                        if (ptr >= 0x1000 && ptr < (UINT64_C(1) << 40)) {
                            agx_ctx_read(s, cat, ptr, block, sizeof(block));
                        }
                    }
                }
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

static bool agx_extract_nc_blur_params(A7State *s, uint64_t cat, uint64_t vs_data,
                                       const uint32_t *ppp, unsigned ppp_count,
                                       uint64_t prog, float *offsets, float *weights,
                                       unsigned *n_taps, bool *is_vertical)
{
    if ((prog == AGX_USC_BLUR_4TAP || prog == AGX_USC_BLUR_7TAP) && ppp_count == 32) {
        unsigned n = prog == AGX_USC_BLUR_4TAP ? 4 : 7;
        uint64_t uptr, wptr;
        uint32_t desc[6];
        float uniforms[48];
        if (!agx_ctx_read(s, cat, vs_data + 48, &uptr, sizeof(uptr)) ||
            uptr < 0x1000 || uptr >= (UINT64_C(1) << 40) ||
            !agx_ctx_read(s, cat, uptr, uniforms, sizeof(uniforms)) ||
            !agx_ctx_read(s, cat, AGX_PDS_HEAP | ppp[12], desc, sizeof(desc))) { return false; }
        wptr = desc[4] | ((uint64_t)desc[5] << 32);
        if (wptr < 0x1000 || wptr >= (UINT64_C(1) << 40) ||
            !agx_ctx_read(s, cat, wptr, weights, n * sizeof(float))) { return false; }
        bool horizontal = fabsf(uniforms[17]) < 0.001f && uniforms[16] > 0.05f;
        bool vertical = fabsf(uniforms[16]) < 0.001f && uniforms[17] > 0.05f;
        float sum = 0, previous = 0;
        if (!horizontal && !vertical) { return false; }
        for (unsigned i = 0; i < n; i++) {
            float off = uniforms[16 + 4 * i + vertical];
            if (!isfinite(off) || off <= previous || off >= 50 ||
                !isfinite(weights[i]) || weights[i] < 0 || weights[i] > 0.6f) { return false; }
            offsets[i] = previous = off;
            sum += weights[i];
        }
        if (fabsf(sum - 0.5f) >= 0.01f) { return false; }
        *n_taps = n;
        *is_vertical = vertical;
        return true;
    }
    return false;
}

static bool agx_extract_blur_params(A7State *s, uint64_t cat, uint64_t vs_data,
                                    const uint32_t *ppp, unsigned ppp_count,
                                    uint64_t prog,
                                    float *offsets, float *weights, unsigned *n_taps,
                                    bool *is_vertical)
{
    unsigned def_taps = 6;
    bool def_vert = false;
    unsigned max_taps = 6;
    if (prog == AGX_USC_BLUR_4TAP) {
        def_taps = 4;
        max_taps = 4;
        for (unsigned i = 0; i < 4; i++) {
            offsets[i] = agx_blur_taps_4[i].offset;
            weights[i] = agx_blur_taps_4[i].weight;
        }
    } else if (prog == AGX_USC_BLUR_7TAP) {
        def_taps = 7;
        max_taps = 7;
        for (unsigned i = 0; i < 7; i++) {
            offsets[i] = agx_blur_taps_7[i].offset;
            weights[i] = agx_blur_taps_7[i].weight;
        }
    } else {
        def_taps = 6;
        max_taps = 6;
        for (unsigned i = 0; i < 6; i++) {
            offsets[i] = agx_blur_taps[i].offset;
            weights[i] = agx_blur_taps[i].weight;
        }
    }
    *n_taps = def_taps;
    *is_vertical = def_vert;

    bool found_offsets = false;
    bool found_weights = false;

    if (vs_data) {
        uint32_t vs_hdr[16];
        if (agx_ctx_read(s, cat, vs_data, vs_hdr, sizeof(vs_hdr))) {
            uint64_t u_ptr = vs_hdr[12] | ((uint64_t)vs_hdr[13] << 32);
            if (u_ptr >= 0x1000 && u_ptr < (UINT64_C(1) << 40)) {
                uint32_t u_words[48];
                if (agx_ctx_read(s, cat, u_ptr, u_words, sizeof(u_words))) {
                    float ox0 = *(float *)&u_words[16];
                    float oy0 = *(float *)&u_words[17];
                    if (fabsf(ox0) > 0.01f && fabsf(oy0) < 0.001f) {
                        *is_vertical = false;
                    } else if (fabsf(oy0) > 0.01f && fabsf(ox0) < 0.001f) {
                        *is_vertical = true;
                    }
                    unsigned read_taps = 0;
                    unsigned limit_taps = (max_taps > 0) ? max_taps : 7;
                    for (unsigned i = 0; i < limit_taps; i++) {
                        float off = *is_vertical ? *(float *)&u_words[16 + 4 * i + 1]
                                                 : *(float *)&u_words[16 + 4 * i];
                        if (off > 0.05f && off < 50.0f) {
                            offsets[i] = off;
                            read_taps++;
                        } else {
                            break;
                        }
                    }
                    if (read_taps >= 3) {
                        *n_taps = read_taps;
                        found_offsets = true;
                    }
                }
            }
        }
    }

    if (ppp && ppp_count) {
        for (unsigned i = 0; i < ppp_count; i++) {
            if ((ppp[i] & 0xc0000000) != 0x80000000) continue;
            uint32_t data[16];
            if (!agx_ctx_read(s, cat, AGX_PDS_HEAP | ppp[i], data, sizeof(data))) continue;
            for (unsigned k = 0; k < 16; k += 2) {
                uint64_t ptr = data[k] | ((uint64_t)data[k + 1] << 32);
                if (ptr < 0x1000 || ptr >= (UINT64_C(1) << 40)) continue;
                uint32_t f_words[128];
                if (!agx_ctx_read(s, cat, ptr, f_words, sizeof(f_words))) continue;

                /* Search for weights */
                if (!found_weights) {
                    for (unsigned n = 7; n >= 4; n = (n == 7 ? 4 : 0)) {
                        for (unsigned s_idx = 0; s_idx + n <= 128; s_idx++) {
                            float sum = 0.0f;
                            bool ok = true;
                            for (unsigned t = 0; t < n; t++) {
                                float w = *(float *)&f_words[s_idx + t];
                                if (w < 0.0005f || w > 0.6f) { ok = false; break; }
                                sum += w;
                            }
                            if (ok && fabsf(sum - 0.5f) < 0.01f) {
                                *n_taps = n;
                                for (unsigned t = 0; t < n; t++) {
                                    weights[t] = *(float *)&f_words[s_idx + t];
                                }
                                found_weights = true;
                                break;
                            }
                        }
                        if (found_weights) break;
                    }
                }

                /* Search for offsets if not found in vertex uniforms */
                if (!found_offsets) {
                    for (unsigned sub = 0; sub <= 1; sub++) {
                        for (unsigned s_idx = 0; s_idx + (*n_taps) * 4 + sub + 1 <= 128; s_idx++) {
                            bool is_h = true, is_v = true;
                            float read_offs[8];
                            for (unsigned t = 0; t < *n_taps; t++) {
                                float ox = *(float *)&f_words[s_idx + t * 4 + sub];
                                float oy = *(float *)&f_words[s_idx + t * 4 + sub + 1];
                                if (!(ox > 0.05f && ox < 50.0f && fabsf(oy) < 0.001f)) is_h = false;
                                if (!(oy > 0.05f && oy < 50.0f && fabsf(ox) < 0.001f)) is_v = false;
                                if (is_h) read_offs[t] = ox;
                                else if (is_v) read_offs[t] = oy;
                                else break;
                            }
                            if (is_h) {
                                for (unsigned t = 0; t < *n_taps; t++) offsets[t] = read_offs[t];
                                *is_vertical = false;
                                found_offsets = true;
                                break;
                            } else if (is_v) {
                                for (unsigned t = 0; t < *n_taps; t++) offsets[t] = read_offs[t];
                                *is_vertical = true;
                                found_offsets = true;
                                break;
                            }
                        }
                        if (found_offsets) break;
                    }
                }

                if (found_weights && found_offsets) break;
            }
            if (found_weights && found_offsets) break;
        }
    }

    return (found_weights && found_offsets) || (prog == AGX_USC_BLUR_4TAP || prog == AGX_USC_BLUR_7TAP);
}

static bool agx_extract_color_matrix(A7State *s, uint64_t cat,
                                     const uint32_t *ppp, unsigned ppp_count,
                                     float matrix[9], float bias[3])
{
    for (unsigned i = 0; i < ppp_count; i++) {
        uint32_t data[8];
        if ((ppp[i] & 0xc0000000) != 0x80000000 ||
            !agx_ctx_read(s, cat, AGX_PDS_HEAP | ppp[i], data, sizeof(data))) {
            continue;
        }
        for (unsigned k = 0; k < 8; k += 2) {
            uint64_t ptr = data[k] | ((uint64_t)data[k + 1] << 32);
            if (ptr < 0x1000 || ptr >= (UINT64_C(1) << 40)) {
                break;
            }
            float fl[16];
            if (!agx_ctx_read(s, cat, ptr, fl, sizeof(fl))) {
                break;
            }
            if (fl[3] == 0.0f && fl[7] == 0.0f && fl[11] == 0.0f && fl[15] == 0.0f &&
                fabsf(fl[0]) > 0.01f && fabsf(fl[12]) > 0.01f) {
                for (int c = 0; c < 3; c++) {
                    matrix[c * 3 + 0] = fl[c * 4 + 0];
                    matrix[c * 3 + 1] = fl[c * 4 + 1];
                    matrix[c * 3 + 2] = fl[c * 4 + 2];
                    bias[c] = fl[12 + c];
                }
                return true;
            }
        }
    }
    matrix[0] =  0.325948f; matrix[1] = -0.034016f; matrix[2] = -0.034016f;
    matrix[3] = -0.114432f; matrix[4] =  0.245568f; matrix[5] = -0.114432f;
    matrix[6] = -0.011552f; matrix[7] = -0.011552f; matrix[8] =  0.348448f;
    bias[0] = 0.776000f; bias[1] = 0.776000f; bias[2] = 0.776000f;
    return false;
}

static __thread uint32_t *agx_scratch1 = NULL;
static __thread size_t agx_scratch1_cap = 0;
static __thread uint32_t *agx_scratch2 = NULL;
static __thread size_t agx_scratch2_cap = 0;
static __thread uint32_t *agx_scratch_nc = NULL;
static __thread size_t agx_scratch_nc_cap = 0;

static uint32_t *agx_get_scratch1(size_t count)
{
    if (count > agx_scratch1_cap) {
        agx_scratch1 = g_realloc(agx_scratch1, count * sizeof(uint32_t));
        agx_scratch1_cap = count;
    }
    return agx_scratch1;
}

static uint32_t *agx_get_scratch2(size_t count)
{
    if (count > agx_scratch2_cap) {
        agx_scratch2 = g_realloc(agx_scratch2, count * sizeof(uint32_t));
        agx_scratch2_cap = count;
    }
    return agx_scratch2;
}

static uint32_t *agx_get_scratch_nc(size_t count)
{
    if (count > agx_scratch_nc_cap) {
        agx_scratch_nc = g_realloc(agx_scratch_nc, count * sizeof(uint32_t));
        agx_scratch_nc_cap = count;
    }
    return agx_scratch_nc;
}

/* Approximate the analytic corner shaders from their signed unit-circle
 * coordinates. Integrate
 * pixel coverage on a 4x4 grid; unlike rounding the quad this also works for
 * mirrored corners and keeps the clipping/restoration masks complementary. */
static unsigned agx_radial_coverage(float u, float v, const float map[6])
{
    unsigned inside = 0;
    for (unsigned j=0; j<4; j++) for (unsigned i=0; i<4; i++) {
        float dx=(i+0.5f)/4-0.5f, dy=(j+0.5f)/4-0.5f;
        float a=u+map[0]*dx+map[1]*dy, b=v+map[3]*dx+map[4]*dy;
        inside += a*a+b*b <= 1;
    }
    return (inside*255+8)/16;
}
static inline uint32_t agx_scale_premul(uint32_t colour, unsigned alpha)
{
    uint32_t b = agx_div255((colour & 0xff) * alpha);
    uint32_t g = agx_div255(((colour >> 8) & 0xff) * alpha);
    uint32_t r = agx_div255(((colour >> 16) & 0xff) * alpha);
    uint32_t a = agx_div255((colour >> 24) * alpha);
    return b | (g << 8) | (r << 16) | (a << 24);
}

static void agx_replay_frame(A7State *s, uint64_t cat, const uint8_t *ta,
                             uint64_t target, uint32_t width, uint32_t height,
                             bool is_clear, uint32_t pitch, uint64_t scissor_base)
{
    /* Thread-safety: row and texrow are thread-local (__thread) buffers,
     * ensuring safe re-entrancy between the dedicated off-BQL render thread
     * and the inline BQL fallback. Guest memory is accessed via
     * address_space_read/write on address_space_memory under RCU read lock. */
    static __thread uint32_t row[640 * 4], texrow[0x4000], texrow1[0x4000], maskrow[0x4000];
    uint64_t pc = ldq_le_p(ta + 0x68), vs_data = 0;
    uint32_t ppp[64], retained_ppp[32] = {0};
    unsigned ppp_count = 0, draws = 0;

    width = MIN(width, (uint32_t)ARRAY_SIZE(row));
    height = MIN(height, 4096u);
    AGXSurface sf;
    agx_surface_layout(s, cat, target, width, &sf);
    if (!sf.strip && pitch >= width * 4) { sf.pitch = pitch; }
    agx_nc_layout_count = 0;
    agx_nc_layout_active = true;
    sf.twiddled = !sf.strip && agx_nc_sparse_layout(s, cat, target, width, height);
    sf.w = width;
    sf.h = height;
    if (sf.twiddled) {
        sf.pixels = g_new0(uint32_t, (size_t)width * height);
        unsigned tiles = (width / 32) * (height / 32);
        sf.loaded = g_malloc0(tiles * 2);
        sf.dirty = sf.loaded + tiles;
        if (is_clear) { memset(sf.loaded, 1, tiles * 2); }
    }
#ifdef QEMU_OSDEP_H
    agx_tlb_reset();
    for (uint32_t y = 0; y < height; y++) {
        uint64_t row_start = agx_surface_gva(&sf, 0, y);
        uint64_t row_end = row_start + width * 4 - 1;
        agx_tlb_lookup(s, cat, row_start & ~0xfffULL);
        if ((row_start & ~0xfffULL) != (row_end & ~0xfffULL)) {
            agx_tlb_lookup(s, cat, row_end & ~0xfffULL);
        }
    }
#endif
    bool audit = agx_replay_audit_enabled();
    bool audit_clock = audit && target >= UINT64_C(0x400000000);
    unsigned before_clock = audit_clock ? agx_audit_clock(s, cat, &sf) : 0;

    if (is_clear) {
        uint32_t zero_row[ARRAY_SIZE(row)];
        memset(zero_row, 0, sizeof(zero_row));
        if (!sf.pixels) {
            for (uint32_t y = 0; y < height; y++) {
                agx_target_write(s, cat, &sf, 0, y, zero_row, width);
            }
        }
    }

    /* Default viewport covers the pass; PPP state updates it. */
    float vp[4] = { width / 2.0f, width / 2.0f, height / 2.0f, height / 2.0f };
    float last_m4[4];
    bool has_last_m4 = false;
    uint32_t last_copy_vs = 0;
    bool last_copy_normalized = false, have_copy_texmat = false;
    uint64_t decoded_nc_addr = 0;
    uint32_t decoded_nc_w = 0, decoded_nc_h = 0;
    uint32_t *decoded_nc_pixels = NULL;
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
            agx_update_ppp_state(retained_ppp, ppp, ppp_count);
            agx_replay_viewport(retained_ppp, 32, vp);
            if (ppp_count > 0) {
                agx_replay_viewport(ppp, ppp_count, vp);
            }
            if (ppp_count > 0) {
                uint32_t hdr = ppp[0];
                if (hdr & (1 << 0)) {
                    scenable = (retained_ppp[0] & (1 << 16)) != 0;
                }
                if (hdr & (1 << 5)) {
                    scindex = retained_ppp[5] & 0xffff;
                }
                if (hdr & (1 << 10)) {
                    uint32_t rc0 = retained_ppp[13];
                    uint32_t rc1 = retained_ppp[14];
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
                AGX_REPLAY_SKIP("no vertex data or stride outside 12..64");
                continue;
            }

            bool is_blur_prog = false;
            uint64_t prog = agx_replay_program_ext(s, cat, retained_ppp, 32, &is_blur_prog);
            if (!prog && ppp_count > 0) {
                prog = agx_replay_program_ext(s, cat, ppp, ppp_count, &is_blur_prog);
            }
            if (!is_blur_prog) {
                is_blur_prog = agx_is_blur_program(prog);
            }
            int found = agx_replay_texture(s, cat, retained_ppp, 32, prog, &tex);
            if (found == AGX_TEX_NONE && ppp_count > 0) {
                found = agx_replay_texture(s, cat, ppp, ppp_count, prog, &tex);
            }
            bool clip_space = stride == 16 && prog == UINT64_C(0x789b8f08a3f3a8cd);
            bool is_span = stride == 16 && (prog == AGX_USC_SPANS || prog == AGX_USC_WEBKIT_SPANS);
            bool radial_clip = stride == 24 && prog == AGX_USC_RADIAL_CLIP;
            bool radial_restore = stride == 36 && prog == AGX_USC_RADIAL_RESTORE;
            bool is_pill = stride == 36 && prog == AGX_USC_PILL_BORDER;
            if ((stride == 16 && !clip_space && !is_span) ||
                (found == AGX_TEX_NONE && !is_span && !is_pill && !radial_clip && stride != 20 && stride != 28 && !(stride == 12 && is_blur_prog)) ||
                (found != AGX_TEX_NONE && tex.w * 4 > sizeof(texrow))) {
                AGX_REPLAY_SKIP(stride == 16 && !clip_space && !is_span ? "stride 16 outside the wallpaper program" :
                                found == AGX_TEX_NONE ? (stride == 24 && prog == 0 ? "stride 24 untextured with program 0x0" : "untextured with unsupported stride") :
                                "texture wider than texrow");
                continue;
            }
            if (is_span) {
                count = MIN(w[1], 1024u);
            }
            bool nc_context = sf.twiddled || (found != AGX_TEX_NONE && tex.nc_sparse);
            uint32_t clear_colour = 0;
            bool nc_normalized = nc_context && stride == 24 &&
                prog == UINT64_C(0x8eb52cf6fe248b1d) &&
                agx_nc_copy_texmat(s, cat, vs_data, &last_copy_vs,
                                    &last_copy_normalized, &have_copy_texmat);
            bool region_clear = nc_context && stride == 12 && is_blur_prog &&
                                agx_extract_region_clear(s, cat, retained_ppp, &clear_colour);
            bool is_cg_line = (prog == UINT64_C(0xba8c50a79810e866) && (found == AGX_TEX_NONE || tex.bpp == 1));
            bool is_clock_cap = (prog == AGX_USC_ROUNDED_CAPS && count == 48 && (found == AGX_TEX_NONE || tex.bpp == 1));
            bool textured = !radial_clip && found != AGX_TEX_NONE && !is_cg_line && !is_clock_cap &&
                            (stride >= 24 || clip_space || prog == AGX_USC_ROUNDED_CAPS);
            /* Textured quads are streamed directly from guest memory. The
             * 64-vertex scratch limit only applies to the solid rasterizer;
             * WebKit batches many more glyph vertices into a single draw. */
            if (textured) {
                count = MIN(w[1], 16380u);
            }
            bool vibrancy = found == AGX_TEX_VIBRANCY;
            bool dest_in = textured && prog == AGX_USC_DEST_IN;
            bool is_blur = textured && is_blur_prog;
            bool is_color_matrix = textured && prog == AGX_USC_COLOR_MATRIX;
            if (!(textured && !is_blur && tex.nc_sparse && tex.addr != target)) {
                decoded_nc_addr = 0;
            }
            float col_mat[9], col_bias[3];
            if (is_color_matrix) {
                agx_extract_color_matrix(s, cat, ppp, ppp_count, col_mat, col_bias);
            }
            AGXTexture mask_tex = { 0 };
            bool has_mask_tex = false;
            if (stride == 36 && (prog == AGX_USC_STRIDE36_DUAL_TEX || prog == AGX_USC_CORNER_RESTORE)) {
                has_mask_tex = agx_replay_decode_mask_tsw(s, cat, retained_ppp, 32, &mask_tex);
                if (!has_mask_tex && ppp_count > 0) {
                    has_mask_tex = agx_replay_decode_mask_tsw(s, cat, ppp, ppp_count, &mask_tex);
                }
            }
            bool downsample = nc_context && textured && prog == AGX_USC_DOWNSAMPLE_8;
            float reduction_offsets[4][2];
            if (downsample && !agx_extract_downsample(s, cat, retained_ppp, reduction_offsets)) {
                AGX_REPLAY_SKIP("downsample uniforms unavailable or invalid");
                continue;
            }
            /* Eight identical zero-offset samples equal the ordinary bilinear
             * path; do not read the whole full-resolution framebuffer for it. */
            if (downsample) {
                bool nonzero = false;
                for (unsigned i = 0; i < 4; i++) {
                    nonzero |= reduction_offsets[i][0] != 0 || reduction_offsets[i][1] != 0;
                }
                downsample = nonzero;
            }
            bool nc_blur = is_blur && nc_context;
            float nc_offsets[8], nc_weights[8];
            unsigned nc_taps = 0;
            bool nc_vertical = false;
            if (nc_blur &&
                !agx_extract_nc_blur_params(s, cat, vs_data, retained_ppp, 32, prog,
                                            nc_offsets, nc_weights, &nc_taps, &nc_vertical)) {
                AGX_REPLAY_SKIP("NC blur uniforms unavailable or invalid");
                continue;
            }
            int nc_bx0 = 0, nc_by0 = 0;
            int nc_bx1 = found != AGX_TEX_NONE ? tex.w : 0;
            int nc_by1 = found != AGX_TEX_NONE ? tex.h : 0;
            if (nc_blur) {
            float lo_u = 1e9f, lo_v = 1e9f, hi_u = -1e9f, hi_v = -1e9f;
            for (unsigned i = 0; i < count; i++) {
                float uv[2];
                unsigned off = stride >= 28 ? 20 : 16;
                if (!agx_ctx_read(s, cat, buf + (uint64_t)i * stride + off, uv, sizeof(uv))) { continue; }
                lo_u = MIN(lo_u, uv[0]); hi_u = MAX(hi_u, uv[0]);
                lo_v = MIN(lo_v, uv[1]); hi_v = MAX(hi_v, uv[1]);
            }
            float su = MAX(hi_u, hi_v) <= 1.0001f ? tex.w : 1;
            float sv = MAX(hi_u, hi_v) <= 1.0001f ? tex.h : 1;
            int pad = (int)ceilf(nc_offsets[nc_taps - 1]) + 2;
            nc_bx0 = MAX(0, MIN((int)tex.w - 1, (int)floorf(lo_u * su) - pad));
            nc_by0 = MAX(0, MIN((int)tex.h - 1, (int)floorf(lo_v * sv) - pad));
            nc_bx1 = MIN((int)tex.w, MAX(nc_bx0 + 1, (int)ceilf(hi_u * su) + pad));
            nc_by1 = MIN((int)tex.h, MAX(nc_by0 + 1, (int)ceilf(hi_v * sv) + pad));
            }
            uint32_t *prepared_tex = NULL;
            if (is_blur && tex.w > 0 && tex.h > 0 && tex.w <= 2048 && tex.h <= 2048) {
                size_t num_pix = (size_t)tex.w * tex.h;
                uint32_t *raw_tex = agx_get_scratch1(num_pix);
                uint32_t *tmp_tex = agx_get_scratch2(num_pix);
                if (raw_tex && tmp_tex) {
                    bool read_ok = true;
                    if (tex.nc_sparse) {
                        read_ok = agx_nc_transfer_region(s, cat, tex.addr, tex.w, tex.h, raw_tex, false,
                                                         nc_bx0, nc_by0, nc_bx1, nc_by1);
                    } else {
                        for (uint32_t ty = nc_blur ? nc_by0 : 0; ty < (nc_blur ? nc_by1 : tex.h); ty++) {
                            if (!agx_texture_row(s, cat, &tex, ty, raw_tex + (size_t)ty * tex.w)) {
                                read_ok = false;
                                break;
                            }
                        }
                    }
                    if (read_ok) {
                        if (nc_blur) {
                            agx_nc_blur_roi(raw_tex, tex.w, tex.h, nc_bx0, nc_by0, nc_bx1, nc_by1,
                                            nc_offsets, nc_weights, nc_taps, nc_vertical);
                        } else {
                            float blur_offs[8], blur_ws[8];
                            unsigned blur_taps = 0;
                            bool blur_vert = false;
                            agx_extract_blur_params(s, cat, vs_data, ppp, ppp_count, prog,
                                                    blur_offs, blur_ws, &blur_taps, &blur_vert);
                            agx_blur_1d(raw_tex, tmp_tex, tex.w, tex.h,
                                        blur_offs, blur_ws, blur_taps, blur_vert);
                        }
                        prepared_tex = raw_tex;
                    }
                }
            }
            /* Decode an NC Morton source once per draw. Row-by-row gathers
             * would fetch the same 4KiB tile again for each of its 32 rows. */
            if (textured && !is_blur && tex.nc_sparse) {
                if (decoded_nc_addr == tex.addr && decoded_nc_w == tex.w && decoded_nc_h == tex.h) {
                    prepared_tex = decoded_nc_pixels;
                } else {
                    uint32_t *raw_tex = agx_get_scratch_nc((size_t)tex.w * tex.h);
                    if (!agx_nc_transfer(s, cat, tex.addr, tex.w, tex.h, raw_tex, false)) {
                        AGX_REPLAY_SKIP("NC source tiles unavailable");
                        continue;
                    }
                    prepared_tex = decoded_nc_pixels = raw_tex;
                    decoded_nc_addr = tex.addr != target ? tex.addr : 0;
                    decoded_nc_w = tex.w; decoded_nc_h = tex.h;
                }
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
            clip_x0 = MAX(0, clip_x0);
            clip_y0 = MAX(0, clip_y0);
            clip_x1 = MIN((int)width, clip_x1);
            clip_y1 = MIN((int)height, clip_y1);
            if (clip_x0 >= clip_x1 || clip_y0 >= clip_y1) {
                continue;
            }

            float m4[4];
            bool transformed = false;
            if (stride != 12) {
                if (agx_replay_matrix(s, cat, vs_data, m4)) {
                    memcpy(last_m4, m4, sizeof(m4));
                    has_last_m4 = true;
                    transformed = true;
                } else if ((is_span || prog == AGX_USC_WEBKIT_GLYPHS || prog == AGX_USC_WEBKIT_STRIPS ||
                            (ldl_le_p(ta + 4) != 1 && prog == UINT64_C(0x8eb52cf6fe248b1d))) && has_last_m4) {
                    memcpy(m4, last_m4, sizeof(m4));
                    transformed = true;
                }
            }
            /* WebKit's GL upload path uses a flipped pixel-space matrix and
             * Morton source atlases. CA's software-produced layout-1 targets
             * retain their established linear representation. */
            /* Uploaded atlases have power-of-two extents. The 96x96
             * rounded-button source is a software-produced linear target,
             * despite passing through the same flipped copy shader. */
            bool webkit_upload = textured && tex.layout1 &&
                                 !(tex.w & (tex.w - 1)) && !(tex.h & (tex.h - 1)) &&
                                 ldl_le_p(ta + 4) != 1 && transformed && m4[1] < 0 &&
                                 prog == UINT64_C(0x8eb52cf6fe248b1d);
            if (textured && webkit_upload && tex.layout1 && !tex.strip) {
                tex.twiddled = true;
            }
            if (is_span) {
                for (unsigned k = 0; k + 1 < count; k += 2) {
                    float xy0[2], xy1[2];
                    if (!agx_ctx_read(s, cat, buf + (uint64_t)k * stride, xy0, 8) ||
                        !agx_ctx_read(s, cat, buf + (uint64_t)(k + 1) * stride, xy1, 8)) {
                        break;
                    }
                    if (transformed) {
                        xy0[0] = (m4[0] * xy0[0] + m4[2]) * vp[0] + vp[1];
                        xy0[1] = (m4[1] * xy0[1] + m4[3]) * vp[2] + vp[3];
                        xy1[0] = (m4[0] * xy1[0] + m4[2]) * vp[0] + vp[1];
                        xy1[1] = (m4[1] * xy1[1] + m4[3]) * vp[2] + vp[3];
                    }
                    uint8_t alpha = 0;
                    agx_ctx_read(s, cat, buf + (uint64_t)k * stride + 15, &alpha, 1);
                    if (alpha == 0) {
                        continue;
                    }
                    int iy = (int)lroundf(xy0[1] - 0.5f);
                    if (iy < clip_y0 || iy >= clip_y1) {
                        continue;
                    }
                    float min_x = MIN(xy0[0], xy1[0]);
                    float max_x = MAX(xy0[0], xy1[0]);
                    int ix0 = MAX(clip_x0, (int)lroundf(min_x - 0.5f));
                    int ix1 = MIN(clip_x1, (int)lroundf(max_x - 0.5f));
                    if (ix0 >= ix1) {
                        continue;
                    }
                    if (!agx_target_read(s, cat, &sf, ix0, iy, row, ix1 - ix0)) {
                        continue;
                    }
                    uint32_t a = alpha;
                    uint32_t span_c = (a << 24) | (a << 16) | (a << 8) | a;
                    for (int x = ix0; x < ix1; x++) {
                        row[x - ix0] = agx_blend_over(row[x - ix0], span_c);
                    }
                    agx_target_write(s, cat, &sf, ix0, iy, row, ix1 - ix0);
                    any_drawn = true;
                }
                if (any_drawn) {
                    draws++;
                }
                continue;
            }
            if (is_pill) {
                for (unsigned q_start = 0; q_start + 6 <= count; q_start += 6) {
                    float vs_xy[6][2];
                    float vs_uv1[6][2], vs_uv2[6][2];
                    uint32_t colour = 0xff000000;
                    bool read_ok = true;
                    for (unsigned i = 0; i < 6; i++) {
                        uint64_t v_gva = buf + (uint64_t)(q_start + i) * stride;
                        float xy[2];
                        float uvs[4];
                        if (!agx_vertex_xy(s, cat, v_gva, stride, xy) ||
                            !agx_ctx_read(s, cat, v_gva + 20, uvs, 16)) {
                            read_ok = false;
                            break;
                        }
                        if (i == 0) {
                            if (agx_ctx_read(s, cat, v_gva + 16, &colour, 4)) {
                                colour = (colour & 0xff00ff00u) | ((colour >> 16) & 0xff) | ((colour & 0xff) << 16);
                            }
                        }
                        if (transformed) {
                            xy[0] = (m4[0] * xy[0] + m4[2]) * vp[0] + vp[1];
                            xy[1] = (m4[1] * xy[1] + m4[3]) * vp[2] + vp[3];
                        }
                        vs_xy[i][0] = xy[0];
                        vs_xy[i][1] = xy[1];
                        vs_uv1[i][0] = uvs[0];
                        vs_uv1[i][1] = uvs[1];
                        vs_uv2[i][0] = uvs[2];
                        vs_uv2[i][1] = uvs[3];
                    }
                    if (!read_ok) {
                        break;
                    }
                    float qminx = vs_xy[0][0], qmaxx = vs_xy[0][0];
                    float qminy = vs_xy[0][1], qmaxy = vs_xy[0][1];
                    for (unsigned i = 1; i < 6; i++) {
                        qminx = MIN(qminx, vs_xy[i][0]);
                        qmaxx = MAX(qmaxx, vs_xy[i][0]);
                        qminy = MIN(qminy, vs_xy[i][1]);
                        qmaxy = MAX(qmaxy, vs_xy[i][1]);
                    }
                    float qw = qmaxx - qminx;
                    float qh = qmaxy - qminy;
                    if (qw < 1e-3f || qh < 1e-3f) {
                        continue;
                    }

                    float u1_tl = 0, v1_tl = 0, u2_tl = 0, v2_tl = 0;
                    float u1_tr = 0, v1_tr = 0, u2_tr = 0, v2_tr = 0;
                    float u1_bl = 0, v1_bl = 0, u2_bl = 0, v2_bl = 0;
                    float u1_br = 0, v1_br = 0, u2_br = 0, v2_br = 0;

                    for (unsigned i = 0; i < 6; i++) {
                        if (fabsf(vs_xy[i][0] - qminx) < 1e-2f && fabsf(vs_xy[i][1] - qminy) < 1e-2f) {
                            u1_tl = vs_uv1[i][0]; v1_tl = vs_uv1[i][1];
                            u2_tl = vs_uv2[i][0]; v2_tl = vs_uv2[i][1];
                        }
                        if (fabsf(vs_xy[i][0] - qmaxx) < 1e-2f && fabsf(vs_xy[i][1] - qminy) < 1e-2f) {
                            u1_tr = vs_uv1[i][0]; v1_tr = vs_uv1[i][1];
                            u2_tr = vs_uv2[i][0]; v2_tr = vs_uv2[i][1];
                        }
                        if (fabsf(vs_xy[i][0] - qminx) < 1e-2f && fabsf(vs_xy[i][1] - qmaxy) < 1e-2f) {
                            u1_bl = vs_uv1[i][0]; v1_bl = vs_uv1[i][1];
                            u2_bl = vs_uv2[i][0]; v2_bl = vs_uv2[i][1];
                        }
                        if (fabsf(vs_xy[i][0] - qmaxx) < 1e-2f && fabsf(vs_xy[i][1] - qmaxy) < 1e-2f) {
                            u1_br = vs_uv1[i][0]; v1_br = vs_uv1[i][1];
                            u2_br = vs_uv2[i][0]; v2_br = vs_uv2[i][1];
                        }
                    }

                    float du1 = MAX(fabsf(u1_tr - u1_tl), fabsf(u1_br - u1_bl));
                    float dv1 = MAX(fabsf(v1_bl - v1_tl), fabsf(v1_br - v1_tr));
                    float du2 = MAX(fabsf(u2_tr - u2_tl), fabsf(u2_br - u2_bl));
                    float dv2 = MAX(fabsf(v2_bl - v2_tl), fabsf(v2_br - v2_tr));
                    float scale1 = MAX(du1 / qw, dv1 / qh);
                    float scale2 = MAX(du2 / qw, dv2 / qh);
                    if (scale1 < 1e-5f) scale1 = 1.0f / 31.0f;
                    if (scale2 < 1e-5f) scale2 = 1.0f / 28.0f;

                    int x0 = MAX(clip_x0, (int)floorf(qminx));
                    int x1 = MIN(clip_x1, (int)ceilf(qmaxx));
                    int y0 = MAX(clip_y0, (int)floorf(qminy));
                    int y1 = MIN(clip_y1, (int)ceilf(qmaxy));
                    if (x0 >= x1 || y0 >= y1) {
                        continue;
                    }

                    uint32_t base_a = (colour >> 24) & 0xff;
                    uint32_t base_b = (colour >> 16) & 0xff;
                    uint32_t base_g = (colour >> 8) & 0xff;
                    uint32_t base_r = colour & 0xff;

                    for (int y = y0; y < y1; y++) {
                        float cy = (float)y + 0.5f;
                        float fy = (cy - qminy) / qh;
                        int first_in = -1, last_in = -1;
                        float cov_buf[640];
                        for (int x = x0; x < x1; x++) {
                            float cx = (float)x + 0.5f;
                            float fx = (cx - qminx) / qw;
                            float u1 = (1.0f - fx) * (1.0f - fy) * u1_tl + fx * (1.0f - fy) * u1_tr +
                                       (1.0f - fx) * fy * u1_bl + fx * fy * u1_br;
                            float v1 = (1.0f - fx) * (1.0f - fy) * v1_tl + fx * (1.0f - fy) * v1_tr +
                                       (1.0f - fx) * fy * v1_bl + fx * fy * v1_br;
                            float u2 = (1.0f - fx) * (1.0f - fy) * u2_tl + fx * (1.0f - fy) * u2_tr +
                                       (1.0f - fx) * fy * u2_bl + fx * fy * u2_br;
                            float v2 = (1.0f - fx) * (1.0f - fy) * v2_tl + fx * (1.0f - fy) * v2_tr +
                                       (1.0f - fx) * fy * v2_bl + fx * fy * v2_br;
                            float r1 = sqrtf(u1 * u1 + v1 * v1);
                            float r2 = sqrtf(u2 * u2 + v2 * v2);
                            float outer_cov = 0.5f - (r1 - 1.0f) / scale1;
                            float inner_cov = 0.5f + (r2 - 1.0f) / scale2;
                            if (outer_cov > 0.0f && inner_cov > 0.0f) {
                                outer_cov = (outer_cov > 1.0f ? 1.0f : outer_cov);
                                inner_cov = (inner_cov > 1.0f ? 1.0f : inner_cov);
                                cov_buf[x - x0] = outer_cov * inner_cov;
                                if (first_in < 0) first_in = x;
                                last_in = x;
                            } else {
                                cov_buf[x - x0] = 0.0f;
                            }
                        }
                        if (first_in < 0) {
                            continue;
                        }
                        any_drawn = true;
                        int span_w = last_in - first_in + 1;
                        if (!agx_target_read(s, cat, &sf, first_in, y, row, span_w)) {
                            continue;
                        }
                        for (int x = first_in; x <= last_in; x++) {
                            float cov = cov_buf[x - x0];
                            if (cov > 0.0f) {
                                uint32_t a = (uint32_t)lroundf((float)base_a * cov);
                                uint32_t b = (uint32_t)lroundf((float)base_b * cov);
                                uint32_t g = (uint32_t)lroundf((float)base_g * cov);
                                uint32_t r = (uint32_t)lroundf((float)base_r * cov);
                                uint32_t src = (a << 24) | (b << 16) | (g << 8) | r;
                                row[x - first_in] = agx_blend_over(row[x - first_in], src);
                            }
                        }
                        agx_target_write(s, cat, &sf, first_in, y, row, span_w);
                    }
                }
                if (any_drawn) {
                    draws++;
                }
                continue;
            }
            if (!textured && !radial_clip && !(stride == 12 && is_blur_prog)) {
                float vs_xy[64][2];
                float m4[4];
                bool transformed = (stride != 12) && agx_replay_matrix(s, cat, vs_data, m4);
                unsigned v_count = MIN(count, 64u);
                for (unsigned i = 0; i < v_count; i++) {
                    float xy[2];
                    if (!agx_vertex_xy(s, cat, buf + (uint64_t)i * stride, stride, xy)) {
                        v_count = i;
                        break;
                    }
                    if (clip_space) {
                        xy[0] = xy[0] * vp[0] + vp[1];
                        xy[1] = xy[1] * vp[2] + vp[3];
                    } else if (transformed) {
                        xy[0] = (m4[0] * xy[0] + m4[2]) * vp[0] + vp[1];
                        xy[1] = (m4[1] * xy[1] + m4[3]) * vp[2] + vp[3];
                    }
                    vs_xy[i][0] = xy[0];
                    vs_xy[i][1] = xy[1];
                }
                if (v_count == 0) {
                    continue;
                }

                uint32_t colour = 0xff000000;
                if (agx_ctx_read(s, cat, buf + 16, &colour, 4)) {
                    /* Untextured vertex colour is stored RGBA in memory;
                     * swap R and B for the internal BGRA framebuffer pipeline. */
                    colour = (colour & 0xff00ff00u) | ((colour >> 16) & 0xff) | ((colour & 0xff) << 16);
                }

                /* Check if geometry is a wide line segment (all vertices are pairs of two endpoints) */
                bool is_line = false;
                float lp0[2] = { 0 }, lp1[2] = { 0 };
                if (v_count == 6) {
                    lp0[0] = vs_xy[0][0]; lp0[1] = vs_xy[0][1];
                    int other_idx = -1;
                    for (unsigned i = 1; i < v_count; i++) {
                        if (fabsf(vs_xy[i][0] - lp0[0]) > 1e-3f || fabsf(vs_xy[i][1] - lp0[1]) > 1e-3f) {
                            other_idx = i;
                            break;
                        }
                    }
                    if (other_idx < 0) {
                        /* All vertices identical: 0-length line (e.g. draw 3) */
                        is_line = true;
                        lp1[0] = lp0[0]; lp1[1] = lp0[1];
                    } else {
                        lp1[0] = vs_xy[other_idx][0]; lp1[1] = vs_xy[other_idx][1];
                        bool all_endpoints = true;
                        for (unsigned i = 0; i < v_count; i++) {
                            bool is_p0 = (fabsf(vs_xy[i][0] - lp0[0]) <= 1e-3f && fabsf(vs_xy[i][1] - lp0[1]) <= 1e-3f);
                            bool is_p1 = (fabsf(vs_xy[i][0] - lp1[0]) <= 1e-3f && fabsf(vs_xy[i][1] - lp1[1]) <= 1e-3f);
                            if (!is_p0 && !is_p1) {
                                all_endpoints = false;
                                break;
                            }
                        }
                        if (all_endpoints) {
                            is_line = true;
                        }
                    }
                }

                /* Check if geometry consists of axis-aligned quads */
                bool is_axis_aligned = false;
                unsigned q_step = (v_count == 4) ? 4 : 6;
                if (!is_line && (v_count == 4 || v_count == 6 || v_count == 12)) {
                    bool all_aa = true;
                    for (unsigned q = 0; q < v_count; q += q_step) {
                        float qminx = vs_xy[q][0], qmaxx = vs_xy[q][0];
                        float qminy = vs_xy[q][1], qmaxy = vs_xy[q][1];
                        for (unsigned k = 1; k < q_step; k++) {
                            qminx = MIN(qminx, vs_xy[q + k][0]);
                            qmaxx = MAX(qmaxx, vs_xy[q + k][0]);
                            qminy = MIN(qminy, vs_xy[q + k][1]);
                            qmaxy = MAX(qmaxy, vs_xy[q + k][1]);
                        }
                        for (unsigned k = 0; k < q_step; k++) {
                            float x = vs_xy[q + k][0], y = vs_xy[q + k][1];
                            if (!((fabsf(x - qminx) < 1e-3f || fabsf(x - qmaxx) < 1e-3f) &&
                                  (fabsf(y - qminy) < 1e-3f || fabsf(y - qmaxy) < 1e-3f))) {
                                all_aa = false;
                                break;
                            }
                        }
                        if (all_aa) {
                            bool has_tl = false, has_tr = false, has_bl = false, has_br = false;
                            for (unsigned k = 0; k < q_step; k++) {
                                float x = vs_xy[q + k][0], y = vs_xy[q + k][1];
                                if (fabsf(x - qminx) < 1e-3f && fabsf(y - qminy) < 1e-3f) has_tl = true;
                                if (fabsf(x - qmaxx) < 1e-3f && fabsf(y - qminy) < 1e-3f) has_tr = true;
                                if (fabsf(x - qminx) < 1e-3f && fabsf(y - qmaxy) < 1e-3f) has_bl = true;
                                if (fabsf(x - qmaxx) < 1e-3f && fabsf(y - qmaxy) < 1e-3f) has_br = true;
                            }
                            if ((has_tl + has_tr + has_bl + has_br) < 3) {
                                all_aa = false;
                            }
                        }
                        if (!all_aa) {
                            break;
                        }
                    }
                    is_axis_aligned = all_aa;
                }

                if (is_line) {
                    /* Wide line rasteriser: segment lp0 -> lp1 with butt ends */
                    float dx = lp1[0] - lp0[0];
                    float dy = lp1[1] - lp0[1];
                    float L2 = dx * dx + dy * dy;
                    float L = sqrtf(L2);
                    if (L >= 1e-3f) {
                        /* Line width:
                         * Second hand: red (0xff1c3edc RGBA -> 0xffdc3e1c BGRA) -> width 1.0 px
                         * Minute hand: L >= 22 px -> width 2.0 px
                         * Hour hand: L < 22 px -> width 3.0 px
                         * (Scaled proportionally if zoomed L > 40 px)
                         */
                        float width = 1.0f;
                        bool is_red = ((colour & 0x00ffffffu) == 0x00dc3e1cu);
                        if (is_red) {
                            width = (L > 40.0f) ? (L / 24.0f) * 1.0f : 1.0f;
                        } else if (L >= 22.0f) {
                            width = (L > 40.0f) ? (L / 30.0f) * 2.0f : 2.0f;
                        } else {
                            width = (L > 40.0f) ? (L / 17.0f) * 3.0f : 3.0f;
                        }
                        float half_w = width * 0.5f;
                        float inv_L = 1.0f / L;

                        int x0 = MAX(clip_x0, (int)floorf(MIN(lp0[0], lp1[0]) - half_w - 1.0f));
                        int x1 = MIN(clip_x1, (int)ceilf(MAX(lp0[0], lp1[0]) + half_w + 1.0f));
                        int y0 = MAX(clip_y0, (int)floorf(MIN(lp0[1], lp1[1]) - half_w - 1.0f));
                        int y1 = MIN(clip_y1, (int)ceilf(MAX(lp0[1], lp1[1]) + half_w + 1.0f));

                        if (x0 < x1 && y0 < y1) {
                            for (int y = y0; y < y1; y++) {
                                float cy = (float)y + 0.5f;
                                float py = cy - lp0[1];
                                int first_in = -1, last_in = -1;
                                float cov_buf[640];
                                for (int x = x0; x < x1; x++) {
                                    float cx = (float)x + 0.5f;
                                    float px = cx - lp0[0];
                                    float dot = px * dx + py * dy;
                                    float t = dot * inv_L;
                                    float min_end = (t < (L - t)) ? t : (L - t);
                                    float cov_par = min_end + 0.5f;
                                    if (cov_par <= 0.0f) {
                                        cov_buf[x - x0] = 0.0f;
                                        continue;
                                    }
                                    float cross = dx * py - dy * px;
                                    float dist_perp = fabsf(cross) * inv_L;
                                    float cov_perp = half_w + 0.5f - dist_perp;
                                    if (cov_perp > 0.0f) {
                                        cov_par = (cov_par > 1.0f) ? 1.0f : cov_par;
                                        cov_perp = (cov_perp > 1.0f) ? 1.0f : cov_perp;
                                        cov_buf[x - x0] = cov_perp * cov_par;
                                        if (first_in < 0) first_in = x;
                                        last_in = x;
                                    } else {
                                        cov_buf[x - x0] = 0.0f;
                                    }
                                }
                                if (first_in < 0) {
                                    continue;
                                }
                                any_drawn = true;
                                int span_w = last_in - first_in + 1;
                                if (!agx_target_read(s, cat, &sf, first_in, y, row, span_w)) {
                                    continue;
                                }
                                uint32_t a = colour >> 24;
                                uint32_t r = (colour >> 16) & 0xff;
                                uint32_t g = (colour >> 8) & 0xff;
                                uint32_t b = colour & 0xff;
                                for (int x = first_in; x <= last_in; x++) {
                                    float cov = cov_buf[x - x0];
                                    if (cov > 0.0f) {
                                        uint32_t ca = (uint32_t)lroundf((float)a * cov);
                                        uint32_t cr = (uint32_t)lroundf((float)r * cov);
                                        uint32_t cg = (uint32_t)lroundf((float)g * cov);
                                        uint32_t cb = (uint32_t)lroundf((float)b * cov);
                                        uint32_t src = (ca << 24) | (cr << 16) | (cg << 8) | cb;
                                        row[x - first_in] = (prog == AGX_USC_COPY) ? src
                                                                                    : agx_blend_over(row[x - first_in], src);
                                    }
                                }
                                agx_target_write(s, cat, &sf, first_in, y, row, span_w);
                            }
                        }
                    }
                } else if (is_axis_aligned) {
                    /* Axis-aligned quad fast path */
                    for (unsigned q = 0; q < v_count; q += q_step) {
                        float qminx = vs_xy[q][0], qmaxx = vs_xy[q][0];
                        float qminy = vs_xy[q][1], qmaxy = vs_xy[q][1];
                        for (unsigned k = 1; k < q_step; k++) {
                            qminx = MIN(qminx, vs_xy[q + k][0]);
                            qmaxx = MAX(qmaxx, vs_xy[q + k][0]);
                            qminy = MIN(qminy, vs_xy[q + k][1]);
                            qmaxy = MAX(qmaxy, vs_xy[q + k][1]);
                        }
                        int x0 = MAX(clip_x0, (int)lroundf(qminx));
                        int x1 = MIN(clip_x1, (int)lroundf(qmaxx));
                        int y0 = MAX(clip_y0, (int)lroundf(qminy));
                        int y1 = MIN(clip_y1, (int)lroundf(qmaxy));
                        if (x0 >= x1 || y0 >= y1) {
                            continue;
                        }
                        any_drawn = true;
                        for (int y = y0; y < y1; y++) {
                            if (!agx_target_read(s, cat, &sf, x0, y, row, x1 - x0)) {
                                continue;
                            }
                            for (int x = x0; x < x1; x++) {
                                row[x - x0] = (prog == AGX_USC_COPY) ? colour
                                                                     : agx_blend_over(row[x - x0], colour);
                            }
                            agx_target_write(s, cat, &sf, x0, y, row, x1 - x0);
                        }
                    }
                } else {
                    /* Triangle rasteriser: each consecutive vertex triple is one triangle */
                    for (unsigned t = 0; t + 2 < v_count; t += 3) {
                        const float *p0 = vs_xy[t];
                        const float *p1 = vs_xy[t + 1];
                        const float *p2 = vs_xy[t + 2];
                        float det = (p1[0] - p0[0]) * (p2[1] - p0[1]) - (p1[1] - p0[1]) * (p2[0] - p0[0]);
                        if (fabsf(det) < 1e-5f) {
                            continue;
                        }
                        float tminx = MIN(p0[0], MIN(p1[0], p2[0]));
                        float tmaxx = MAX(p0[0], MAX(p1[0], p2[0]));
                        float tminy = MIN(p0[1], MIN(p1[1], p2[1]));
                        float tmaxy = MAX(p0[1], MAX(p1[1], p2[1]));

                        int x0 = MAX(clip_x0, (int)floorf(tminx - 0.5f));
                        int x1 = MIN(clip_x1, (int)ceilf(tmaxx + 0.5f));
                        int y0 = MAX(clip_y0, (int)floorf(tminy - 0.5f));
                        int y1 = MIN(clip_y1, (int)ceilf(tmaxy + 0.5f));
                        if (x0 >= x1 || y0 >= y1) {
                            continue;
                        }
                        uint32_t tri_colour = colour;
                        if (agx_ctx_read(s, cat, buf + (uint64_t)t * stride + 16, &tri_colour, 4)) {
                            tri_colour = (tri_colour & 0xff00ff00u) | ((tri_colour >> 16) & 0xff) | ((tri_colour & 0xff) << 16);
                        }
                        uint32_t a = tri_colour >> 24;
                        uint32_t r = (tri_colour >> 16) & 0xff;
                        uint32_t g = (tri_colour >> 8) & 0xff;
                        uint32_t b = tri_colour & 0xff;
                        uint32_t src_table[17];
                        src_table[0] = 0;
                        for (unsigned c = 1; c <= 16; c++) {
                            float cov = (float)c * 0.0625f;
                            uint32_t ca = (uint32_t)lroundf((float)a * cov);
                            uint32_t cr = (uint32_t)lroundf((float)r * cov);
                            uint32_t cg = (uint32_t)lroundf((float)g * cov);
                            uint32_t cb = (uint32_t)lroundf((float)b * cov);
                            src_table[c] = (ca << 24) | (cr << 16) | (cg << 8) | cb;
                        }
                        for (int y = y0; y < y1; y++) {
                            int first_in = -1, last_in = -1;
                            uint8_t cnt_buf[640];
                            for (int x = x0; x < x1; x++) {
                                unsigned cnt = 0;
                                for (unsigned si = 0; si < 4; si++) {
                                    float sx = (float)x + (float)(2 * si + 1) * 0.125f;
                                    for (unsigned sj = 0; sj < 4; sj++) {
                                        float sy = (float)y + (float)(2 * sj + 1) * 0.125f;
                                        float e0 = (p1[0] - p0[0]) * (sy - p0[1]) - (p1[1] - p0[1]) * (sx - p0[0]);
                                        float e1 = (p2[0] - p1[0]) * (sy - p1[1]) - (p2[1] - p1[1]) * (sx - p1[0]);
                                        float e2 = (p0[0] - p2[0]) * (sy - p2[1]) - (p0[1] - p2[1]) * (sx - p2[0]);
                                        bool inside = (det > 0.0f) ? (e0 >= 0.0f && e1 >= 0.0f && e2 >= 0.0f)
                                                                   : (e0 <= 0.0f && e1 <= 0.0f && e2 <= 0.0f);
                                        if (inside) {
                                            cnt++;
                                        }
                                    }
                                }
                                cnt_buf[x - x0] = (uint8_t)cnt;
                                if (cnt > 0) {
                                    if (first_in < 0) first_in = x;
                                    last_in = x;
                                }
                            }
                            if (first_in < 0) {
                                continue;
                            }
                            any_drawn = true;
                            int span_w = last_in - first_in + 1;
                            if (!agx_target_read(s, cat, &sf, first_in, y, row, span_w)) {
                                continue;
                            }
                            for (int x = first_in; x <= last_in; x++) {
                                unsigned cnt = cnt_buf[x - x0];
                                if (cnt > 0) {
                                    uint32_t src = src_table[cnt];
                                    row[x - first_in] = (prog == AGX_USC_COPY) ? src
                                                                                : agx_blend_over(row[x - first_in], src);
                                }
                            }
                            agx_target_write(s, cat, &sf, first_in, y, row, span_w);
                        }
                    }
                }
                if (any_drawn) {
                    draws++;
                }
                continue;
            }
            for (unsigned q_start = 0; q_start + step <= count; q_start += step) {
                float minx = 1e9, miny = 1e9, maxx = -1e9, maxy = -1e9;
                float quad_xy[3][2], quad_uv[3][2], uv_map[6];
                bool affine = false;
                unsigned quad_vertices = 0;

                for (unsigned i = q_start; i < q_start + step; i++) {
                    float xy[2];
                    if (!agx_vertex_xy(s, cat, buf + (uint64_t)i * stride, stride, xy)) {
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
                float quad_uv2[3][2], uv2_map[6];
                float u2_0 = 0, u2_1 = 1, v2_0 = 0, v2_1 = 1;
                bool affine2 = false;
                if (stride == 20 || (stride == 28 && !textured)) {
                    agx_ctx_read(s, cat, buf + (uint64_t)q_start * stride + 16, &colour, 4);
                } else if (stride == 24 || stride == 28 || stride == 36 || clip_space) {
                    /* stride 36 (two UV sets, grabber/camera mask + backdrop) */
                    unsigned uv = clip_space ? 8 : (stride >= 28 ? 20 : 16);
                    u0 = v0 = 1e9; u1 = v1 = -1e9;
                    if (stride == 28 || stride == 36) {
                        agx_ctx_read(s, cat, buf + (uint64_t)q_start * stride + 16, &tint, 4);
                        if (prog == AGX_USC_WEBKIT_GLYPHS) {
                            tint = (tint & 0xff00ff00u) | ((tint & 0xff) << 16) | ((tint >> 16) & 0xff);
                        }
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
                    if (stride == 36 && (prog == AGX_USC_CORNER_RESTORE || radial_restore || prog == AGX_USC_STRIDE36_DUAL_TEX)) {
                        u2_0 = v2_0 = 1e9f; u2_1 = v2_1 = -1e9f;
                        for (unsigned i = q_start; i < q_start + step; i++) {
                            float t2[2];
                            if (!agx_ctx_read(s, cat, buf + (uint64_t)i * stride + 28, t2, 8)) {
                                break;
                            }
                            u2_0 = MIN(u2_0, t2[0]); u2_1 = MAX(u2_1, t2[0]);
                            v2_0 = MIN(v2_0, t2[1]); v2_1 = MAX(v2_1, t2[1]);
                            unsigned sub_idx = i - q_start;
                            if (sub_idx < 3) {
                                quad_uv2[sub_idx][0] = t2[0];
                                quad_uv2[sub_idx][1] = t2[1];
                                if (sub_idx == 2) {
                                    affine2 = agx_quad_affine(quad_xy, quad_uv2, uv2_map);
                                }
                            }
                        }
                    }
                }

                int x0 = MAX(clip_x0, (int)lroundf(minx)), y0 = MAX(clip_y0, (int)lroundf(miny));
                int x1 = MIN(clip_x1, (int)lroundf(maxx)), y1 = MIN(clip_y1, (int)lroundf(maxy));
                if (x0 >= x1 || y0 >= y1 || !(u1 >= u0) || !(v1 >= v0)) {
                    continue;
                }
                if (stride == 36 && prog == AGX_USC_CORNER_RESTORE && (!(u2_1 >= u2_0) || !(v2_1 >= v2_0))) {
                    continue;
                }
                if ((radial_clip && !affine) ||
                    ((radial_restore || prog == AGX_USC_STRIDE36_DUAL_TEX) && !affine2)) {
                    continue;
                }
                any_drawn = true;
                if (audit) {
                    qemu_log_mask(LOG_UNIMP, "AGX AUDIT draw target 0x%llx stride %u prog 0x%llx rect %d,%d-%d,%d texture 0x%llx tint 0x%x uv %g,%g-%g,%g\n",
                                  (unsigned long long)target, stride, (unsigned long long)prog, x0,y0,x1,y1,
                                  (unsigned long long)(textured ? tex.addr : 0), tint, u0,v0,u1,v1);
                }

                if (radial_clip) {
                    for (int y = y0; y < y1; y++) {
                        if (!agx_target_read(s, cat, &sf, x0, y, row, x1 - x0)) { continue; }
                        for (int x = x0; x < x1; x++) {
                            float u = uv_map[0]*(x+0.5f)+uv_map[1]*(y+0.5f)+uv_map[2];
                            float v = uv_map[3]*(x+0.5f)+uv_map[4]*(y+0.5f)+uv_map[5];
                            unsigned a = agx_radial_coverage(u, v, uv_map);
                            row[x-x0] = agx_scale_premul(row[x-x0], a);
                        }
                        agx_target_write(s, cat, &sf, x0, y, row, x1-x0);
                    }
                    continue;
                }

                if (region_clear) {
                    for (int x = 0; x < x1 - x0; x++) { row[x] = clear_colour; }
                    for (int y = y0; y < y1; y++) {
                        agx_target_write(s, cat, &sf, x0, y, row, x1 - x0);
                    }
                    continue;
                }
                if (stride == 12 && is_blur_prog && !textured) {
                    /* Untextured Gaussian blur backdrop pass on canvas */
                    int pad = 16;
                    int bx0 = MAX(0, x0 - pad);
                    int by0 = MAX(0, y0 - pad);
                    int bx1 = MIN((int)width, x1 + pad);
                    int by1 = MIN((int)height, y1 + pad);
                    uint32_t bw = (uint32_t)(bx1 - bx0), bh = (uint32_t)(by1 - by0);
                    if (bw > 0 && bh > 0 && bw <= 2048 && bh <= 2048) {
                        /* Formerly: raw_tex = g_malloc((size_t)bw * bh * sizeof(uint32_t)); */
                        uint32_t *raw_tex = agx_get_scratch1((size_t)bw * bh);
                        uint32_t *tmp_tex = agx_get_scratch2((size_t)bw * bh);
                        if (raw_tex && tmp_tex) {
                            bool read_ok = true;
                            for (uint32_t y = 0; y < bh; y++) {
                                if (!agx_target_read(s, cat, &sf, bx0, by0 + y, raw_tex + (size_t)y * bw, bw)) {
                                    read_ok = false;
                                    break;
                                }
                            }
                            if (read_ok) {
                                float blur_offs[8], blur_ws[8];
                                unsigned blur_taps = 0;
                                bool blur_vert = false;
                                bool blur_found = agx_extract_blur_params(s, cat, vs_data, retained_ppp, 32, prog,
                                                                          blur_offs, blur_ws, &blur_taps, &blur_vert);
                                if (blur_found || prog == AGX_USC_BLUR_4TAP || prog == AGX_USC_BLUR_7TAP) {
                                    agx_blur_1d(raw_tex, tmp_tex, bw, bh,
                                                blur_offs, blur_ws, blur_taps, blur_vert);
                                } else {
                                    agx_blur_texture(raw_tex, tmp_tex, bw, bh);
                                }
                                for (int y = y0; y < y1; y++) {
                                    size_t src_off = (size_t)(y - by0) * bw + (size_t)(x0 - bx0);
                                    agx_target_write(s, cat, &sf, x0, y, raw_tex + src_off, x1 - x0);
                                }
                            }
                        }
                    }
                    continue;
                }

                if (!textured) {
                    for (int y = y0; y < y1; y++) {
                        if (!agx_target_read(s, cat, &sf, x0, y, row, x1 - x0)) {
                            continue;
                        }
                        for (int x = x0; x < x1; x++) {
                            row[x - x0] = prog == AGX_USC_COPY ? colour :
                                         prog == AGX_USC_SOLID_PLUS_L ? agx_blend_plus_lighter(row[x - x0], colour) :
                                         agx_blend_over(row[x - x0], colour);
                        }
                        agx_target_write(s, cat, &sf, x0, y, row, x1 - x0);
                    }
                    continue;
                }

                /* Texel-unit coordinates (rectangle textures) exceed 1. */
                float su = nc_normalized || MAX(u1, v1) <= 1.0001f ? tex.w : 1;
                float sv = nc_normalized || MAX(u1, v1) <= 1.0001f ? tex.h : 1;
                float sub_w = (u1 - u0) * su;
                float sub_h = (v1 - v0) * sv;
                bool is_scaled = (fabsf((maxx - minx) - sub_w) > 0.1f ||
                                  fabsf((maxy - miny) - sub_h) > 0.1f);
                if (affine) {
                    float step_x = hypotf(uv_map[0] * su, uv_map[3] * sv);
                    float step_y = hypotf(uv_map[1] * su, uv_map[4] * sv);
                    is_scaled = fabsf(step_x - 1.0f) > 0.1f || fabsf(step_y - 1.0f) > 0.1f;
                }
                if (stride == 36 && prog == AGX_USC_STRIDE36_DUAL_TEX) {
                    bool complete = true;
                    for (int y = y0; y < y1; y++) {
                        float fv = affine ? uv_map[4] * (y + 0.5f) + uv_map[5] :
                                            agx_quad_coordinate(y, miny, maxy, v0, v1);
                        uint32_t ty = MIN(tex.h - 1, (uint32_t)MAX(0.0f, fv * sv));
                        if (!agx_texture_row(s, cat, &tex, ty, texrow)) {
                            complete = false;
                            break;
                        }
                    }
                    if (!complete) {
                        continue;
                    }
                }
                /* A cross-axis V coordinate needs multiple source rows per
                 * output row: snapshot this bounded texture for the quad (the
                 * axis-aligned path keeps its two-row cache; blur already
                 * owns a snapshot). */
                uint32_t *sample_tex = prepared_tex, *quad_tex = NULL;
                if ((downsample || (affine && fabsf(uv_map[3]) > 1e-8f)) && !sample_tex) {
                    if ((uint64_t)tex.w * tex.h > 4096 * 4096) {
                        continue;
                    }
                    quad_tex = agx_get_scratch1((size_t)tex.w * tex.h);
                    for (unsigned ty = 0; ty < tex.h; ty++) {
                        if (!agx_texture_row(s, cat, &tex, ty, quad_tex + (size_t)ty * tex.w)) {
                            quad_tex = NULL;
                            break;
                        }
                    }
                    if (!quad_tex) {
                        continue;
                    }
                    sample_tex = quad_tex;
                }
                uint32_t tint_a = tint >> 24;
                uint32_t tint_r = (tint >> 16) & 0xff;
                uint32_t tint_g = (tint >> 8) & 0xff;
                uint32_t tint_b = tint & 0xff;
                bool rounded_caps = (prog == AGX_USC_ROUNDED_CAPS && (tex.addr == UINT64_C(0x10524000) || (tex.w == 114 && tex.h == 87)));
                bool simple_blend = !dest_in && !vibrancy && (prog != AGX_USC_PLUS_L) && (prog != AGX_USC_PLUS_L2) && (prog != AGX_USC_CORNER_RESTORE);

                uint32_t a8_tint_lut[256];
                if (tex.bpp == 1) {
                    for (unsigned c = 0; c < 256; c++) {
                        uint32_t a = agx_div255(c * tint_a);
                        uint32_t r = agx_div255(tint_r * a);
                        uint32_t g = agx_div255(tint_g * a);
                        uint32_t b = agx_div255(tint_b * a);
                        a8_tint_lut[c] = (a << 24) | (r << 16) | (g << 8) | b;
                    }
                }
                bool fast_path = !sample_tex &&
                                 !clip_space && !webkit_upload &&
                                 !is_color_matrix &&
                                 !rounded_caps &&
                                 !(stride == 36 && prog == AGX_USC_CORNER_RESTORE && has_mask_tex) &&
                                 !(radial_restore && affine2) &&
                                 simple_blend;

                int cached_y0 = -1, cached_y1 = -1, cached_mask_y = -1;
                bool mask_row_ok = false;

                for (int y = y0; y < y1; y++) {
                    /* Clip coverage, not the interpolation domain. */
                    float fv = affine ? uv_map[4] * (y + 0.5f) + uv_map[5] :
                                        agx_quad_coordinate(y, miny, maxy, v0, v1);
                    if (tex.bpp == 1 && tex.w == 256 && tex.h == 256) {
                        float v_eff = (fv > 0.5f) ? (1.0f - fv) : fv;
                        fv = 2.0f * v_eff;
                    }
                    if (has_mask_tex && mask_tex.addr) {
                        uint32_t mask_ty;
                        if (prog == AGX_USC_CORNER_RESTORE) {
                            float fv2 = affine2 ? uv2_map[4] * (y + 0.5f) + uv2_map[5] :
                                                  agx_quad_coordinate(y, miny, maxy, v2_0, v2_1);
                            int mty = (int)lroundf((fv2 + 0.25f) * (float)mask_tex.h);
                            mask_ty = MAX(0, MIN((int)mask_tex.h - 1, mty));
                        } else {
                            float fv2 = uv2_map[3]*(x0+0.5f)+uv2_map[4]*(y+0.5f)+uv2_map[5];
                            /* CA's A8 corner atlas stores the lower half of an
                             * ellipse. UV1 uses mirrored nine-slice coordinates,
                             * just like the single-texture A8 path above. */
                            if (mask_tex.bpp == 1 && mask_tex.w == 256 && mask_tex.h == 256) {
                                fv2 = 2.0f * (fv2 > 0.5f ? 1.0f-fv2 : fv2);
                            }
                            float scale = MAX(u2_1,v2_1) <= 1.0001f ? mask_tex.h : 1;
                            mask_ty = MIN(mask_tex.h-1, (uint32_t)MAX(0.0f, fv2*scale));
                        }
                        if ((int)mask_ty != cached_mask_y) {
                            mask_row_ok = agx_texture_row(s, cat, &mask_tex, mask_ty, maskrow);
                            cached_mask_y = (int)mask_ty;
                        }
                    }
                    uint32_t ty0 = 0, ty1 = 0;
                    float fy = 0.0f;
                    int wy = 0;
                    if (!sample_tex) {
                        if (is_scaled || tex.linear_filter) {
                            float v_sample = fv * sv - 0.5f;
                            if (v_sample < 0.0f) v_sample = 0.0f;
                            if (v_sample > (float)(tex.h - 1)) v_sample = (float)(tex.h - 1);
                            ty0 = (uint32_t)v_sample;
                            ty1 = ty0 + 1 < tex.h ? ty0 + 1 : ty0;
                            fy = v_sample - (float)ty0;
                            wy = (int)(fy * 256.0f + 0.5f);

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
                    if (!agx_target_read(s, cat, &sf, x0, y, row, x1 - x0)) {
                        continue;
                    }
                    if (fast_path) {
                        if (is_scaled || tex.linear_filter) {
                            const uint32_t *row1 = (ty1 == ty0) ? texrow : texrow1;
                            if (tex.bpp == 1) {
                                for (int x = x0; x < x1; x++) {
                                    float fu = affine ? uv_map[0] * (x + 0.5f) + uv_map[1] * (y + 0.5f) + uv_map[2] :
                                                        agx_quad_coordinate(x, minx, maxx, u0, u1);
                                    float u_sample = fu * su - 0.5f;
                                    if (u_sample < 0.0f) u_sample = 0.0f;
                                    if (u_sample > (float)(tex.w - 1)) u_sample = (float)(tex.w - 1);
                                    uint32_t tx0 = (uint32_t)u_sample;
                                    uint32_t tx1 = tx0 + 1 < tex.w ? tx0 + 1 : tx0;
                                    float fx = u_sample - (float)tx0;
                                    uint32_t src = agx_bilinear_interpolate_wy(texrow[tx0], texrow[tx1],
                                                                               row1[tx0], row1[tx1], fx, wy);
                                    uint8_t a = src >> 24;
                                    if (a) {
                                        row[x - x0] = agx_blend_over(row[x - x0], a8_tint_lut[a]);
                                    }
                                }
                            } else if (tint != 0xffffffff) {
                                for (int x = x0; x < x1; x++) {
                                    float fu = affine ? uv_map[0] * (x + 0.5f) + uv_map[1] * (y + 0.5f) + uv_map[2] :
                                                        agx_quad_coordinate(x, minx, maxx, u0, u1);
                                    float u_sample = fu * su - 0.5f;
                                    if (u_sample < 0.0f) u_sample = 0.0f;
                                    if (u_sample > (float)(tex.w - 1)) u_sample = (float)(tex.w - 1);
                                    uint32_t tx0 = (uint32_t)u_sample;
                                    uint32_t tx1 = tx0 + 1 < tex.w ? tx0 + 1 : tx0;
                                    float fx = u_sample - (float)tx0;
                                    uint32_t src = agx_bilinear_interpolate_wy(texrow[tx0], texrow[tx1],
                                                                               row1[tx0], row1[tx1], fx, wy);
                                    if ((src & 0x00ffffff) == 0 && (src >> 24) != 0) {
                                        uint32_t a = agx_div255((src >> 24) * tint_a);
                                        uint32_t r = agx_div255(tint_r * a);
                                        uint32_t g = agx_div255(tint_g * a);
                                        uint32_t b = agx_div255(tint_b * a);
                                        src = (a << 24) | (r << 16) | (g << 8) | b;
                                    } else {
                                        uint32_t b = agx_div255((src & 0xff) * tint_b);
                                        uint32_t g = agx_div255(((src >> 8) & 0xff) * tint_g);
                                        uint32_t r = agx_div255(((src >> 16) & 0xff) * tint_r);
                                        uint32_t a = agx_div255((src >> 24) * tint_a);
                                        src = b | (g << 8) | (r << 16) | (a << 24);
                                    }
                                    row[x - x0] = agx_blend_over(row[x - x0], src);
                                }
                            } else {
                                for (int x = x0; x < x1; x++) {
                                    float fu = affine ? uv_map[0] * (x + 0.5f) + uv_map[1] * (y + 0.5f) + uv_map[2] :
                                                        agx_quad_coordinate(x, minx, maxx, u0, u1);
                                    float u_sample = fu * su - 0.5f;
                                    if (u_sample < 0.0f) u_sample = 0.0f;
                                    if (u_sample > (float)(tex.w - 1)) u_sample = (float)(tex.w - 1);
                                    uint32_t tx0 = (uint32_t)u_sample;
                                    uint32_t tx1 = tx0 + 1 < tex.w ? tx0 + 1 : tx0;
                                    float fx = u_sample - (float)tx0;
                                    uint32_t src = agx_bilinear_interpolate_wy(texrow[tx0], texrow[tx1],
                                                                               row1[tx0], row1[tx1], fx, wy);
                                    row[x - x0] = agx_blend_over(row[x - x0], src);
                                }
                            }
                        } else {
                            if (tex.bpp == 1) {
                                for (int x = x0; x < x1; x++) {
                                    float fu = affine ? uv_map[0] * (x + 0.5f) + uv_map[1] * (y + 0.5f) + uv_map[2] :
                                                        agx_quad_coordinate(x, minx, maxx, u0, u1);
                                    uint32_t tx = MIN(tex.w - 1, (uint32_t)MAX(0.0f, fu * su));
                                    uint8_t a = texrow[tx] >> 24;
                                    if (a) {
                                        row[x - x0] = agx_blend_over(row[x - x0], a8_tint_lut[a]);
                                    }
                                }
                            } else if (tint != 0xffffffff) {
                                for (int x = x0; x < x1; x++) {
                                    float fu = affine ? uv_map[0] * (x + 0.5f) + uv_map[1] * (y + 0.5f) + uv_map[2] :
                                                        agx_quad_coordinate(x, minx, maxx, u0, u1);
                                    uint32_t tx = MIN(tex.w - 1, (uint32_t)MAX(0.0f, fu * su));
                                    uint32_t src = texrow[tx];
                                    if ((src & 0x00ffffff) == 0 && (src >> 24) != 0) {
                                        uint32_t a = agx_div255((src >> 24) * tint_a);
                                        uint32_t r = agx_div255(tint_r * a);
                                        uint32_t g = agx_div255(tint_g * a);
                                        uint32_t b = agx_div255(tint_b * a);
                                        src = (a << 24) | (r << 16) | (g << 8) | b;
                                    } else {
                                        uint32_t b = agx_div255((src & 0xff) * tint_b);
                                        uint32_t g = agx_div255(((src >> 8) & 0xff) * tint_g);
                                        uint32_t r = agx_div255(((src >> 16) & 0xff) * tint_r);
                                        uint32_t a = agx_div255((src >> 24) * tint_a);
                                        src = b | (g << 8) | (r << 16) | (a << 24);
                                    }
                                    row[x - x0] = agx_blend_over(row[x - x0], src);
                                }
                            } else {
                                for (int x = x0; x < x1; x++) {
                                    float fu = affine ? uv_map[0] * (x + 0.5f) + uv_map[1] * (y + 0.5f) + uv_map[2] :
                                                        agx_quad_coordinate(x, minx, maxx, u0, u1);
                                    uint32_t tx = MIN(tex.w - 1, (uint32_t)MAX(0.0f, fu * su));
                                    row[x - x0] = agx_blend_over(row[x - x0], texrow[tx]);
                                }
                            }
                        }
                    } else {
                        for (int x = x0; x < x1; x++) {
                            float fu = affine ? uv_map[0] * (x + 0.5f) + uv_map[1] * (y + 0.5f) + uv_map[2] :
                                                agx_quad_coordinate(x, minx, maxx, u0, u1);
                            float sample_v = affine ? fv + uv_map[3] * (x + 0.5f) : fv;
                            uint32_t src;
                            uint32_t tx = MIN(tex.w - 1, (uint32_t)MAX(0.0f, fu * su));
                            if (downsample && sample_tex) {
                                src = agx_sample_downsample(sample_tex, tex.w, tex.h,
                                                            fu * su, sample_v * sv, reduction_offsets);
                            } else if (sample_tex) {
                                if (is_blur || is_scaled || tex.linear_filter) {
                                    src = agx_sample_bilinear(sample_tex, tex.w, tex.h,
                                                              fu * su, sample_v * sv);
                                } else {
                                    unsigned stx = MIN(tex.w - 1, (unsigned)MAX(0.0f, fu * su));
                                    unsigned sty = MIN(tex.h - 1, (unsigned)MAX(0.0f, sample_v * sv));
                                    src = sample_tex[(size_t)sty * tex.w + stx];
                                }
                            } else if (is_scaled || tex.linear_filter) {
                                float u_sample = fu * su - 0.5f;
                                if (u_sample < 0.0f) u_sample = 0.0f;
                                if (u_sample > (float)(tex.w - 1)) u_sample = (float)(tex.w - 1);
                                uint32_t tx0 = (uint32_t)u_sample;
                                uint32_t tx1 = tx0 + 1 < tex.w ? tx0 + 1 : tx0;
                                float fx = u_sample - (float)tx0;
                                uint32_t c00 = texrow[tx0];
                                uint32_t c10 = texrow[tx1];
                                uint32_t c01 = (ty1 == ty0) ? c00 : texrow1[tx0];
                                uint32_t c11 = (ty1 == ty0) ? c10 : texrow1[tx1];
                                src = agx_bilinear_interpolate_wy(c00, c10, c01, c11, fx, wy);
                            } else {
                                src = texrow[tx];
                            }
                            if (clip_space || webkit_upload) {
                                /* CoreImage's wallpaper source is RGBA (stock
                                 * 101@2x matches only with R/B exchanged). */
                                src = (src & 0xff00ff00u) | ((src >> 16) & 0xff) | ((src & 0xff) << 16);
                            }
                            if (is_color_matrix) {
                                float rin = (float)((src >> 16) & 0xff) / 255.0f;
                                float gin = (float)((src >> 8) & 0xff) / 255.0f;
                                float bin = (float)(src & 0xff) / 255.0f;
                                float rout = col_mat[0] * rin + col_mat[3] * gin + col_mat[6] * bin + col_bias[0];
                                float gout = col_mat[1] * rin + col_mat[4] * gin + col_mat[7] * bin + col_bias[1];
                                float bout = col_mat[2] * rin + col_mat[5] * gin + col_mat[8] * bin + col_bias[2];
                                int r = (int)lroundf(rout * 255.0f);
                                int g = (int)lroundf(gout * 255.0f);
                                int b = (int)lroundf(bout * 255.0f);
                                r = MAX(0, MIN(255, r));
                                g = MAX(0, MIN(255, g));
                                b = MAX(0, MIN(255, b));
                                src = 0xff000000u | ((uint32_t)r << 16) | ((uint32_t)g << 8) | (uint32_t)b;
                            }
                            uint32_t dst = row[x - x0];
                            if (rounded_caps) {
                                uint32_t cov = agx_div255(((src >> 16) & 0xff) * (src >> 24));
                                uint32_t a = agx_div255(tint_a * cov);
                                uint32_t r = agx_div255(tint_r * a);
                                uint32_t g = agx_div255(tint_g * a);
                                uint32_t b = agx_div255(tint_b * a);
                                src = (a << 24) | (r << 16) | (g << 8) | b;
                            } else if (tint != 0xffffffff || tex.bpp == 1) {
                                if ((src & 0x00ffffff) == 0 && (src >> 24) != 0) {
                                    uint32_t a = agx_div255((src >> 24) * tint_a);
                                    uint32_t r = agx_div255(tint_r * a);
                                    uint32_t g = agx_div255(tint_g * a);
                                    uint32_t b = agx_div255(tint_b * a);
                                    src = (a << 24) | (r << 16) | (g << 8) | b;
                                } else {
                                    uint32_t b = agx_div255((src & 0xff) * tint_b);
                                    uint32_t g = agx_div255(((src >> 8) & 0xff) * tint_g);
                                    uint32_t r = agx_div255(((src >> 16) & 0xff) * tint_r);
                                    uint32_t a = agx_div255((src >> 24) * tint_a);
                                    src = b | (g << 8) | (r << 16) | (a << 24);
                                }
                            }
                            if (stride == 36 && prog == AGX_USC_CORNER_RESTORE && has_mask_tex && mask_row_ok) {
                                float fu2 = affine2 ? uv2_map[0] * (x + 0.5f) + uv2_map[1] * (y + 0.5f) + uv2_map[2] :
                                                      agx_quad_coordinate(x, minx, maxx, u2_0, u2_1);
                                int tx2 = (int)lroundf(fu2 * (float)mask_tex.w);
                                tx2 = MAX(0, MIN((int)mask_tex.w - 1, tx2));
                                uint32_t m_a = (maskrow[tx2] >> 24) & 0xff;
                                uint32_t inv_a = 255 - m_a;
                                uint32_t sa = agx_div255((src >> 24) * inv_a);
                                uint32_t sr = agx_div255(((src >> 16) & 0xff) * inv_a);
                                uint32_t sg = agx_div255(((src >> 8) & 0xff) * inv_a);
                                uint32_t sb = agx_div255((src & 0xff) * inv_a);
                                src = (sa << 24) | (sr << 16) | (sg << 8) | sb;
                            }
                            if (radial_restore && affine2) {
                                float u = uv2_map[0]*(x+0.5f)+uv2_map[1]*(y+0.5f)+uv2_map[2];
                                float v = uv2_map[3]*(x+0.5f)+uv2_map[4]*(y+0.5f)+uv2_map[5];
                                src = agx_scale_premul(src, 255-agx_radial_coverage(u, v, uv2_map));
                                row[x-x0] = agx_blend_plus_lighter(dst, src);
                            } else if (dest_in) {
                                uint32_t a = src >> 24;
                                if (a == 255) {
                                    row[x - x0] = dst;
                                } else if (a == 0) {
                                    row[x - x0] = 0;
                                } else {
                                    uint32_t b = agx_div255((dst & 0xff) * a);
                                    uint32_t g = agx_div255(((dst >> 8) & 0xff) * a);
                                    uint32_t r = agx_div255(((dst >> 16) & 0xff) * a);
                                    uint32_t oa = agx_div255((dst >> 24) * a);
                                    row[x - x0] = b | (g << 8) | (r << 16) | (oa << 24);
                                }
                            } else if (simple_blend) {
                                row[x - x0] = agx_blend_over(dst, src);
                            } else if (vibrancy) {
                                if (stride == 36 && has_mask_tex && mask_row_ok) {
                                    float fu2 = uv2_map[0]*(x+0.5f)+uv2_map[1]*(y+0.5f)+uv2_map[2];
                                    float scale = MAX(u2_1,v2_1) <= 1.0001f ? mask_tex.w : 1;
                                    unsigned mx = MIN(mask_tex.w-1, (uint32_t)MAX(0.0f, fu2*scale));
                                    uint32_t m_alpha = maskrow[mx] >> 24;
                                    if (m_alpha == 0) {
                                        row[x - x0] = dst;
                                    } else {
                                        /* This two-sampler program has no luma constants: it
                                         * composites the backdrop through the second alpha texture. */
                                        uint32_t vib = agx_blend_over(dst, src);
                                        if (m_alpha == 255) {
                                            row[x - x0] = vib;
                                        } else {
                                            uint32_t inv = 255 - m_alpha;
                                            uint32_t b = agx_div255((vib & 0xff) * m_alpha + (dst & 0xff) * inv);
                                            uint32_t g = agx_div255(((vib >> 8) & 0xff) * m_alpha + ((dst >> 8) & 0xff) * inv);
                                            uint32_t r = agx_div255(((vib >> 16) & 0xff) * m_alpha + ((dst >> 16) & 0xff) * inv);
                                            row[x - x0] = 0xff000000u | (r << 16) | (g << 8) | b;
                                        }
                                    }
                                } else {
                                    row[x - x0] = agx_blend_vibrancy(dst, src);
                                }
                            } else if (prog == AGX_USC_PLUS_L || prog == AGX_USC_PLUS_L2 || prog == AGX_USC_CORNER_RESTORE) {
                                row[x - x0] = agx_blend_plus_lighter(dst, src);
                            } else {
                                row[x - x0] = agx_blend_over(dst, src);
                            }
                        }
                    }
                    agx_target_write(s, cat, &sf, x0, y, row, x1 - x0);
                }
            }
            if (any_drawn) {
                draws++;
            }
        } else {
            qemu_log_mask(LOG_GUEST_ERROR, "AGX replay: unknown VDM block 0x%x at 0x%llx\n",
                          w[0], (unsigned long long)pc);
            break;
        }
    }
    if (sf.pixels) {
        unsigned columns = width / 32;
        for (unsigned y = 0; y < height; y += 32) {
            for (unsigned x = 0; x < width; x += 32) {
                if (sf.dirty[(y / 32) * columns + x / 32]) {
                    agx_nc_transfer_region(s, cat, target, width, height, sf.pixels,
                                            true, x, y, x + 32, y + 32);
                }
            }
        }
        g_free(sf.loaded);
        g_free(sf.pixels);
        sf.pixels = NULL;
    }
    if (audit_clock) {
        unsigned after_clock = agx_audit_clock(s, cat, &sf);
        if (before_clock != after_clock || draws > 1) {
            qemu_log_mask(LOG_UNIMP, "AGX AUDIT result target 0x%llx draws %u clock %u -> %u clear %u strip %u pitch %u\n",
                          (unsigned long long)target, draws, before_clock, after_clock, is_clear, sf.strip, sf.pitch);
        }
    }
    qemu_log_mask(LOG_UNIMP, "AGX replay target 0x%llx draws %u\n",
                  (unsigned long long)target, draws);
    agx_nc_layout_active = false;
}
/* AGX_REPLAY_CORE_END */

/* Publish one finished job (main loop, BQL held). */
static int agx_cmp_int64(const void *a, const void *b)
{
    int64_t va = *(const int64_t *)a;
    int64_t vb = *(const int64_t *)b;
    return (va > vb) - (va < vb);
}

static void agx_perf_timer_cb(void *opaque)
{
    A7State *s = opaque;
    double avg_lat = 0, p50_lat = 0, max_lat = 0;
    if (s->agx_perf_win_lat_count > 0) {
        qsort(s->agx_perf_win_lat_samples, s->agx_perf_win_lat_count, sizeof(int64_t), agx_cmp_int64);
        avg_lat = (double)s->agx_perf_win_lat_us_sum / s->agx_perf_win_lat_count;
        p50_lat = (double)s->agx_perf_win_lat_samples[s->agx_perf_win_lat_count / 2];
        max_lat = (double)s->agx_perf_win_lat_samples[s->agx_perf_win_lat_count - 1];
    }

    double avg_q_wait = s->agx_perf_win_queue_wait_count ?
        (double)s->agx_perf_win_queue_wait_us_sum / s->agx_perf_win_queue_wait_count : 0.0;
    double avg_render_ms = s->agx_perf_win_render_count ?
        (double)s->agx_perf_win_render_us_sum / (s->agx_perf_win_render_count * 1000.0) : 0.0;
    double avg_bh_delay = s->agx_perf_win_bh_delay_count ?
        (double)s->agx_perf_win_bh_delay_us_sum / s->agx_perf_win_bh_delay_count : 0.0;
    double capture_ta_ms = (double)s->agx_perf_win_capture_ta_us / 1000.0;

    fprintf(stderr,
            "[agx-perf 5s] doorbells=%lu passes=%lu jobs=%lu latency_us(avg/p50/max)=%.1f/%.1f/%.1f "
            "queue_wait_us=%.1f render_ms=%.2f bh_delay_us=%.1f capture_ta_ms=%.2f repolls_1ms=%lu\n",
            (unsigned long)s->agx_perf_win_doorbells,
            (unsigned long)s->agx_perf_win_passes,
            (unsigned long)s->agx_perf_win_jobs,
            avg_lat, p50_lat, max_lat,
            avg_q_wait, avg_render_ms, avg_bh_delay, capture_ta_ms,
            (unsigned long)s->agx_perf_win_repolls_1ms);

    s->agx_perf_win_doorbells = 0;
    s->agx_perf_win_passes = 0;
    s->agx_perf_win_jobs = 0;
    s->agx_perf_win_repolls_1ms = 0;
    s->agx_perf_win_capture_ta_us = 0;
    s->agx_perf_win_queue_wait_us_sum = 0;
    s->agx_perf_win_queue_wait_count = 0;
    s->agx_perf_win_render_us_sum = 0;
    s->agx_perf_win_render_count = 0;
    s->agx_perf_win_bh_delay_us_sum = 0;
    s->agx_perf_win_bh_delay_count = 0;
    s->agx_perf_win_lat_us_sum = 0;
    s->agx_perf_win_lat_count = 0;

    timer_mod(s->agx_perf_timer, qemu_clock_get_ms(QEMU_CLOCK_REALTIME) + 5000);
}

static void agx_replay_complete_job(AGXRenderJob *job)
{
    A7State *s = job->s;

    if (s->agx_perf_enabled) {
        int64_t now = g_get_monotonic_time();
        s->agx_perf_total_jobs++;
        s->agx_perf_win_jobs++;
        if (job->target != 0) {
            s->agx_perf_total_passes++;
            s->agx_perf_win_passes++;
            if (job->render_end_us > job->render_start_us) {
                s->agx_perf_win_render_us_sum += (job->render_end_us - job->render_start_us);
                s->agx_perf_win_render_count++;
            }
        }
        if (job->render_start_us > job->enqueue_time_us) {
            s->agx_perf_win_queue_wait_us_sum += (job->render_start_us - job->enqueue_time_us);
            s->agx_perf_win_queue_wait_count++;
        }
        if (job->done_enqueue_us > 0) {
            s->agx_perf_win_bh_delay_us_sum += (now - job->done_enqueue_us);
            s->agx_perf_win_bh_delay_count++;
        }
        if (job->post_completion && job->doorbell_time_us > 0) {
            int64_t lat = now - job->doorbell_time_us;
            if (lat >= 0) {
                s->agx_perf_win_lat_us_sum += lat;
                if (s->agx_perf_win_lat_count < ARRAY_SIZE(s->agx_perf_win_lat_samples)) {
                    s->agx_perf_win_lat_samples[s->agx_perf_win_lat_count++] = lat;
                }
            }
        }
    }

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
    agx_job_free(job);
}

static void agx_kick_cb(void *opaque);

static void agx_replay_done_bh(void *opaque)
{
    A7State *s = opaque;
    bool stamp_updated = false;
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
            break;
        }
        if (job->has_stamp) {
            stamp_updated = true;
        }
        job->next = NULL;
        agx_replay_complete_job(job);
    }
    if (stamp_updated || s->agx_stamp_wait) {
        s->agx_stamp_wait = false;
        agx_kick_cb(s);
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

        if (s->agx_perf_enabled) {
            job->render_start_us = g_get_monotonic_time();
        }

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

        if (s->agx_perf_enabled) {
            job->render_end_us = g_get_monotonic_time();
            job->done_enqueue_us = job->render_end_us;
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
    int64_t enq_time = s->agx_perf_enabled ? g_get_monotonic_time() : 0;
    for (AGXRenderJob *j = head; j; j = j->next) {
        s->agx_inflight_jobs++;
        j->enqueue_time_us = enq_time;
    }

    if (s->agx_async_enabled && s->agx_inflight_jobs <= 3) {
        for (AGXRenderJob *j = head; j; j = j->next) {
            if (j->has_stamp) {
                agx_gpu_access(s, j->stamp_gva, &j->stamp_value, 4, true);
                j->has_stamp = false;
            }
            if (j->has_queue_ctrl) {
                smp_wmb();
                agx_gpu_access(s, j->queue_ctrl_gva, &j->queue_read_index, 4, true);
                for (unsigned i = 0; i < ARRAY_SIZE(s->agx_host_read); i++) {
                    if (s->agx_host_read[i].valid &&
                        s->agx_host_read[i].control == j->queue_ctrl_gva &&
                        s->agx_host_read[i].index == j->queue_read_index) {
                        s->agx_host_read[i].valid = false;
                    }
                }
                j->has_queue_ctrl = false;
            }
            if (j->post_completion) {
                agx_post_fw_message(s, 1);
                j->post_completion = false;
            }
        }
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
#ifdef QEMU_OSDEP_H
    agx_tlb_reset();
#endif
    uint64_t first = UINT64_MAX, actual = 0;
    static const char *fill = NULL;
    static bool fill_checked = false;
    if (__builtin_expect(!fill_checked, 0)) {
        fill = getenv("VPHONE_AGX_RT_FILL");
        fill_checked = true;
    }

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
    static const char *dump_context = NULL;
    static const char *min_env = NULL;
    static int dump_offscreen_cached = -1;
    static int dump_display_cached = -1;
    static bool dump_env_checked = false;
    if (__builtin_expect(!dump_env_checked, 0)) {
        dump_context = getenv("VPHONE_AGX_FRAME_DUMP_CONTEXT");
        min_env = getenv("VPHONE_AGX_FRAME_DUMP_MIN_DRAWS");
        dump_offscreen_cached = getenv("VPHONE_AGX_FRAME_DUMP_OFFSCREEN") != NULL;
        dump_display_cached = getenv("VPHONE_AGX_FRAME_DUMP_DISPLAY") != NULL;
        dump_env_checked = true;
    }
    bool capture_context = dump_context ?
        (!strcmp(dump_context, "all") || context_id == strtoul(dump_context, NULL, 0)) :
        (context_id == 1 && s->disp_mmu_table);
    if (capture_context && context_cat && pbe_target) {
        /* Optional: only dump frames with at least MIN_DRAWS draws (CA
         * redraws only dirty regions, so most frames are small). */
        const uint8_t *dump_ta = agx_find_ta(s, context_id, raw);
        bool offscreen = (ldl_le_p(raw + 0x74) & 0xffff) != 639 ||
                         (ldl_le_p(raw + 0x70) & 0xffff) != 1135;
        /* OFFSCREEN=1 keeps only offscreen passes, DISPLAY=1 only display ones
         * (load passes, so each replays to the whole screen). */
        if ((!dump_offscreen_cached || offscreen) &&
            (!dump_display_cached || !offscreen) &&
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
            hwaddr sc_pa;
            uint64_t sc_base_page = scissor_base & ~UINT64_C(0xfff);
            if (!g_hash_table_lookup(snapshot->pages, &sc_base_page) &&
                agx_gpu_translate_ctx(s, cat_base, sc_base_page, &sc_pa)) {
                uint8_t *page = agx_page_alloc();
                if (address_space_read(&address_space_memory, sc_pa, MEMTXATTRS_UNSPECIFIED, page, 4096) == MEMTX_OK) {
                    uint64_t *key = agx_key_alloc();
                    *key = sc_base_page;
                    g_hash_table_insert(snapshot->pages, key, page);
                } else {
                    agx_page_free(page);
                }
            }
        }
        if (!agx_replay_thread_enabled()) {
            agx_read_snapshot = snapshot;
            agx_replay_frame(s, cat_base, pass_ta, pbe_target, pass_w, pass_h, is_clear, pbe_pitch, scissor_base);
            agx_read_snapshot = NULL;
        } else if (out_job) {
            AGXRenderJob *job = agx_job_alloc();
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
    static int frame_dump_enabled = -1;
    uint32_t pointers[2], read_index, write_index, size;

    if (__builtin_expect(dump_enabled < 0, 0)) {
        dump_enabled = getenv("VPHONE_AGX_DUMP") != NULL;
    }
    if (__builtin_expect(frame_dump_enabled < 0, 0)) {
        frame_dump_enabled = getenv("VPHONE_AGX_FRAME_DUMP") != NULL;
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
#ifdef QEMU_OSDEP_H
                if (s->agx_perf_enabled) {
                    s->agx_perf_total_repolls_1ms++;
                    s->agx_perf_win_repolls_1ms++;
                }
                s->agx_stamp_wait = true;
#endif
                timer_mod(s->agx_kick_timer,
                          qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 200000);
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
                AGXRenderJob *stamp_job = agx_job_alloc();
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
            if (command[0] == 0 && (frame_dump_enabled || agx_replay_enabled()) &&
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
#ifdef QEMU_OSDEP_H
                    int64_t t_cap = s->agx_perf_enabled ? g_get_monotonic_time() : 0;
#endif
                    s->agx_ta_snapshot[context][slot] = agx_capture_ta(s, cat, ta);
#ifdef QEMU_OSDEP_H
                    if (s->agx_perf_enabled) {
                        s->agx_perf_win_capture_ta_us += (g_get_monotonic_time() - t_cap);
                    }
#endif
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
        AGXRenderJob *ctrl_job = agx_job_alloc();
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
    int64_t db_time = s->agx_doorbell_time_us ? s->agx_doorbell_time_us :
                      (s->agx_perf_enabled ? g_get_monotonic_time() : 0);
    s->agx_doorbell_in_flight = false;
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
    for (AGXRenderJob *j = local_head; j; j = j->next) {
        if (!j->doorbell_time_us) {
            j->doorbell_time_us = db_time;
        }
    }
    if (local_tail) {
        local_tail->post_completion = true;
        local_tail->commands = commands;
        local_tail->stamps = stamps;
        agx_enqueue_render_jobs(s, local_head, local_tail);
    } else if (commands) {
        if (agx_replay_thread_enabled() && s->agx_inflight_jobs > 0) {
            AGXRenderJob *comp_job = agx_job_alloc();
            comp_job->s = s;
            comp_job->post_completion = true;
            comp_job->commands = commands;
            comp_job->stamps = stamps;
            comp_job->doorbell_time_us = db_time;
            agx_enqueue_render_jobs(s, comp_job, comp_job);
        } else {
            qemu_log_mask(LOG_UNIMP, "AGX META: consumed %u commands, %u stamps\n", commands, stamps);
            agx_post_fw_message(s, 1); /* type 1: work completed */
            if (s->agx_perf_enabled && db_time > 0) {
                int64_t now = g_get_monotonic_time();
                int64_t lat = now - db_time;
                if (lat >= 0) {
                    s->agx_perf_win_lat_us_sum += lat;
                    if (s->agx_perf_win_lat_count < ARRAY_SIZE(s->agx_perf_win_lat_samples)) {
                        s->agx_perf_win_lat_samples[s->agx_perf_win_lat_count++] = lat;
                    }
                }
            }
        }
    }
}

static void agx_doorbell(A7State *s, uint64_t value)
{
    if (!s->agx_shared_gva || (value & ~UINT64_C(3)) != 0x10) {
        return;
    }
    s->agx_pending_doorbells |= 1U << (value & 3);
    if (s->agx_perf_enabled) {
        s->agx_perf_total_doorbells++;
        s->agx_perf_win_doorbells++;
        if (!s->agx_doorbell_in_flight) {
            s->agx_doorbell_time_us = g_get_monotonic_time();
            s->agx_doorbell_in_flight = true;
        }
    }
    qemu_bh_schedule(s->agx_kick_bh);
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
    /* The program has run: the run bits self-clear. The idle check at
     * 0xffffff80011874f8 waits for sgx1+0x1000 & 0xf == 0, which the sleep
     * path's power-down program never reached. */
    stl_le_p(p->regs + 0x1000, ldl_le_p(p->regs + 0x1000) & ~UINT32_C(0xf));
}

static uint64_t peripheral_read(void *opaque, hwaddr off, unsigned size)
{
    A7Peripheral *p = opaque;
    uint64_t val = peripheral_read_impl(opaque, off, size);
    if (p->base == UINT64_C(0x201e00000) && off == 0 && size == 4 &&
        p->owner->agx_inflight_jobs == 0) {
        /* sgx1+0 bit 29 is the GPU's idle status. AGXMetaFirmware's idle
         * check (0xffffff80011874d4) needs bits 29 and 30 both set before it
         * lets the display sleep; the driver only ever writes bit 30, so
         * without this the sleep path polled forever and later Home and
         * hold presses were never handled. */
        val |= UINT32_C(0x20000000);
    }
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
    if (p->base == UINT64_C(0x20a0c0000) && size == 4 &&
        ((off & 0x3fff) == 0x44 || (off & 0x3fff) == 0x48)) {
        /* MCA channel +0x44 is a self-clearing reset/flush command register
         * (bits 0/8 one direction, 1/9 the other) and +0x48 bit 0 a
         * self-clearing start. AppleAE2MCA sleeps in 10 ms steps until they
         * read back 0 (0xffffff8000fe6974, ...fe6998, ...fe70b8, ...fe712c);
         * storing them verbatim wedged mediaserverd on the lock sound and
         * every later wake. */
        uint32_t clear = (off & 0x3fff) == 0x44 ? UINT32_MAX : 1;
        stl_le_p(p->regs + off, ldl_le_p(p->regs + off) & ~clear);
    }
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

static void a7_wifi_patch_mac(A7State *s)
{
    if (!s->wifi) {
        return;
    }
    char name[32];
    /* devicetree_pa is 0x8018d4000. local-mac-address is at 0x8018d48d8 */
    address_space_read(&address_space_memory, 0x8018d48d8, MEMTXATTRS_UNSPECIFIED, name, 18);
    if (!memcmp(name, "local-mac-address\0", 18)) {
        address_space_write(&address_space_memory, 0x8018d48fc, MEMTXATTRS_UNSPECIFIED,
                            s->wifi->conf.macaddr.a, 6);
        fprintf(stderr, "WIFI DeviceTree MAC %02x:%02x:%02x:%02x:%02x:%02x written at PA 0x8018d48fc\n",
                s->wifi->conf.macaddr.a[0], s->wifi->conf.macaddr.a[1],
                s->wifi->conf.macaddr.a[2], s->wifi->conf.macaddr.a[3],
                s->wifi->conf.macaddr.a[4], s->wifi->conf.macaddr.a[5]);
        qemu_log_mask(LOG_UNIMP, "WIFI DeviceTree MAC %02x:%02x:%02x:%02x:%02x:%02x\n",
                      s->wifi->conf.macaddr.a[0], s->wifi->conf.macaddr.a[1],
                      s->wifi->conf.macaddr.a[2], s->wifi->conf.macaddr.a[3],
                      s->wifi->conf.macaddr.a[4], s->wifi->conf.macaddr.a[5]);
        return;
    }
    /* Fallback search in devicetree area */
    for (uint64_t pa = 0x8018d0000; pa < 0x8018e0000 - 48; pa += 4) {
        address_space_read(&address_space_memory, pa, MEMTXATTRS_UNSPECIFIED, name, 18);
        if (!memcmp(name, "local-mac-address\0", 18)) {
            uint32_t len = 0;
            address_space_read(&address_space_memory, pa + 32, MEMTXATTRS_UNSPECIFIED, &len, 4);
            if (len == 6) {
                address_space_write(&address_space_memory, pa + 36, MEMTXATTRS_UNSPECIFIED,
                                    s->wifi->conf.macaddr.a, 6);
                fprintf(stderr, "WIFI DeviceTree MAC %02x:%02x:%02x:%02x:%02x:%02x written at PA 0x%" PRIx64 "\n",
                        s->wifi->conf.macaddr.a[0], s->wifi->conf.macaddr.a[1],
                        s->wifi->conf.macaddr.a[2], s->wifi->conf.macaddr.a[3],
                        s->wifi->conf.macaddr.a[4], s->wifi->conf.macaddr.a[5],
                        pa + 36);
                qemu_log_mask(LOG_UNIMP, "WIFI DeviceTree MAC %02x:%02x:%02x:%02x:%02x:%02x\n",
                              s->wifi->conf.macaddr.a[0], s->wifi->conf.macaddr.a[1],
                              s->wifi->conf.macaddr.a[2], s->wifi->conf.macaddr.a[3],
                              s->wifi->conf.macaddr.a[4], s->wifi->conf.macaddr.a[5]);
                return;
            }
        }
    }
    fprintf(stderr, "WIFI DeviceTree: local-mac-address not found in PA 0x8018d0000..0x8018e0000\n");
}

/* Pacing statistics never feed guest clocks or interrupt decisions. */
static void pacing_sample(A7State *s, unsigned kind, int64_t now)
{
    if (!s->agx_perf_enabled) {
        return;
    }
    if (s->pacing[kind].last_ns) {
        uint64_t dt = now - s->pacing[kind].last_ns;
        if (!s->pacing[kind].count) {
            s->pacing[kind].first_start_host_ns = s->pacing[kind].last_ns;
        }
        s->pacing[kind].bins[MIN(dt / 100000, 1023)]++;
        s->pacing[kind].count++;
        s->pacing[kind].max_ns = MAX(s->pacing[kind].max_ns, dt);
    }
    s->pacing[kind].last_ns = now;
}

static void pacing_duration(A7State *s, unsigned kind, uint64_t dt)
{
    if (!s->agx_perf_enabled) {
        return;
    }
    if (!s->pacing[kind].count) {
        s->pacing[kind].first_start_host_ns = qemu_clock_get_ns(QEMU_CLOCK_REALTIME);
        if (kind == 3) { s->pacing[kind].first_start_host_ns -= dt; }
    }
    s->pacing[kind].bins[MIN(dt / 100000, 1023)]++;
    s->pacing[kind].count++;
    s->pacing[kind].max_ns = MAX(s->pacing[kind].max_ns, dt);
}

static void pacing_report(A7State *s, int64_t now)
{
    if (!s->agx_perf_enabled || now - s->pacing_report_ns < 5000000000LL) {
        return;
    }
    int64_t window_start = s->pacing_report_ns;
    s->pacing_report_ns = now;
    double quantiles[4][3] = {{0}};
    const char *names[] = {"vblank", "swap-complete", "vblank-late", "swap-request"};
    for (unsigned k = 0; k < 4; k++) {
        uint64_t n = s->pacing[k].count, cumulative = 0;
        const unsigned percent[] = { 50, 95, 99 };
        unsigned q = 0;
        for (unsigned b = 0; b < 1024; b++) {
            cumulative += s->pacing[k].bins[b];
            while (n && q < 3 && cumulative >= (n * percent[q] + 99) / 100) {
                /* Upper bound of bin; overflow uses the observed maximum. */
                quantiles[k][q++] = b == 1023 ? s->pacing[k].max_ns / 1e6 :
                                                               (b + 1) / 10.0;
            }
        }
        if (k < 2) {
            fprintf(stderr, "[pacing 5s] %s n=%" PRIu64
                    " interval_ms(p50/p95/p99/max)=%.2f/%.2f/%.2f/%.2f\n",
                    names[k], n, quantiles[k][0], quantiles[k][1], quantiles[k][2],
                    s->pacing[k].max_ns / 1e6);
        }
    }
    flockfile(stderr);
    fprintf(stderr, "[pacing-json] {\"version\":2,\"window_start_host_ns\":%" PRId64
            ",\"host_ns\":%" PRId64
            ",\"virtual_ns\":%" PRId64 ",\"swap_request_ms\":null,\"metrics\":{",
            window_start, now, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
    for (unsigned k = 0; k < 4; k++) {
        fprintf(stderr, "%s\"%s\":{\"n\":%" PRIu64
                ",\"first_start_host_ns\":%" PRId64 ",\"p50\":%.3f,\"p95\":%.3f,\"p99\":%.3f,\"max\":%.3f,\"bins_100us\":[",
                k ? "," : "", names[k], s->pacing[k].count,
                s->pacing[k].first_start_host_ns, quantiles[k][0], quantiles[k][1], quantiles[k][2], s->pacing[k].max_ns / 1e6);
        for (unsigned b = 0; b < 1024; b++) {
            fprintf(stderr, "%s%" PRIu64, b ? "," : "", s->pacing[k].bins[b]);
        }
        fprintf(stderr, "]}");
        memset(s->pacing[k].bins, 0, sizeof(s->pacing[k].bins));
        s->pacing[k].count = s->pacing[k].max_ns = 0;
    }
    fprintf(stderr, "}");
    if (s->ans_perf_enabled) {
        uint64_t fsyncs = 0;
        if (s->ans_worker_started) {
            qemu_mutex_lock(&s->ans_mutex);
            fsyncs = s->ans_fsyncs;
            qemu_mutex_unlock(&s->ans_mutex);
        }
        fprintf(stderr, ",\"ans\":{\"requests\":%" PRIu64
                ",\"fsyncs\":%" PRIu64 ",\"background_fsync_bql_ns\":0"
                ",\"handler_max_ns\":%" PRIu64 ",\"handler_bins_log2_ns\":[",
                s->ans_requests - s->ans_report_requests,
                fsyncs - s->ans_report_fsyncs, s->ans_window_max_ns);
        for (unsigned b = 0; b < 64; b++) {
            fprintf(stderr, "%s%" PRIu64, b ? "," : "", s->ans_window_hist[b]);
        }
        fprintf(stderr, "]}");
        s->ans_report_requests = s->ans_requests;
        s->ans_report_fsyncs = fsyncs;
        memset(s->ans_window_hist, 0, sizeof(s->ans_window_hist));
        s->ans_window_max_ns = 0;
    }
    fprintf(stderr, "}\n");
    funlockfile(stderr);
}

#ifdef CONFIG_VNC
/* Patch 0006: flush VNC after the display producer, under the BQL. */
void vphone_vnc_refresh(QemuConsole *con);
#endif

static void vblank_timer_cb(void *opaque)
{
    A7State *s = opaque;
    int64_t wall_ns = qemu_clock_get_ns(QEMU_CLOCK_REALTIME);
    pacing_sample(s, 0, wall_ns);
    if (s->agx_perf_enabled) {
        int64_t late = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) - s->vblank_deadline_ns;
        pacing_duration(s, 2, MAX(late, 0));
    }
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
                qemu_log_mask(LOG_UNIMP, "[DISP0] vblank swap completed, token=%u, mask=0x%08x\n",
                        s->disp_token, mask);
                pacing_sample(s, 1, wall_ns);
                if (s->disp_request_host_ns) {
                    pacing_duration(s, 3, MAX(wall_ns - s->disp_request_host_ns, 0));
                    s->disp_request_host_ns = 0;
                }
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
    if (s->wifi && !s->wifi_mac_patched) {
        a7_wifi_patch_mac(s);
        s->wifi_mac_patched = true;
    }
    /* Preserve phase on the virtual clock, including across WFI. A late
     * callback must not permanently add host wake-up latency to every frame.
     * Retain overdue ticks (and IRQ/draw ordering), never jump over them.
     */
    s->vblank_deadline_ns += 16666667;
    {
        /* Short hiccups keep their phase (overdue ticks run back to back),
         * but a long stall (host sleep, SIGSTOP, heavy contention) must not
         * replay minutes of vblanks in one burst: resync past 8 ticks. */
        int64_t now_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        if (now_ns - s->vblank_deadline_ns > 8 * INT64_C(16666667)) {
            s->vblank_deadline_ns = now_ns + 16666667;
        }
    }
    timer_mod(s->vblank_timer, s->vblank_deadline_ns);
    pacing_report(s, wall_ns);
#ifdef CONFIG_VNC
    vphone_vnc_refresh(s->con);
#endif
}

static void a7_reset(void *opaque)
{
    A7State *s = opaque;
    ans_drain(s);
    s->wifi_mac_patched = false;
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
        s->vblank_deadline_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 16666667;
        timer_mod(s->vblank_timer, s->vblank_deadline_ns);
        memset(s->pacing, 0, sizeof(s->pacing));
        s->pacing_report_ns = qemu_clock_get_ns(QEMU_CLOCK_REALTIME);
    }
    if (s->agx_meta_timer) {
        timer_del(s->agx_meta_timer);
    }
    if (s->agx_kick_timer) {
        timer_del(s->agx_kick_timer);
    }
    s->agx_cat_base = 0;
    s->disp_mmu_table = 0;
    s->disp_mapping_valid = false;
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
    s->agx_frame_dump_window = 0;
    s->agx_frame_dump_in_window = 0;
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
            agx_job_free(job);
            job = next;
        }
        s->agx_job_head = NULL;
        s->agx_job_tail = NULL;
        for (job = s->agx_done_head; job; ) {
            AGXRenderJob *next = job->next;
            agx_snapshot_unref(job->snapshot);
            agx_job_free(job);
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
    if (s->wifi) wifi_power_reset(s->wifi);
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
    memset(&s->multi_touch, 0, sizeof(s->multi_touch));
    timer_del(s->home_timer);
    s->home_pressed = false;
    timer_del(s->hold_timer);
    s->hold_pressed = false;
    s->backlight_off = false;
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
    s->disp_request_host_ns = 0;
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
    if (!surface) {
        return false;
    }
    uint8_t *ram = memory_region_get_ram_ptr(MACHINE(s)->ram);
    uint32_t dst_stride = surface_stride(surface);
    uint8_t *dst_base = surface_data(surface);

    if (s->disp_mmu_table) {
        if (!s->disp_mapping_valid || s->disp_cached_mmu_table != s->disp_mmu_table ||
            s->disp_cached_offset != s->disp_offset) {
            uint32_t start_page = s->disp_offset >> 12;
            uint32_t end_page = (s->disp_offset + 2560 * 1136 + 0xfff) >> 12;
            bool contiguous = true;
            bool all_valid = true;
            uint64_t prev_pa = 0;

            for (uint32_t p = start_page; p < end_page && p < ARRAY_SIZE(s->disp_page_ram_offset); p++) {
                MemTxResult r;
                uint32_t e = address_space_ldl_le(&address_space_memory,
                                                  s->disp_mmu_table + p * 4,
                                                  MEMTXATTRS_UNSPECIFIED, &r);
                uint64_t pa = (uint64_t)(e & ~0xfU) << 4;
                if (r == MEMTX_OK && (e & 1) && pa >= RAM_BASE && pa + 0x1000 <= RAM_BASE + GiB) {
                    s->disp_page_ram_offset[p] = (uint32_t)(pa - RAM_BASE);
                } else {
                    s->disp_page_ram_offset[p] = 0;
                    all_valid = false;
                }
                if (p == start_page) {
                    prev_pa = pa;
                } else {
                    if (pa != prev_pa + 0x1000) {
                        contiguous = false;
                    }
                    prev_pa = pa;
                }
            }
            s->disp_mapping_contiguous = (all_valid && contiguous);
            s->disp_cached_pa_base = (all_valid && contiguous) ?
                (uint64_t)s->disp_page_ram_offset[start_page] + (s->disp_offset & 0xfff) : 0;
            s->disp_cached_mmu_table = s->disp_mmu_table;
            s->disp_cached_offset = s->disp_offset;
            s->disp_mapping_valid = all_valid;
        }
    }

    uint8_t temp_row[2560];
    int dirty_start = -1;

    for (unsigned y = 0; y < 1136; y++) {
        const uint8_t *src_row;
        if (!s->disp_mmu_table) {
            src_row = ram + s->framebuffer - RAM_BASE + y * 2560;
        } else if (s->disp_mapping_contiguous) {
            src_row = ram + s->disp_cached_pa_base + y * 2560;
        } else {
            uint64_t o = s->disp_offset + (uint64_t)y * 2560;
            for (unsigned x = 0; x < 2560;) {
                uint32_t p = (uint32_t)((o + x) >> 12);
                uint32_t poff = (uint32_t)((o + x) & 0xfff);
                unsigned chunk = MIN(2560 - x, 0x1000 - poff);
                if (p < ARRAY_SIZE(s->disp_page_ram_offset) && s->disp_page_ram_offset[p]) {
                    memcpy(temp_row + x, ram + s->disp_page_ram_offset[p] + poff, chunk);
                } else {
                    memset(temp_row + x, 0, chunk);
                }
                x += chunk;
            }
            src_row = temp_row;
        }

        uint8_t *dst_row = dst_base + y * dst_stride;
        if (memcmp(dst_row, src_row, 2560) != 0) {
            memcpy(dst_row, src_row, 2560);
            if (dirty_start < 0) {
                dirty_start = (int)y;
            }
        } else {
            if (dirty_start >= 0) {
                qemu_console_update(s->con, 0, dirty_start, 640, (int)y - dirty_start);
                dirty_start = -1;
            }
        }
    }
    if (dirty_start >= 0) {
        qemu_console_update(s->con, 0, dirty_start, 640, 1136 - dirty_start);
    }
    return true;
}
static const GraphicHwOps display_ops = { .gfx_update = display_update };

/* USB complex DT ranges: arm-io +0xc000000, EHCI +0x200000/+0x400000.
 * Use the EHCI engine, including real reset/run/halt, frame clock, W1C,
 * schedule and IRQ semantics. Optional WLAN attaches to EHCI1 port 1.
 * The root-port counts cover the DT's EHCI0 ports 1/3 and EHCI1 port 1;
 * these are a modeled topology, not recovered capability/OTP dumps.
 */
static void a7_wifi_init(A7State *s, DeviceState *ehci)
{
    if (!s->wifi_netdev) return;
    USBDevice *dev = USB_DEVICE(qdev_new(TYPE_WIFI_DONGLE));
    qdev_prop_set_string(DEVICE(dev), "netdev", s->wifi_netdev);
    if (s->wifi_ssid) qdev_prop_set_string(DEVICE(dev), "ssid", s->wifi_ssid);
    if (s->wifi_scan_file) qdev_prop_set_string(DEVICE(dev), "scan-file", s->wifi_scan_file);
    qdev_prop_set_string(DEVICE(dev), "port", "1");
    if (s->wifi_rom) qdev_prop_set_bit(DEVICE(dev), "rom", true);
    usb_realize_and_unref(dev, &SYS_BUS_EHCI(ehci)->ehci.bus, &error_fatal);
    s->wifi = WIFI_DONGLE(dev);
    wifi_host_ready(s->wifi, ldl_le_p(s->gpio_regs + 166 * 4) & 1);
}

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
    /* Alias the bounded system DRAM aperture, not the entire RAM leaf.
     * QEMU 11 flattens a full-leaf, zero-offset alias to the RAM region;
     * that loses the relocation when the leaf lives at RAM_BASE. */
    memory_region_init_alias(&s->usb_dma_ram, OBJECT(s), "a7.usb-dram",
                             get_system_memory(), RAM_BASE, GiB);
    memory_region_add_subregion(&s->usb_dma_memory, 0, &s->usb_dma_ram);
    address_space_init(&s->usb_dma_as, &s->usb_dma_memory, "a7.usb-dma");
    static const char *const types[] = { "a7.ehci0", "a7.ehci1" };
    for (unsigned i = 0; i < ARRAY_SIZE(types); i++) {
        DeviceState *dev = qdev_new(types[i]);
        SysBusDevice *bus = SYS_BUS_DEVICE(dev);
        sysbus_realize_and_unref(bus, &error_fatal);
        SYS_BUS_EHCI(dev)->ehci.as = &s->usb_dma_as;
        if (i == 1) a7_wifi_init(s, dev);
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
    s->home_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, home_release, s);
    s->hold_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, hold_release, s);
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
    s->agx_kick_bh = qemu_bh_new(agx_kick_cb, s);
    s->agx_job_head = NULL;
    s->agx_job_tail = NULL;
    s->agx_render_thread_running = false;
    s->agx_render_thread_exit = false;
    s->agx_inflight_jobs = 0;
    agx_render_ensure_thread(s);
    const char *perf_stats_env = getenv("VPHONE_PERF_STATS");
    s->agx_perf_enabled = perf_stats_env && strcmp(perf_stats_env, "0") != 0;
    if (s->agx_perf_enabled) {
        s->agx_perf_timer = timer_new_ms(QEMU_CLOCK_REALTIME, agx_perf_timer_cb, s);
        timer_mod(s->agx_perf_timer, qemu_clock_get_ms(QEMU_CLOCK_REALTIME) + 5000);
    }
    const char *async_env = getenv("VPHONE_AGX_ASYNC");
    s->agx_async_enabled = async_env && strcmp(async_env, "0") != 0;
    ans_init_storage(s);
    ans_init_utility(s);
    s->ans_perf_enabled = perf_stats_env && strcmp(perf_stats_env, "0") != 0;
    s->ans_flush_timer = timer_new_ms(QEMU_CLOCK_REALTIME, ans_flush_timer_cb, s);
    s->ans_exit_notifier.notify = ans_exit_notify;
    s->ans_shutdown_notifier.notify = ans_shutdown_notify;
    qemu_add_exit_notifier(&s->ans_exit_notifier);
    qemu_register_shutdown_notifier(&s->ans_shutdown_notifier);
    s->ans_notifiers_registered = true;
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
    ans_drain(s);
    ans_worker_stop(s);
    if (s->ans_notifiers_registered) {
        qemu_unregister_reset(a7_reset, s);
        notifier_remove(&s->ans_exit_notifier);
        notifier_remove(&s->ans_shutdown_notifier);
    }
    if (s->ans_flush_timer) { timer_free(s->ans_flush_timer); }
    if (s->sys_fd >= 0) { close(s->sys_fd); }
    if (s->data_fd >= 0) { close(s->data_fd); }
    if (s->utility_fd >= 0) { close(s->utility_fd); }
    if (s->agx_perf_timer) {
        timer_del(s->agx_perf_timer);
        timer_free(s->agx_perf_timer);
        s->agx_perf_timer = NULL;
    }
    if (s->agx_kick_bh) {
        qemu_bh_delete(s->agx_kick_bh);
        s->agx_kick_bh = NULL;
    }
    g_free(s->wifi_netdev);
    g_free(s->wifi_ssid);
    g_free(s->wifi_scan_file);
    g_free(s->system_image);
    g_free(s->data_image);
    g_free(s->utility_image);
}
/* Read-only readiness for host gestures; this never forces the digitizer gate. */
static bool a7_touch_reporting(Object *o, Error **errp)
{
    A7State *s = A7(o);
    A7Touch *t = touch_device(s);
    return t && (s->touch_force || touch_reporting_enabled(t));
}

static bool a7_display_on(Object *o, Error **errp)
{
    return !A7(o)->backlight_off;
}

static void a7_instance_init(Object *o)
{
    A7State *s = A7(o);
    object_property_add_bool(o, "touch-reporting-enabled", a7_touch_reporting, NULL);
    object_property_add_bool(o, "display-on", a7_display_on, NULL);
    s->sys_fd = -1;
    s->data_fd = -1;
    s->utility_fd = -1;
    object_property_add(o, "wifi-netdev", "str", get_image, set_image, NULL,
                        (void *)offsetof(A7State, wifi_netdev));
    object_property_add(o, "wifi-ssid", "str", get_image, set_image, NULL,
                        (void *)offsetof(A7State, wifi_ssid));
    object_property_add(o, "wifi-scan-file", "str", get_image, set_image, NULL,
                        (void *)offsetof(A7State, wifi_scan_file));
    object_property_add(o, "wifi-rom", "uint64", get_address, set_address, NULL,
                        (void *)offsetof(A7State, wifi_rom));
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
    type_register_static(&wifi_type);
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
