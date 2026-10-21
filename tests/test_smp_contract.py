"""Exercise the board's SMP register helpers without building/running QEMU."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'backend/s5l8960x.c'


class SMPRegisterTests(unittest.TestCase):
    def test_actual_board_helpers(self):
        compiler = shutil.which('cc')
        if not compiler:
            self.skipTest('host C compiler unavailable')
        source = SOURCE.read_text()
        helpers = source[source.index('static unsigned a7_current_cpu('):
                         source.index('static int uart_can_read(')]
        helpers += source[source.index('static bool aic_cpu_window('):
                          source.index('static const MemoryRegionOps uart_ops')]
        helpers += source[source.index('static void a7_release_cpus('):
                          source.index('static void pmgr_write(')]
        helpers += source[source.index('static void timer_irq('):
                          source.index('static uint64_t apple_c15_read(')]
        harness = r'''
#include <assert.h>
#include <stdio.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <inttypes.h>
#define NIRQ 1024
#define MACHINE(s) (s)
#define CPU(c) (c)
#define DEVICE(c) (c)
#define ARM_CPU_IRQ 0
#define ARM_CPU_FIQ 1
#define LOG_UNIMP 0
#define LOG_GUEST_ERROR 1
#define HWADDR_PRIx PRIx64
#define qemu_log_mask(...) ((void)0)
#define QEMU_ARM_POWERCTL_RET_SUCCESS 0
typedef uint64_t hwaddr;
typedef struct { int lines[2]; } ARMCPU;
typedef struct {
    struct { unsigned cpus; } smp;
    ARMCPU *ap_cpu[2];
    uint32_t mask[32], soft[32], device_irq[32], targets[NIRQ], uart_reg[16];
    uint32_t ipi[2], ipi_mask[2];
    bool timer_level[2][2], secondary_started;
    unsigned rx_count;
    uint64_t boot_el, boot_entry;
} A7State;
static ARMCPU *current_cpu;
static int *qdev_get_gpio_in(ARMCPU *c, int n) { return &c->lines[n]; }
static void qemu_set_irq(int *line, int level) { *line = level; }
static unsigned starts;
static uint64_t start_entry, start_el;
static int arm_set_cpu_on(uint64_t id, uint64_t entry, uint64_t context,
                          uint32_t el, bool aa64) {
    assert(id == 1 && context == 0 && aa64);
    starts++; start_entry = entry; start_el = el;
    return 0;
}
'''
        checks = r'''
int main(void) {
    ARMCPU cpus[2] = {0};
    A7State s = {.smp.cpus = 2, .ap_cpu = {&cpus[0], &cpus[1]}};
    memset(s.mask, 0xff, sizeof(s.mask));
    memset(s.ipi_mask, 0xff, sizeof(s.ipi_mask));
    current_cpu = &cpus[0];
    aic_write(&s, 0x2008, 2, 4);
    assert(s.ipi[1] == 1 && s.ipi[0] == 0);
    aic_write(&s, 0x50a8, 1, 4); /* CPU1's explicit mask-clear bank */
    assert(cpus[1].lines[0] && !cpus[0].lines[0]);
    assert(aic_read(&s, 0x2004, 4) == 0); /* CPU0 cannot steal CPU1 IPI */
    current_cpu = &cpus[1];
    assert(aic_read(&s, 0x2000, 4) == 1);
    assert(aic_read(&s, 0x2004, 4) == 0x40001);
    assert(!cpus[1].lines[0] && s.ipi[1] == 1); /* IAR auto-mask, ACK separate */
    aic_write(&s, 0x200c, 1, 4);
    aic_write(&s, 0x2028, 1, 4);
    assert(!cpus[1].lines[0]);
    aic_write(&s, 0x2008, 0x80000001, 4); /* CPU1 self + CPU0 cross */
    aic_write(&s, 0x2028, 0x80000000, 4);
    assert(aic_read(&s, 0x2004, 4) == 0x40002);
    assert(s.ipi[0] == 1 && s.ipi[1] == 0x80000000);
    aic_write(&s, 0x200c, 0x80000000, 4);
    current_cpu = &cpus[0];
    aic_write(&s, 0x2028, 1, 4);
    assert(aic_read(&s, 0x2004, 4) == 0x40001);
    aic_write(&s, 0x200c, 1, 4);
    /* One shared IRQ source, targeted to CPU1. Shared auto-mask prevents theft. */
    aic_write(&s, 0x3000 + 42 * 4, 2, 4);
    aic_write(&s, 0x4004, 1 << 10, 4);
    aic_write(&s, 0x4184, 1 << 10, 4);
    assert(!cpus[0].lines[0] && cpus[1].lines[0]);
    assert(aic_read(&s, 0x2004, 4) == 0);
    assert(aic_read(&s, 0x5084, 4) == 0x1002a);
    assert(!cpus[1].lines[0]);
    assert(aic_read(&s, 0x5084, 4) == 0);
    aic_write(&s, 0x4184, 1 << 10, 4);
    assert(cpus[1].lines[0]); /* asserted level returns after unmask */
    aic_write(&s, 0x4084, 1 << 10, 4);
    assert(!cpus[1].lines[0]);
    /* FIQ timer OR is local to each core. */
    timer_irq(&s, 2, 1); timer_irq(&s, 3, 1); timer_irq(&s, 2, 0);
    assert(cpus[1].lines[1] && !cpus[0].lines[1]);
    timer_irq(&s, 0, 1); timer_irq(&s, 3, 0);
    assert(cpus[0].lines[1] && !cpus[1].lines[1]);
    s.boot_el = 1; s.boot_entry = UINT64_C(0x800000000);
    a7_release_cpus(&s, 1); assert(starts == 0);
    a7_release_cpus(&s, 2); a7_release_cpus(&s, 2);
    assert(starts == 1 && start_el == 1 && start_entry == s.boot_entry);
    s.secondary_started = false;
    s.boot_el = 3; s.boot_entry = UINT64_C(0x80060c120);
    a7_release_cpus(&s, 2);
    assert(starts == 2 && start_el == 3 && start_entry == UINT64_C(0x800600000));
    /* Legacy one-CPU routing ignores targets, including a target of CPU1. */
    s.smp.cpus = 1; current_cpu = &cpus[0];
    aic_write(&s, 0x4004, 1 << 10, 4);
    assert(cpus[0].lines[0]);
    assert(aic_read(&s, 0x2004, 4) == 0x1002a);
    return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix='a7-smp-helpers-') as tmp:
            root = Path(tmp)
            cfile = root / 'helpers.c'
            cfile.write_text(harness + helpers + checks)
            subprocess.run([compiler, '-std=c11', '-Wall', '-Werror',
                            str(cfile), '-o', str(root / 'helpers')], check=True)
            subprocess.run([str(root / 'helpers')], check=True)
