"""Host-only checks of the board's real EHCI construction and IRQ routing.

QEMU's EHCI engine is not executed here; backend/ehci_contract_test.py is the
maintainer's synthetic MMIO test after building the backend.
"""
from pathlib import Path
import json
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class EHCIModelTests(unittest.TestCase):
    def test_native_ranges_topology_and_irq_isolation(self):
        cc = shutil.which('cc')
        if not cc:
            self.skipTest('host C compiler unavailable')
        source = (ROOT / 'backend/s5l8960x.c').read_text()
        glue = source[source.index('static void a7_ehci_class_init('):
                      source.index('static void a7_init(')]
        # Capture the actual class registration, too: it supplies port counts.
        register = source[source.index('    static const unsigned ports[] = { 3, 1 };'): 
                          source.index('    type_register_static(&a7_touch_display_type);')]
        c = r'''
#include <assert.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#define ARRAY_SIZE(x) (sizeof(x)/sizeof((x)[0]))
typedef struct { unsigned capsbase, opregbase, portscbase, portnr; } ObjectClass;
typedef ObjectClass SysBusEHCIClass;
#define SYS_BUS_EHCI_CLASS(x) (x)
#define TYPE_SYS_BUS_EHCI "sysbus-ehci-usb"
typedef struct { uint64_t size, base, offset; void *target; } MemoryRegion;
typedef struct { MemoryRegion *root; } AddressSpace;
#define GiB UINT64_C(0x40000000)
#define OBJECT(x) (x)
typedef struct {
 uint32_t device_irq[32]; unsigned updates;
 MemoryRegion usb_dma_memory, usb_dma_ram, *ram;
 AddressSpace usb_dma_as;
} A7State;
#define MACHINE(x) (x)
static void memory_region_init(MemoryRegion *r, void *o, const char *n, uint64_t size) {
 assert(o && !strcmp(n,"a7.usb-dma")); r->size=size;
}
static void memory_region_init_alias(MemoryRegion *r, void *o, const char *n,
                                     MemoryRegion *target, uint64_t offset, uint64_t size) {
 assert(o && !strcmp(n,"a7.usb-dram")); r->target=target; r->offset=offset; r->size=size;
}
static void memory_region_add_subregion(MemoryRegion *root, uint64_t base, MemoryRegion *r) {
 assert(base+r->size<=root->size); r->base=base; root->target=r;
}
static void address_space_init(AddressSpace *as, MemoryRegion *r, const char *n) {
 assert(!strcmp(n,"a7.usb-dma")); as->root=r;
}
static void aic_update(A7State *s) { s->updates++; }
typedef struct { const char *name, *parent; void (*class_init)(ObjectClass *, const void *); const void *class_data; } TypeInfo;
static ObjectClass classes[2];
static unsigned registered, created, mapped, connected;
static void type_register_static(const TypeInfo *t) {
 assert(!strcmp(t->parent, TYPE_SYS_BUS_EHCI));
 assert(!strcmp(t->name, registered ? "a7.ehci1" : "a7.ehci0"));
 t->class_init(&classes[registered++], t->class_data);
}
typedef struct { unsigned index; int realized; struct { AddressSpace *as; } ehci; } DeviceState;
#define SYS_BUS_EHCI(x) (x)
typedef DeviceState SysBusDevice;
#define SYS_BUS_DEVICE(x) (x)
static int error_fatal;
static DeviceState devices[2];
static DeviceState *qdev_new(const char *type) {
 assert(!strcmp(type, created ? "a7.ehci1" : "a7.ehci0"));
 devices[created].index=created; return &devices[created++];
}
static void sysbus_realize_and_unref(SysBusDevice *d, int *err) { assert(err==&error_fatal); d->realized=1; }
static void sysbus_mmio_map(SysBusDevice *d, unsigned slot, uint64_t pa) {
 assert(d->realized && slot==0 && d->index==mapped);
 assert(pa==(mapped ? UINT64_C(0x20c400000) : UINT64_C(0x20c200000))); mapped++;
}
static void a7_wifi_init(A7State *s, DeviceState *d) { assert(s && d->index==1); }
static A7State *owner;
static void (*irqfn)(void *,int,int);
static unsigned qemu_allocate_irq(void (*fn)(void *,int,int), void *s, unsigned irq) {
 assert(s==owner); irqfn=fn; return irq;
}
static void sysbus_connect_irq(SysBusDevice *d, unsigned slot, unsigned irq) {
 assert(slot==0 && d->index==connected);
 assert(irq==(connected ? 165 : 163)); connected++;
}
'''
        c += glue + '\nstatic void register_types(void) {\n' + register + '\n}\n'
        c += r'''
int main(void) {
 register_types(); assert(registered==2);
 for(unsigned i=0;i<2;i++) {
  assert(classes[i].capsbase==0 && classes[i].opregbase==0x10);
  assert(classes[i].portscbase==0x44 && classes[i].portnr==(i?1:3));
 }
 MemoryRegion ram={.size=GiB};
 A7State s={.ram=&ram}; owner=&s; a7_usb_init(&s);
 assert(s.usb_dma_as.root==&s.usb_dma_memory);
 assert(s.usb_dma_memory.size==UINT64_C(0x100000000));
 assert(s.usb_dma_memory.target==&s.usb_dma_ram);
 assert(s.usb_dma_ram.target==&ram && s.usb_dma_ram.offset==0);
 assert(s.usb_dma_ram.size==GiB && s.usb_dma_ram.base==0);
 for(unsigned i=0;i<2;i++) assert(devices[i].ehci.as==&s.usb_dma_as);
 assert(created==2 && mapped==2 && connected==2);
 s.device_irq[5]=1u<<4; /* OHCI163's adjacent IRQ164 must survive. */
 irqfn(&s,163,1); irqfn(&s,165,1);
 assert(s.device_irq[5]==((1u<<3)|(1u<<4)|(1u<<5)));
 irqfn(&s,163,0); assert(s.device_irq[5]==((1u<<4)|(1u<<5)));
 irqfn(&s,165,0); assert(s.device_irq[5]==(1u<<4));
 assert(s.updates==4); return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix='a7-ehci-host-') as tmp:
            p = Path(tmp)
            (p / 'test.c').write_text(c)
            subprocess.run([cc, '-std=c11', '-Wall', '-Wextra', '-Werror',
                            str(p / 'test.c'), '-o', str(p / 'test')], check=True,
                           capture_output=True, text=True)
            subprocess.run([str(p / 'test')], check=True)


    def test_vendor_storage_and_full_aperture_overlay(self):
        cc = shutil.which('cc')
        if not cc:
            self.skipTest('host C compiler unavailable')
        source = (ROOT / 'backend/s5l8960x.c').read_text()
        callbacks = source[source.index('static uint64_t bridge_read('):
                           source.index('static const MemoryRegionOps bridge_ops')]
        definitions = source[source.index('    } bridge_defs[NUM_BRIDGES] = {'):
                             source.index('    a7_usb_init(s);')]
        definitions = '    struct { uint64_t base; uint32_t size;\n' + definitions
        c = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uint64_t hwaddr;
#define NUM_BRIDGES 18
#define OBJECT(x) (x)
#define g_malloc0(x) calloc(1,x)
typedef struct { uint64_t size, base; int priority; } MemoryRegion;
typedef struct { uint64_t base; uint32_t size; uint8_t *regs; MemoryRegion mr; } A7Bridge;
typedef struct { A7Bridge bridges[NUM_BRIDGES]; } A7State;
static const int bridge_ops=0;
static MemoryRegion system_memory;
static MemoryRegion *get_system_memory(void) { return &system_memory; }
static void memory_region_init_io(MemoryRegion *r, void *o, const int *ops,
                                 void *opaque, const char *name, uint64_t size) {
 assert(o && ops==&bridge_ops && opaque && name); r->size=size;
}
static void memory_region_add_subregion(MemoryRegion *root, uint64_t base, MemoryRegion *r) {
 assert(root==&system_memory); r->base=base;
}
static void memory_region_add_subregion_overlap(MemoryRegion *root, uint64_t base,
                                               MemoryRegion *r, int priority) {
 memory_region_add_subregion(root,base,r); r->priority=priority;
}
'''
        c += callbacks + '\nstatic void init(A7State *s) {\n' + definitions + '\n}\n'
        c += r'''
int main(void) {
 A7State s={0}; init(&s);
 for(unsigned i=0;i<2;i++) {
  A7Bridge *b=&s.bridges[NUM_BRIDGES-2+i];
  assert(b->base==UINT64_C(0x20c200000)+i*0x200000);
  assert(b->size==0x10000 && b->mr.priority==-1 && b->mr.base==b->base);
  unsigned offsets[]={0x64,0x2c0,0xffc,0x1000,0xfffc};
  for(unsigned j=0;j<sizeof(offsets)/sizeof(*offsets);j++) {
   assert(bridge_read(b,offsets[j],4)==0);
   bridge_write(b,offsets[j],0x12345678+i,4);
   assert(bridge_read(b,offsets[j],4)==0x12345678+i);
  }
  bridge_write(b,0x2c1,0xabcd,2);
  assert(bridge_read(b,0x2c0,4)==(0x12abcd78u+i));
 }
 A7Bridge *ctl=&s.bridges[NUM_BRIDGES-3];
 assert(ctl->base==UINT64_C(0x20c000000) && ctl->size==16);
 for(unsigned i=0;i<4;i++) bridge_write(ctl,4*i,0xf+i,4);
 for(unsigned i=0;i<4;i++) assert(bridge_read(ctl,4*i,4)==0xf+i);
 for(unsigned i=0;i<NUM_BRIDGES;i++) {
  memset(s.bridges[i].regs,0,s.bridges[i].size);
  assert(bridge_read(&s.bridges[i],0,4)==0); free(s.bridges[i].regs);
 }
 return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix='a7-ehci-vendor-') as tmp:
            p = Path(tmp)
            (p / 'test.c').write_text(c)
            subprocess.run([cc, '-std=c11', '-Wall', '-Wextra', '-Werror',
                            str(p / 'test.c'), '-o', str(p / 'test')], check=True,
                           capture_output=True, text=True)
            subprocess.run([str(p / 'test')], check=True)

    def test_build_selects_ehci_engine(self):
        # Build selection matters: a registered board subtype alone is unusable.
        from unittest.mock import patch
        import sys
        sys.path.insert(0, str(ROOT / 'scripts/qemu'))
        import build_backend
        with patch.object(build_backend, 'run') as run, \
             patch.object(build_backend.subprocess, 'check_output') as output, \
             patch.object(build_backend.shutil, 'copyfile'), \
             patch.object(Path, 'exists', return_value=True), \
             patch.object(Path, 'write_bytes') as write, \
             patch.object(sys, 'argv', ['build_backend.py']):
            # Stop at Kconfig write, before configure or build.
            output.side_effect = [json.loads((ROOT / 'backend/source.lock.json').read_text())['commit'] + '\n', b'upstream meson', b'upstream kconfig']
            def stop_after_config(contents):
                if b'config S5L8960X' in contents:
                    self.assertIn(b'select USB_EHCI_SYSBUS', contents)
                    raise StopIteration
            write.side_effect = stop_after_config
            with self.assertRaises(StopIteration):
                build_backend.main()
            run.assert_not_called()
