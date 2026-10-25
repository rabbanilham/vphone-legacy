#!/usr/bin/env python3
"""D2045 native I2C fixture; --host checks extracted C without running QEMU."""
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import i2c_contract_test as harness
from gpio_contract_test import move64

EPOCH = 1700000000


def fixture():
    code = move64(4, 0x800002000)
    expected = []
    bus = 0x20a110000

    def write(a, v):
        code.extend(move64(2, a) + move64(0, v) + [0xb9000040])

    def read(a, v):
        code.extend(move64(2, a) + [0xb9400043,
                    0xb9000003 | len(expected) << 10 | 4 << 5])
        expected.append(v)

    def prefix(reg):
        write(bus + 0x14, 0xffffffff)
        write(bus, 0x1e8)
        write(bus, reg >> 8)
        write(bus, 0x200 | (reg & 255))
        read(bus + 0x14, 0x08000000)

    def receive(reg, values):
        prefix(reg)
        write(bus + 0x14, 0xffffffff)
        write(bus, 0x1e9)
        write(bus, 0x600 | len(values))
        read(bus + 0x14, 0x00100000)
        for n, value in enumerate(values):
            read(bus + 8, (len(values) - n) << 8)
            read(bus + 4, value)
        read(bus + 4, 0x100)

    receive(0x5c8, list(struct.pack('<I', EPOCH)))
    write(bus, 0x1e8)
    write(bus, 0x40)
    write(bus, 4)
    for value in [0x78, 0x56, 0x34, 0x212]:
        write(bus, value)
    receive(0x4004, [0x78, 0x56, 0x34, 0x12])
    receive(0x50, [0] * 12)
    # Reset controller, preserving external PMU storage.
    write(bus + 0x10, 0x80000000)
    receive(0x4004, [0x78, 0x56, 0x34, 0x12])
    write(bus + 0x1014, 0xffffffff)
    write(bus + 0x1000, 0x1e8)
    read(bus + 0x1014, 0x08200000)  # PMU only exists on I2C0.
    write(0x800002000 + 4 * len(expected), 0xface)
    expected.append(0xface)
    code.append(0x14000000)
    return struct.pack('<' + 'I' * len(code), *code), expected


def host_check(extra_checks=""):
    """Compile real device/controller functions against bounded clock/IRQ stubs."""
    compiler = shutil.which('cc')
    if not compiler:
        raise RuntimeError('host C compiler unavailable')
    source = Path(__file__).with_name('s5l8960x.c').read_text()
    device = source[source.index('typedef struct A7D2045 {'):
                    source.index('typedef struct A7Peripheral {')]
    functions = source[source.index('/* Recovered ABI: 10513ec'):
                       source.index('static void ans_update_irq(')]
    prelude = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <time.h>
#include <errno.h>
#define QEMU_CLOCK_VIRTUAL 0
#define LOG_UNIMP 0
#define qemu_log_mask(...) ((void)0)
#define error_report(...) ((void)0)
typedef uint64_t hwaddr;
typedef struct { bool pending; int64_t deadline; } QEMUTimer;
static int64_t now;
static int64_t qemu_clock_get_ns(int clock) { (void)clock; return now; }
static void timer_del(QEMUTimer *t) { t->pending = false; }
static void timer_mod(QEMUTimer *t, int64_t n) { t->pending = true; t->deadline = n; }
static uint32_t ldl_le_p(const void *p) {
 const uint8_t *b=p; return b[0] | (uint32_t)b[1]<<8 | (uint32_t)b[2]<<16 | (uint32_t)b[3]<<24;
}
static void stl_le_p(void *p, uint32_t v) {
 uint8_t *b=p; for (unsigned i=0;i<4;i++) b[i]=v>>(i*8);
}
'''
    types = r'''
typedef struct { void *wifi; A7D2045 pmu; uint8_t gpio_regs[0x1000];
 uint32_t device_irq[32]; uint8_t chestnut_control; } A7State;
