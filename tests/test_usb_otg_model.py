"""Host-compiled actual board callbacks; never builds or executes QEMU."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from usb_otg_contract_test import transactions, fixture, BASE


class USBOTGModelTests(unittest.TestCase):
    def test_idle_core_native_poll_contract(self):
        cc = shutil.which('cc')
        if not cc:
            self.skipTest('host C compiler unavailable')
        source = (ROOT / 'backend/s5l8960x.c').read_text()
        callbacks = source[source.index('static void a7_otg_reset('):
                           source.index('static const MemoryRegionOps a7_otg_ops')]
        c = r'''
#include <stdint.h>
#include <string.h>
#include <assert.h>
typedef uint64_t hwaddr;
#define MIN(a,b) ((a)<(b)?(a):(b))
typedef struct { uint32_t usb_regs[0x10000/4]; int level; } A7State;
'''
        c += callbacks
        c += '\nstatic void a7_usb_irq(void *opaque, int irq, int level) {\n'
        c += ' assert(irq==162); ((A7State *)opaque)->level=!!level; }\n'
        c += 'int main(void) { A7State state={0}; A7State *s=&state; a7_otg_reset(s);\n'
        for op, addr, value in transactions():
            offset = addr - BASE
            if op == 'w':
                c += f'a7_otg_write(s,{offset},UINT32_C({value}),4);\n'
            else:
                c += f'assert(a7_otg_read(s,{offset},4)==UINT32_C({value}));\n'
        c += r'''
 /* Partial access lanes, pulse writes and reset reproducibility. */
 a7_otg_write(s,0xfffc,0x12345678,4);
 a7_otg_write(s,0xfffd,0xabcd,2);
 assert(a7_otg_read(s,0xfffc,4)==0x12abcd78);
 assert(a7_otg_read(s,0xfffd,2)==0xabcd);
 a7_otg_write(s,0x804,0x80,1);
 assert(a7_otg_read(s,0x14,4)&0x40);
 a7_otg_write(s,0x805,1,1);
 assert(!(a7_otg_read(s,0x14,4)&0x40));
 /* Interrupt is masked at both levels and never simulates a cable. */
 a7_otg_write(s,8,1,4); a7_otg_write(s,0x18,0x80,4);
 assert(!s->level);
 a7_otg_write(s,0x804,0x202,4); assert(s->level);
 a7_otg_write(s,0x804,0x402,4); assert(!s->level);
 a7_otg_write(s,0x810,2,4); a7_otg_write(s,0x81c,1,4);
 a7_otg_write(s,0x18,1<<18,4); a7_otg_write(s,0x900,0x48000000,4);
 assert(s->level); a7_otg_write(s,0x908,2,1); assert(!s->level);
 a7_otg_reset(s); assert(a7_otg_read(s,0xfffc,4)==0);
 return 0; }
'''
        with tempfile.TemporaryDirectory(prefix='a7-otg-host-') as tmp:
            path = Path(tmp)
            (path / 'test.c').write_text(c)
            result = subprocess.run([cc, '-std=c11', '-Wall', '-Wextra', '-Werror',
                                    '-fsanitize=undefined,address', str(path / 'test.c'), '-o', str(path / 'test')],
                                   capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(path / 'test')], check=True)

    def test_synthetic_payload_fits_and_has_completion(self):
        payload, expected = fixture()
        self.assertLess(len(payload), 0x2000)
        self.assertEqual(expected[-1], 0xface)

    def test_exact_dt_resources_and_reset_wiring(self):
        source = (ROOT / 'backend/s5l8960x.c').read_text()
        for base, size in [('20c000010', '10'), ('20c000050', '10'),
                           ('20c000060', '10'), ('20c300000', '10000')]:
            self.assertIn(f'{{ UINT64_C(0x{base}), 0x{size} }}', source)
        self.assertIn('"a7.usb-device", 0x10000', source)
        self.assertIn('qdev_prop_set_uint32(ohci, "num-ports", 1)', source)
        self.assertIn('SYSBUS_OHCI(ohci)->ohci.as = &s->usb_dma_as', source)
        self.assertIn('qemu_allocate_irq(a7_usb_irq, s, 164)', source)
        self.assertIn('memset(s->usb_aux[i].regs, 0, s->usb_aux[i].size)', source)
        self.assertIn('/* The TA command whose parameter buffer', source)
        self.assertEqual(source.count('MemoryRegion usb_otg;'), 1)
        self.assertNotIn('A7Bridge usb_otg,', source)
        self.assertEqual(source.count('UINT64_C(0x20c100000)'), 1)
        self.assertIn('select USB_OHCI_SYSBUS', (ROOT / 'scripts/qemu/build_backend.py').read_text())

    def test_detached_vbus_through_pmu_i2c(self):
        from pmu_contract_test import host_check
        host_check(r"""
 assert(d2045_adc_sample(9)==0);
 store(&p,0x500,0x89); assert(timer.pending);
 now=timer.deadline; d2045_adc_done(&s);
 assert(!(s.pmu.regs[0x500]&0x80) && (s.pmu.regs[0x5b]&2));
 read_regs(&p,0x502,2); assert(!p.pmu_rx[0]&&!p.pmu_rx[1]);
 read_regs(&p,0x50,12); assert(p.pmu_rx[11]==2);
 store(&p,0x541,0x89); read_regs(&p,0x541,3);
 assert(!(p.pmu_rx[0]&0x80)&&!p.pmu_rx[1]&&!p.pmu_rx[2]);
 read_regs(&p,0x60,12);
 for(unsigned i=0;i<12;i++) assert(!p.pmu_rx[i]);
