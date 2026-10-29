"""Firmware-free scheduling tests: execute the board's actual vblank callback."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from qemu.runtime import host_launch_command


class CPUPacingTests(unittest.TestCase):
    def test_launch_policy(self):
        command = ['/path with spaces/qemu', '-S']
        with patch.dict(os.environ, {'VPHONE_TASKPOLICY': 'inherit'}):
            self.assertEqual(host_launch_command(command), command)
        with patch.dict(os.environ, {'VPHONE_TASKPOLICY': 'foreground'}):
            with patch('sys.platform', 'darwin'):
                self.assertEqual(host_launch_command(command),
                                 ['/usr/bin/taskpolicy', '-B', *command])
            with patch('sys.platform', 'linux'):
                with self.assertRaises(ValueError):
                    host_launch_command(command)
        with patch.dict(os.environ, {'VPHONE_TASKPOLICY': 'invalid'}):
            with self.assertRaises(ValueError):
                host_launch_command(command)

    @unittest.skipUnless(sys.platform == 'darwin', 'Darwin SDK required')
    def test_qos_patch_compiles_against_host_sdk(self):
        patch_text = (ROOT / 'backend/patches/0007-darwin-interactive-cpu-main-loop.patch').read_text()
        code = '#include <pthread.h>\n#include <stdio.h>\n#include <string.h>\n'
        code += 'struct Args { const char *name; };\n'
        for i, block in enumerate(patch_text.split('diff --git ')[1:]):
            added = '\n'.join(line[1:] for line in block.splitlines()
                              if line.startswith('+') and not line.startswith('+++'))
            code += f'void check{i}(struct Args *qemu_thread_args) {{\n{added}\n}}\n'
        with tempfile.TemporaryDirectory() as tmp:
            c = Path(tmp) / 'qos.c'
            c.write_text(code)
            subprocess.run(['cc', '-Werror', '-fsyntax-only', str(c)], check=True)

    def test_vnc_event_targets_matching_console(self):
        patch_text = (ROOT / 'backend/patches/0006-vnc-low-latency-refresh.patch').read_text()
        added = '\n'.join(line[1:] for line in patch_text.splitlines()
                          if line.startswith('+') and not line.startswith('+++'))
        code = r'''
#include <assert.h>
typedef int QemuConsole;
typedef struct { QemuConsole *con; } DisplayChangeListener;
typedef struct VncDisplay {
    DisplayChangeListener dcl;
    unsigned clients, calls;
    struct VncDisplay *next;
} VncDisplay;
static VncDisplay *vnc_displays;
static QemuConsole default_con, other_con;
#define QTAILQ_FOREACH(v, head, field) for ((v) = *(head); (v); (v) = (v)->field)
#define QTAILQ_EMPTY(head) (*(head) == 0)
#define container_of(p, type, field) ((type *)(p))
static QemuConsole *qemu_console_lookup_default(void) { return &default_con; }
static void vnc_refresh(DisplayChangeListener *dcl) {
    container_of(dcl, VncDisplay, dcl)->calls++;
}
'''
        code += added
        code += r'''
int main(void) {
    VncDisplay idle = { .dcl.con = &default_con };
    VncDisplay other = { .dcl.con = &other_con, .clients = 1, .next = &idle };
    VncDisplay def = { .clients = 1, .next = &other };
    VncDisplay bound = { .dcl.con = &default_con, .clients = 1, .next = &def };
    vnc_displays = &bound;
    vphone_vnc_refresh(&default_con);
    assert(bound.calls == 1 && def.calls == 1 && !other.calls && !idle.calls);
    vphone_vnc_refresh(&other_con);
    assert(other.calls == 1 && bound.calls == 1 && def.calls == 1);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            c, binary = Path(tmp) / 'vnc.c', Path(tmp) / 'vnc'
            c.write_text(code)
            subprocess.run(['cc', '-std=c11', '-Werror', str(c), '-o', str(binary)], check=True)
            subprocess.run([binary], check=True)

    def test_delayed_vblank_preserves_phase_and_swap_irq(self):
        source = (ROOT / 'backend/s5l8960x.c').read_text()
        callback = source[source.index('/* Pacing statistics never feed'):source.index('static void a7_reset(')]
        harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <inttypes.h>
#include <stdio.h>
#include <string.h>
#define CONFIG_VNC 1
#define NUM_PERIPHERALS 1
#define QEMU_CLOCK_REALTIME 1
#define LOG_UNIMP 0
#define MIN(a,b) ((a)<(b)?(a):(b))
#define MAX(a,b) ((a)>(b)?(a):(b))
typedef int QemuConsole;
typedef struct { uint64_t base; uint8_t regs[0x1100]; } A7Peripheral;
typedef struct {
    bool agx_perf_enabled, disp_swap_pending, wifi_mac_patched;
    void *wifi;
    unsigned disp_token;
    uint32_t device_irq[3], mask[3], soft[3];
    A7Peripheral peripherals[1];
    int64_t vblank_deadline_ns, pacing_report_ns;
    struct { int64_t last_ns; uint64_t bins[1024], count, max_ns; } pacing[2];
    int64_t *vblank_timer;
    QemuConsole *con;
} A7State;
static int64_t wall;
static unsigned drains, refreshes;
static int64_t qemu_clock_get_ns(int c) { return wall; }
static void agx_drain_context_ring(A7State *s) { drains++; }
static void aic_update(A7State *s) { assert(drains > 0); }
static void a7_wifi_patch_mac(A7State *s) {}
static void timer_mod(int64_t *t, int64_t n) { *t = n; }
#define qemu_log_mask(...) ((void)0)
'''
        harness += callback
        harness += r'''
void vphone_vnc_refresh(QemuConsole *con) { refreshes++; }
int main(void) {
    int64_t deadline;
    A7State s = {0};
    s.agx_perf_enabled = true;
    s.vblank_timer = &deadline;
    s.vblank_deadline_ns = 16666667;
    s.peripherals[0].base = UINT64_C(0x206200000);
    (*(uint32_t *)(s.peripherals[0].regs + 0x1028)) = 1 | (1<<8);
    s.disp_swap_pending = true;
    s.disp_token = 42;
    /* Host wakes late: each overdue tick is retained; deadline never
     * becomes callback-wall-time + period. Render/IRQ path still runs. */
    wall = 50000000;
    vblank_timer_cb(&s);
    assert(deadline == 33333334);
    assert((*(uint32_t *)(s.peripherals[0].regs + 0x102c)) == (1 | (1<<8)));
    assert((*(uint32_t *)(s.peripherals[0].regs + 0x1048)) == 42);
    assert(!s.disp_swap_pending);
    assert(s.device_irq[2] & 1);
    vblank_timer_cb(&s);
    assert(deadline == 50000001);
    wall += 20000000;
    s.disp_swap_pending = true;
    vblank_timer_cb(&s);
    assert(deadline == 66666668);
    assert(drains == 3 && refreshes == 3);
    assert(s.pacing[0].count == 2 && s.pacing[1].count == 1);
    assert(s.pacing[1].max_ns == 20000000);
    /* Overflow and nearest-rank quantiles must retain long stalls. */
    wall += 6000000000LL;
    pacing_sample(&s, 0, wall);
    pacing_report(&s, wall);
    assert(s.pacing[0].count == 0);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            c = Path(tmp) / 'pacing.c'
            binary = Path(tmp) / 'pacing'
            c.write_text(harness)
            subprocess.run(['cc', '-std=c11', '-Werror', str(c), '-o', str(binary)], check=True)
            result = subprocess.run([binary], check=True, capture_output=True, text=True)
            self.assertIn('vblank n=3', result.stderr)
            self.assertIn('6000.00/6000.00/6000.00', result.stderr)