typedef struct { A7State *owner; hwaddr base; uint8_t regs[0x1000];
 bool i2c_active, i2c_read; uint8_t i2c_address;
 uint8_t chestnut_pointer, i2c_rx_byte;
 bool chestnut_prefix, chestnut_pointer_valid, i2c_rx_valid;
 uint16_t pmu_pointer; unsigned pmu_prefix_count, pmu_rx_head, pmu_rx_count;
 bool pmu_pointer_valid; uint8_t pmu_rx[255]; A7RegisterI2C tristar, lm3534; } A7Peripheral;
static void aic_update(A7State *s) { (void)s; }
'''
    # Also use the real GPIO group aggregation/level assertion helper.
    gpio = source[source.index('static void touch_gpio_update('):
                  source.index('static void touch_gpio_level(')]
    types = types.replace('uint8_t chestnut_control;',
                          'uint8_t chestnut_control; bool touch_irq_low, home_pressed;')
    main = r'''
static void send(A7Peripheral *p, uint32_t v) { i2c_write(p, 0, v); }
static void prefix(A7Peripheral *p, unsigned reg) {
 i2c_write(p,0x14,~0u); send(p,0x1e8); send(p,reg>>8); send(p,0x200|(reg&255));
 assert(ldl_le_p(p->regs+0x14)==0x08000000);
}
static void read_regs(A7Peripheral *p,unsigned reg,unsigned count) {
 prefix(p,reg); i2c_write(p,0x14,~0u); send(p,0x1e9); send(p,0x600|count);
 assert(ldl_le_p(p->regs+0x14)==0x00100000); assert(p->pmu_rx_count==count);
}
static void store(A7Peripheral *p,unsigned reg,uint8_t byte) {
 send(p,0x1e8);send(p,reg>>8);send(p,reg&255);send(p,0x200|byte);
}
int main(void) {
 A7State s={0}; QEMUTimer timer={0}; s.pmu.adc_timer=&timer;
 A7Peripheral p={.owner=&s,.base=0x20a110000ULL};
 d2045_reset(&s); assert(d2045_seconds(&s.pmu)==1700000000);
 read_regs(&p,0x5c6,6); assert(ldl_le_p(p.pmu_rx+2)==1700000000);
 now=2500000000LL; assert(d2045_seconds(&s.pmu)==1700000002);
 /* Previous burst is a coherent snapshot, even after virtual clock advances. */
 assert(ldl_le_p(p.pmu_rx+2)==1700000000);
 read_regs(&p,0x5c6,6); assert(ldl_le_p(p.pmu_rx+2)==1700000002);
 assert(p.pmu_rx[0]==0 && p.pmu_rx[1]==0x80);
 send(&p,0x1e8);send(&p,0x40);send(&p,4);
 send(&p,0x78);send(&p,0x56);send(&p,0x34);send(&p,0x212);
 read_regs(&p,0x4004,4);assert(ldl_le_p(p.pmu_rx)==0x12345678);
 for(unsigned i=0;i<4;i++) {
  assert(i2c_read(&p,8)==(4-i)*256);
  assert(i2c_read(&p,4)==((0x12345678u>>(i*8))&255));
 }
 assert(i2c_read(&p,4)==0x100 && i2c_read(&p,8)==0);
 i2c_write(&p,0x10,0x80000000);assert(!p.pmu_rx_count && !p.pmu_pointer_valid);
 read_regs(&p,0x4004,4);assert(ldl_le_p(p.pmu_rx)==0x12345678);
 /* Actual native RTC set: requested - counter, then get counter + offset. */
 uint32_t offset=1800000000u-d2045_seconds(&s.pmu);
 for(unsigned i=0;i<4;i++) store(&p,0x4004+i,offset>>(i*8));
 assert(d2045_seconds(&s.pmu)+ldl_le_p(s.pmu.regs+0x4004)==1800000000u);
 /* Direct seconds writes, natural uint32 wrap, and persistent offset storage. */
 now=3000000000LL;
 for(unsigned i=0;i<4;i++) store(&p,0x5c8+i,0xff);
 assert(d2045_seconds(&s.pmu)==UINT32_MAX);
 now+=1000000000; assert(d2045_seconds(&s.pmu)==0);
 /* Native GPIO15 level-low interrupt, group 2, ADC event 91 unmasked. */
 stl_le_p(s.gpio_regs+15*4,0x20006);
 store(&p,0x500,0x84);assert(timer.pending);
 now=timer.deadline;d2045_adc_done(&s);timer.pending=false;
 assert(s.pmu.regs[0x5b]==2 && !s.pmu.irq_low); /* masked pending event */
 store(&p,0x7b,0xfd); /* unmask pending conversion */
 assert(s.pmu.irq_low);assert(s.device_irq[110/32]&(1u<<(110%32)));
 read_regs(&p,0x502,2);assert(((unsigned)p.pmu_rx[1]<<4|p.pmu_rx[0])==2130);
 read_regs(&p,0x50,12);assert(p.pmu_rx[11]==2 && !s.pmu.irq_low);
 stl_le_p(s.gpio_regs+0x880,0);touch_gpio_update(&s);
 assert(!(s.device_irq[110/32]&(1u<<(110%32))));
 read_regs(&p,0x50,12);assert(!p.pmu_rx[11]);
 store(&p,0x500,0x84);assert(timer.pending);
 store(&p,0x500,4);assert(!timer.pending); /* cancel conversion */
 store(&p,0x541,0x88);read_regs(&p,0x541,3);
 assert(!(p.pmu_rx[0]&0x80));assert(((unsigned)p.pmu_rx[2]<<4|p.pmu_rx[1])==1061);
 /* Auto increment across low-byte and full 16-bit address boundaries. */
 store(&p,0xffff,0xab);store(&p,0,0xcd);read_regs(&p,0xffff,2);
 assert(p.pmu_rx[0]==0xab && p.pmu_rx[1]==0xcd);
 read_regs(&p,0x100,255);assert(p.pmu_rx_count==255);
 send(&p,0x1e9);send(&p,0x600);assert(!p.pmu_rx_count);
 assert(ldl_le_p(p.regs+0x14)&0x00200000);
 i2c_write(&p,0x10,0x80000000);send(&p,0x1e8);send(&p,0x205);
 assert(ldl_le_p(p.regs+0x14)==0x08200000); /* truncated prefix */
 p.base+=0x1000;i2c_write(&p,0x14,~0u);send(&p,0x1e8);
 assert(ldl_le_p(p.regs+0x14)==0x08200000);
 p.base-=0x1000;i2c_write(&p,0x14,~0u);send(&p,0x14e);send(&p,5);send(&p,0x20f);
 assert(s.chestnut_control==15); /* existing slave still routes separately */
 d2045_reset(&s);assert(d2045_seconds(&s.pmu)==1700000000);
 assert(ldl_le_p(s.pmu.regs+0x4004)==0);
 return 0;
}
'''
    main = main.replace(' return 0;\n}', extra_checks + '\n return 0;\n}')
    with tempfile.TemporaryDirectory(prefix='d2045-host-') as temp:
        root = Path(temp)
        (root / 'test.c').write_text(prelude + device + types + gpio + functions + main)
        subprocess.run([compiler, '-std=c11', '-Wall', '-Wextra', '-Werror',
                        str(root / 'test.c'), '-o', str(root / 'test')],
                       check=True, capture_output=True, text=True)
        env = os.environ.copy()
        env['VPHONE_RTC_EPOCH'] = str(EPOCH)
        subprocess.run([str(root / 'test')], check=True, env=env)


if __name__ == '__main__':
    if '--host' in sys.argv:
        host_check()
        print('PASS: extracted D2045/controller C, virtual RTC, set/offset, ADC IRQ and bus isolation')
    else:
        os.environ['VPHONE_RTC_EPOCH'] = str(EPOCH)
        harness.fixture = fixture
        harness.main()
