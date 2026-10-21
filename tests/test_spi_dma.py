"""Compile the actual SPI/HBPP and SIO DMA functions with bounded RAM stubs.

This checks byte movement and mailbox messages without claiming QEMU/iOS
integration coverage. The separate AArch64 guest tests CPU/MMIO routing.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'backend/s5l8960x.c'


class SPIDMAContractTests(unittest.TestCase):
    def test_transport_and_hbpp_loader(self):
        compiler = shutil.which('cc')
        if not compiler:
            self.skipTest('host C compiler unavailable')
        source = SOURCE.read_text()

        def extract(start, end):
            return source[source.index(start):source.index(end)]

        harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define MAX(a,b) ((a) > (b) ? (a) : (b))
#define MiB (1024U * 1024U)
#define GiB (1024ULL * MiB)
#define RAM_BASE UINT64_C(0x800000000)
#define NUM_PERIPHERALS 1
#define SIO_OUTBOX_CAPACITY 64
#define LOG_UNIMP 0
#define LOG_GUEST_ERROR 0
#define qemu_log_mask(...) ((void)0)
#define g_malloc malloc
#define g_free free
#define MEMTXATTRS_UNSPECIFIED 0
#define MEMTX_OK 0
typedef uint64_t hwaddr;
typedef void QEMUTimer;
typedef struct A7State A7State;
static uint16_t lduw_le_p(const void *v) {
    const uint8_t *p=v; return p[0] | p[1]<<8;
}
static uint16_t lduw_be_p(const void *v) {
    const uint8_t *p=v; return p[1] | p[0]<<8;
}
static uint32_t ldl_le_p(const void *v) {
    const uint8_t *p=v; return p[0] | p[1]<<8 | p[2]<<16 | (uint32_t)p[3]<<24;
}
static uint64_t ldq_le_p(const void *v) {
    return ldl_le_p(v) | (uint64_t)ldl_le_p((const uint8_t *)v+4)<<32;
}
static void stw_le_p(void *v, uint16_t n) { uint8_t *p=v; p[0]=n; p[1]=n>>8; }
static void stw_be_p(void *v, uint16_t n) { uint8_t *p=v; p[0]=n>>8; p[1]=n; }
static void stl_le_p(void *v, uint32_t n) {
    uint8_t *p=v; p[0]=n; p[1]=n>>8; p[2]=n>>16; p[3]=n>>24;
}
static void stq_le_p(void *v, uint64_t n) {
    stl_le_p(v,n); stl_le_p((uint8_t *)v+4,n>>32);
}
'''
        harness += extract('#define SPI_FIFO_SIZE', 'typedef struct A7Peripheral')
        harness += r'''
typedef struct A7Peripheral {
    A7State *owner; hwaddr base; uint8_t *regs; A7SPI spi;
} A7Peripheral;
'''
        harness += extract('typedef struct SioDMA', '#define SEP_OUTBOX_CAPACITY')
        harness += r'''
struct A7State {
    uint32_t device_irq[8]; SioMailbox sio; A7Peripheral peripherals[1];
};
static void aic_update(A7State *s) { (void)s; }
static uint8_t ram[MiB];
static int address_space_memory;
static unsigned mmio_attempts;
static int address_space_read(void *as, hwaddr a, int attrs, void *b, size_t n) {
    (void)as; (void)attrs;
    if (a < RAM_BASE || a-RAM_BASE > sizeof(ram) || n > sizeof(ram)-(a-RAM_BASE)) {
        mmio_attempts++; return 1;
    }
    memcpy(b,ram+a-RAM_BASE,n); return 0;
}
static int address_space_write(void *as, hwaddr a, int attrs, const void *b, size_t n) {
    (void)as; (void)attrs;
    if (a < RAM_BASE || a-RAM_BASE > sizeof(ram) || n > sizeof(ram)-(a-RAM_BASE)) {
        mmio_attempts++; return 1;
    }
    memcpy(ram+a-RAM_BASE,b,n); return 0;
}
'''
        harness += extract('static int spi_index(', 'static int i2c_index(')
        harness += extract('static bool ans_ram_range(', 'static bool ans_file_io(')
        harness += extract('static uint64_t sio_pack_msg(', 'static void sio_update_irq(')
        harness += r'''
static void sio_outbox_push(A7State *s, uint64_t m) {
    assert(s->sio.outbox_count < SIO_OUTBOX_CAPACITY);
    s->sio.outbox_queue[s->sio.outbox_count++]=m;
}
static bool sio_execute_aes(A7State *s, uint32_t n) { (void)s; (void)n; return false; }
'''
        harness += extract('/* SmartIODMA:', 'static bool sio_execute_aes(')
        harness += extract('static void sio_handle_inbox_msg(', 'static void sep_update_irq(')
        harness += r'''
static A7State state;
static uint8_t regs[0x100];
static void inbox(unsigned ep, unsigned tag, unsigned op, unsigned param, uint32_t data) {
    sio_handle_inbox_msg(&state,sio_pack_msg(ep,tag,op,param,data));
}
static void expect(unsigned slot, unsigned ep, unsigned tag, unsigned op,
                   unsigned param, uint32_t data) {
    assert(slot < state.sio.outbox_count);
    assert(state.sio.outbox_queue[slot]==sio_pack_msg(ep,tag,op,param,data));
}
static void segment(unsigned handle, unsigned index, hwaddr address, unsigned len) {
    uint8_t *p=ram+0x1000+(handle+2+index)*12;
    stq_le_p(p,address); stl_le_p(p+8,len);
}
static void descriptor(unsigned handle, unsigned count) {
    stl_le_p(ram+0x1000+handle*12+20,count);
}
static void pio(A7Peripheral *p, const uint8_t *bytes, unsigned n, uint8_t *reply) {
    spi_write(p,4,0x4018); spi_write(p,0x34,n); spi_write(p,0x4c,n);
    spi_write(p,4,0x4038);
    for (unsigned i=0;i<n;i++) {
        spi_write(p,0x10,bytes[i]); reply[i]=spi_read(p,0x20);
    }
    spi_write(p,4,0x4018);
}
int main(void) {
    A7Peripheral *p=&state.peripherals[0]; p->owner=&state;
    p->base=UINT64_C(0x20a084000); p->regs=regs;
    uint8_t probe[16]={0x1a,0xa1}, reply[32];
    for (unsigned i=2;i<16;i+=2) { probe[i]=0x18;probe[i+1]=0xe1; }
    spi_write(p,0,0xd); pio(p,probe,16,reply);
    assert(!memcmp(reply,"\x4a\xd1\x4a\xd1",4));
    inbox(0,1,3,1,(RAM_BASE+0x1000)>>12); inbox(0,2,3,2,32);
    inbox(22,129,5,0,0); expect(2,22,129,0x65,0,0);
    inbox(23,129,5,0,0); expect(3,23,129,0x65,0,0);
    state.sio.outbox_count=0;
    for (unsigned i=0;i<0xfb58;i++) { ram[0x2000+i]=i; }
    descriptor(0,2); segment(0,0,RAM_BASE+0x2000,7);
    segment(0,1,RAM_BASE+0x2007,0xfb58-7);
    inbox(22,130,6,0,0); assert(state.sio.outbox_count==0);
    inbox(22,131,7,0,0); expect(0,22,131,0x69,130,0);
    spi_write(p,0x34,0); spi_write(p,0x4c,0xfb58); spi_write(p,4,0x4058);
    expect(1,22,130,0x68,0xc0,0xfb58);
    assert(spi_read(p,8)==0x400000 && !(state.device_irq[4]&(1<<9)));
    assert(p->spi.touch.firmware_bytes==0xfb58);
    pio(p,probe,2,reply); assert(reply[0]==0x4b && reply[1]==0xc1);
    pio(p,probe,2,reply); assert(reply[0]==0x4a && reply[1]==0xd1);
    /* Valid small framed firmware upload, then bad checksum. */
    uint8_t frame[20]={0x18,0xe1,0x30,1,0,1};
    stw_be_p(frame+10,touch_sum(frame+4,6));
    frame[12]=1;frame[13]=2;frame[14]=3;frame[15]=4;
    frame[17]=10;
    pio(p,frame,20,reply); pio(p,probe,2,reply);
    assert(reply[0]==0x4b && reply[1]==0xc1);
    frame[17]=11; pio(p,frame,20,reply); pio(p,probe,2,reply);
    assert(reply[0]==0xde && reply[1]==0xad);
    uint8_t execute[]={0x1f,1}; pio(p,execute,2,reply);
    assert(p->spi.touch.booted);
    /* Full duplex E2 with two unaligned segments on each side. */
    memset(ram+0x2000,0,16);ram[0x2000]=0xe2;
    descriptor(4,2);segment(4,0,RAM_BASE+0x2000,3);segment(4,1,RAM_BASE+0x2003,13);
    descriptor(8,2);segment(8,0,RAM_BASE+0x20000,5);segment(8,1,RAM_BASE+0x20005,11);
    state.sio.outbox_count=0;
    inbox(23,132,6,0,8);inbox(22,133,6,0,4);
    spi_write(p,0x34,16);spi_write(p,0x4c,16);spi_write(p,4,0x204058);
    expect(0,22,133,0x68,0xc0,16);expect(1,23,132,0x68,0xc0,16);
    assert(ram[0x20000]==0xe2 && ram[0x20002]==0x11 && ram[0x20004]==5);
    assert(lduw_le_p(ram+0x2000e)==touch_sum(ram+0x20000,14));
    assert(state.device_irq[4]&(1<<9));spi_write(p,8,0x40000f);
    assert(!(state.device_irq[4]&(1<<9)));spi_write(p,4,0x4018);
    /* MMIO anywhere in the list rejects all work without reaching the bus. */
    segment(4,1,p->base,13);state.sio.outbox_count=0;
    inbox(22,134,6,0,4);expect(0,22,134,0x66,0,0);assert(mmio_attempts==0);
    segment(4,1,RAM_BASE+0x2003,13);
    inbox(22,135,6,0,4);inbox(22,136,8,0,0);
    expect(1,22,136,0x65,0,0);expect(2,22,135,0x68,0xc0,0);
    /* RX-only emits dummy clocks and completion; mode may precede submit. */
    state.sio.outbox_count=0;
    spi_write(p,0x34,16);spi_write(p,0x4c,16);spi_write(p,4,0x4058);
    inbox(23,140,6,0,8);expect(0,23,140,0x68,0xc0,16);
    for (unsigned i=0;i<16;i++) { assert(ram[0x20000+i]==0xff); }
    spi_write(p,4,0x4018);
    /* Reserve space for BOTH completions before shifting any slave byte. */
    inbox(23,141,6,0,8);inbox(22,142,6,0,4);
    spi_write(p,0x34,16);spi_write(p,0x4c,16);
    state.sio.outbox_count=63;spi_write(p,4,0x4058);
    assert(p->spi.shifted==0 && state.sio.spi_dma[0].active);
    state.sio.outbox_count=0;sio_spi_dma_kick(&state);
    expect(0,22,142,0x68,0xc0,16);expect(1,23,141,0x68,0xc0,16);
    spi_write(p,4,0x4018);
    uint8_t final_execute[12]={0x1d,0x53,0,0,0,0,0,1};
    stw_be_p(final_execute+10,touch_sum(final_execute+2,8));
    p->spi.touch.booted=false;pio(p,final_execute,12,reply);
    assert(p->spi.touch.booted);
    unsigned report_len;assert(touch_report(&p->spi.touch,0xa1,&report_len));
    assert(report_len==1);
    /* Malformed table bounds/zero lengths and repeated idle stop. */
    state.sio.outbox_count=0;inbox(22,137,6,0,31);expect(0,22,137,0x66,0,0);
    segment(4,0,RAM_BASE+0x2000,0);inbox(22,138,6,0,4);expect(1,22,138,0x66,0,0);
    inbox(22,139,8,0,0);expect(2,22,139,0x65,0,0);expect(3,22,0,0x68,0x40,0);
    state.sio.outbox_count=0;
    descriptor(4,0);inbox(22,143,6,0,4);expect(0,22,143,0x66,0,0);
    descriptor(4,1);segment(4,0,UINT64_MAX-3,16);
    inbox(22,144,6,0,4);expect(1,22,144,0x66,0,0);
    /* Long receive-only dummy clocks must not be counted as an upload. */
    unsigned uploaded=p->spi.touch.firmware_bytes;
    descriptor(8,1);segment(8,0,RAM_BASE+0x20000,1024);
    inbox(23,145,6,0,8);spi_write(p,0x34,1024);spi_write(p,0x4c,1024);
    spi_write(p,4,0x4058);expect(2,23,145,0x68,0xc0,1024);
    assert(p->spi.touch.firmware_bytes==uploaded);
    assert(mmio_attempts==0);
    return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix='spi-dma-host-') as directory:
            root = Path(directory)
            (root / 'test.c').write_text(harness)
            build = subprocess.run([compiler, '-std=c11', '-Wall', '-Wextra', '-Werror',
                                    '-fsanitize=address,undefined', str(root / 'test.c'),
                                    '-o', str(root / 'test')], capture_output=True, text=True)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(root / 'test')], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