""")

    def test_compiled_board_resource_construction(self):
        cc = shutil.which('cc')
        if not cc:
            self.skipTest('host C compiler unavailable')
        source = (ROOT / 'backend/s5l8960x.c').read_text()
        start = source.index('    /* All remaining USB reg resources;')
        end = source.index('    static const struct {\n        const char *name;', start)
        construction = source[start:end]
        c = r'''
#include <assert.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#define ARRAY_SIZE(x) (sizeof(x)/sizeof(*(x)))
#define OBJECT(x) (x)
#define g_malloc0(n) calloc(1,n)
typedef uint64_t hwaddr;
typedef struct {uint64_t base,size;int priority;} MemoryRegion;
typedef struct {hwaddr base;uint32_t size;uint8_t *regs;MemoryRegion mr;} A7Bridge;
typedef struct {MemoryRegion usb_otg;A7Bridge usb_aux[4];int usb_dma_as;} A7State;
static MemoryRegion system_memory;
static int a7_otg_ops,bridge_ops,error_fatal;
static MemoryRegion *get_system_memory(void) {return &system_memory;}
static void memory_region_init_io(MemoryRegion *r,void *obj,int *ops,void *p,const char *n,uint64_t size) {
 assert(obj&&ops&&p&&n);r->size=size;
}
static void memory_region_add_subregion(MemoryRegion *root,uint64_t pa,MemoryRegion *r) {assert(root==&system_memory);r->base=pa;}
static void memory_region_add_subregion_overlap(MemoryRegion *root,uint64_t pa,MemoryRegion *r,int pri) {
 memory_region_add_subregion(root,pa,r);r->priority=pri;
}
typedef struct {struct {int *as;} ohci;unsigned ports,irq;uint64_t base;} DeviceState;
static DeviceState device;
#define SYS_BUS_DEVICE(x) (x)
#define SYSBUS_OHCI(x) (x)
static DeviceState *qdev_new(const char *n) {assert(!strcmp(n,"sysbus-ohci"));return &device;}
static void qdev_prop_set_uint32(DeviceState *d,const char *n,unsigned v) {assert(!strcmp(n,"num-ports"));d->ports=v;}
static void sysbus_realize_and_unref(DeviceState *d,int *e) {assert(d==&device&&e==&error_fatal);}
static void sysbus_mmio_map(DeviceState *d,unsigned n,uint64_t pa) {assert(!n);d->base=pa;}
static void a7_usb_irq(void *s,int irq,int level) {(void)s;(void)irq;(void)level;}
static unsigned qemu_allocate_irq(void (*f)(void *,int,int),void *s,unsigned irq) {assert(f==a7_usb_irq&&s);return irq;}
static void sysbus_connect_irq(DeviceState *d,unsigned n,unsigned irq) {assert(!n);d->irq=irq;}
'''
        c += 'static void init(A7State *s) {\n' + construction + '\n}\n'
        c += r'''
int main(void) {
 A7State s={0};init(&s);
 assert(s.usb_otg.base==UINT64_C(0x20c100000)&&s.usb_otg.size==0x10000);
 uint64_t bases[]={0x20c000010,0x20c000050,0x20c000060,0x20c300000};
 for(unsigned i=0;i<4;i++) {
  A7Bridge *b=&s.usb_aux[i];
  assert(b->base==bases[i]&&b->mr.base==bases[i]);
  assert(b->size==(i==3?0x10000:0x10)&&b->mr.size==b->size);
  assert(b->mr.priority==-1);free(b->regs);
 }
 assert(device.ports==1&&device.irq==164&&device.base==UINT64_C(0x20c300000));
 assert(device.ohci.as==&s.usb_dma_as);return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix='a7-usb-wiring-') as tmp:
            path = Path(tmp)
            (path / 'test.c').write_text(c)
            result = subprocess.run([cc, '-std=c11', '-Wall', '-Wextra', '-Werror',
                                     str(path / 'test.c'), '-o', str(path / 'test')],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(path / 'test')], check=True)
