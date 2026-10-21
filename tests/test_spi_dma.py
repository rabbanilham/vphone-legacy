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
#include <stdio.h>
#include <math.h>
#include <inttypes.h>
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define MIN(a,b) ((a) < (b) ? (a) : (b))
#define MAX(a,b) ((a) > (b) ? (a) : (b))
#define MiB (1024U * 1024U)
#define GiB (1024ULL * MiB)
#define RAM_BASE UINT64_C(0x800000000)
#define NUM_PERIPHERALS 1
#define SIO_OUTBOX_CAPACITY 64
#define GPIO_SIZE 0x100000
#define QEMU_CLOCK_VIRTUAL 0
#define error_report(...) ((void)0)
static int64_t virtual_ns, timer_deadline;
static int64_t qemu_clock_get_ns(int clock) { (void)clock; return virtual_ns; }
static void timer_mod(void *t, int64_t n) { (void)t; timer_deadline=n; }
static void timer_del(void *t) { (void)t; timer_deadline=0; }
#define LOG_UNIMP 0
#define LOG_GUEST_ERROR 0
static void test_log(int mask, ...) { (void)mask; }
#define qemu_log_mask(...) test_log(__VA_ARGS__)
#define HWADDR_PRIx PRIx64

#define g_malloc malloc
#define g_free free
#define MEMTXATTRS_UNSPECIFIED 0
#define MEMTX_OK 0
typedef uint64_t hwaddr;
static void mmio_trace(const char *n, const char *o, hwaddr a, unsigned z, uint64_t v) {
    (void)n; (void)o; (void)a; (void)z; (void)v;
}
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
    uint8_t *gpio_regs; bool touch_irq_low; QEMUTimer *touch_timer;
    int64_t touch_start_ns, touch_duration_ns;
    bool touch_force;
    unsigned touch_step, touch_steps;
    int touch_x0, touch_y0, touch_x1, touch_y1;
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
        harness += extract('static void touch_gpio_update(', 'static const MemoryRegionOps gpio_ops')
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
#include <stdio.h>
static A7State state;
static uint8_t regs[0x100];
static void inbox(unsigned ep, unsigned tag, unsigned op, unsigned param, uint32_t data) {
    sio_handle_inbox_msg(&state,sio_pack_msg(ep,tag,op,param,data));
}
static void drain(void) {
    state.sio.outbox_count=state.sio.outbox_head=state.sio.outbox_tail=0;
}
static void expect(unsigned slot, unsigned ep, unsigned tag, unsigned op,
                   unsigned param, uint32_t data) {
    assert(slot < state.sio.outbox_count);
    if (state.sio.outbox_queue[slot]!=sio_pack_msg(ep,tag,op,param,data))
        fprintf(stderr,"expect slot %u tag %u op %x got %llx\\n",slot,tag,op,(unsigned long long)state.sio.outbox_queue[slot]);
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
    static uint8_t gpio[GPIO_SIZE]; state.gpio_regs=gpio;
    p->spi.touch.owner=&state;
    uint8_t probe[16]={0x1a,0xa1}, reply[128];
    for (unsigned i=2;i<16;i+=2) { probe[i]=0x18;probe[i+1]=0xe1; }
    spi_write(p,0,0xd); pio(p,probe,16,reply);
    assert(!memcmp(reply,"\x4a\xd1\x4a\xd1",4));
    inbox(0,1,3,1,(RAM_BASE+0x1000)>>12); inbox(0,2,3,2,32);
    inbox(22,129,5,0,0); expect(2,22,129,0x65,0,0);
    inbox(23,129,5,0,0); expect(3,23,129,0x65,0,0);
    drain();
    for (unsigned i=0;i<0xfb58;i++) { ram[0x2000+i]=i; }
    descriptor(0,2); segment(0,0,RAM_BASE+0x2000,7);
    segment(0,1,RAM_BASE+0x2007,0xfb58-7);
    inbox(22,130,6,0,0); expect(0,22,130,0x65,0,0);
    assert(state.sio.outbox_count==1);
    inbox(22,131,7,0,0); expect(1,22,131,0x69,130,0);
    spi_write(p,0x34,0); spi_write(p,0x4c,0xfb58); spi_write(p,4,0x4058);
    expect(2,22,130,0x68,0xc0,0xfb58);
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
    drain();
    inbox(23,132,6,0,8);inbox(22,133,6,0,4);
    spi_write(p,0x34,16);spi_write(p,0x4c,16);spi_write(p,4,0x204058);
    expect(0,23,132,0x65,0,0);expect(1,22,133,0x65,0,0);
    expect(2,22,133,0x68,0xc0,16);expect(3,23,132,0x68,0xc0,16);
    assert(ram[0x20000]==0xe2 && ram[0x20002]==0x11 && ram[0x20004]==5);
    assert(lduw_le_p(ram+0x2000e)==touch_sum(ram+0x20000,14));
    assert(state.device_irq[4]&(1<<9));spi_write(p,8,0x40000f);
    assert(!(state.device_irq[4]&(1<<9)));spi_write(p,4,0x4018);
    /* MMIO anywhere in the list rejects all work without reaching the bus. */
    segment(4,1,p->base,13);drain();
    inbox(22,134,6,0,4);expect(0,22,134,0x66,0,0);assert(mmio_attempts==0);
    segment(4,1,RAM_BASE+0x2003,13);
    inbox(22,135,6,0,4);inbox(22,136,8,0,0);
    expect(1,22,135,0x65,0,0);expect(2,22,136,0x65,0,0);expect(3,22,135,0x68,0xc0,0);
    /* RX-only emits dummy clocks and completion; mode may precede submit. */
    drain();
    spi_write(p,0x34,16);spi_write(p,0x4c,16);spi_write(p,4,0x4058);
    inbox(23,140,6,0,8);expect(0,23,140,0x65,0,0);expect(1,23,140,0x68,0xc0,16);
    for (unsigned i=0;i<16;i++) { assert(ram[0x20000+i]==0xff); }
    spi_write(p,4,0x4018);
    /* Reserve space for BOTH completions before shifting any slave byte. */
    inbox(23,141,6,0,8);inbox(22,142,6,0,4);
    spi_write(p,0x34,16);spi_write(p,0x4c,16);
    state.sio.outbox_count=63;spi_write(p,4,0x4058);
    assert(p->spi.shifted==0 && state.sio.spi_dma[0].active);
    drain();sio_spi_dma_kick(&state);
    expect(0,22,142,0x68,0xc0,16);expect(1,23,141,0x68,0xc0,16);
    spi_write(p,4,0x4018);
    uint8_t final_execute[12]={0x1d,0x53,0,0,0,0,0,1};
    stw_be_p(final_execute+10,touch_sum(final_execute+2,8));
    p->spi.touch.booted=false;pio(p,final_execute,12,reply);
    assert(p->spi.touch.booted);
    unsigned report_len;assert(touch_report(&p->spi.touch,0xa1,&report_len));
    assert(report_len==1);
    /* Malformed table bounds/zero lengths and repeated idle stop. */
    drain();inbox(22,137,6,0,31);expect(0,22,137,0x66,0,0);
    segment(4,0,RAM_BASE+0x2000,0);inbox(22,138,6,0,4);expect(1,22,138,0x66,0,0);
    inbox(22,139,8,0,0);expect(2,22,139,0x65,0,0);expect(3,22,0,0x68,0x40,0);
    drain();
    descriptor(4,0);inbox(22,143,6,0,4);expect(0,22,143,0x66,0,0);
    descriptor(4,1);segment(4,0,UINT64_MAX-3,16);
    inbox(22,144,6,0,4);expect(1,22,144,0x66,0,0);
    /* Long receive-only dummy clocks must not be counted as an upload. */
    unsigned uploaded=p->spi.touch.firmware_bytes;
    descriptor(8,1);segment(8,0,RAM_BASE+0x20000,1024);
    inbox(23,145,6,0,8);spi_write(p,0x34,1024);spi_write(p,0x4c,1024);
    spi_write(p,4,0x4058);expect(2,23,145,0x65,0,0);expect(3,23,145,0x68,0xc0,1024);
    assert(p->spi.touch.firmware_bytes==uploaded);
    assert(mmio_attempts==0);
    /* Final execute must not assert the line before the driver attaches
     * its GPIO handler (probe 85 livelock); the deferred-unlock 70=2
     * announces boot completion instead. It must not wait for 70=1. */
    assert(p->spi.touch.frame_count==0 && !state.touch_irq_low);
    uint8_t stop[16]={0xe4,0x70,1,2};
    stw_le_p(stop+14,touch_sum(stop,14));pio(p,stop,16,reply);
    assert(p->spi.touch.frame_count==1 && p->spi.touch.reporting==2);
    /* Clear only for the independent unlock reporting contract below. */
    p->spi.touch.frame_count=0;touch_gpio_level(&state,false);
    /* Unlock UI, then consume native EB reset-complete and contact.
     * Queries must leave the FIFO intact, including the initial duplicate. */
    setenv("VPHONE_TOUCH_SCRIPT","70,60,1018,600,1018,0.3",1);
    touch_script_reset(&state);
    assert(timer_deadline==70000000000LL && state.touch_steps==18);
    assert(state.touch_x0==60 && state.touch_y1==1018);
    setenv("VPHONE_TOUCH_SCRIPT","nan,60,1018,600,1018,0.3",1);
    touch_script_reset(&state); assert(timer_deadline==0);
    unsetenv("VPHONE_TOUCH_SCRIPT"); touch_script_reset(&state);
    uint8_t start[16]={0xe4,0x70,1,2};
    stw_le_p(start+14,touch_sum(start,14));
    /* Disabled (0xe): the falling edge is not latched, and enabling a
     * falling-edge pin while the line is already low latches nothing. */
    stl_le_p(gpio+88,0x20e);pio(p,start,16,reply);
    assert(state.touch_irq_low && p->spi.touch.frame_count==1);
    assert(!(state.device_irq[3] & (1u<<12)));
    stl_le_p(gpio+88,0x20a);touch_gpio_update(&state);
    assert(!(state.device_irq[3] & (1u<<12)));
    touch_gpio_level(&state,false);touch_gpio_level(&state,true);
    assert(state.device_irq[3] & (1u<<12));
    uint8_t query[16]={0xeb,1};
    pio(p,query,16,reply); assert(lduw_le_p(reply+1)==4);
    assert(lduw_le_p(reply+14)==touch_sum(reply,14));
    query[1]=2;pio(p,query,16,reply);assert(lduw_le_p(reply+1)==4);
    gpio_write(&state,0x800,1u<<22,4);
    /* Edge latch: W1C clears even while the line stays low (the GPIOIC
     * rescan at ec9afc would otherwise never terminate). */
    assert(gpio_read(&state,0x800,4)==0 && state.touch_irq_low);
    assert(!(state.device_irq[3] & (1u<<12)));
    uint8_t fetch[64]={0xeb,2,1};
    stw_le_p(fetch+7,touch_sum(fetch,3));
    pio(p,fetch,9,reply);assert(!memcmp(reply+5,"\x60\x02",2));
    assert(!(touch_sum(reply,5)&255));
    assert(lduw_le_p(reply+7)==0x62 && !state.touch_irq_low);
    /* After consumption the line is released; nothing re-latches. */
    gpio_write(&state,0x800,1u<<22,4);
    assert(gpio_read(&state,88,4)==0x20b);
    assert(!(state.device_irq[3] & (1u<<12)));
    uint8_t enable[16]={0xe4,0xbf,4,0x9d,1,9,0};
    stw_le_p(enable+14,touch_sum(enable,14));pio(p,enable,16,reply);
    virtual_ns=70000000000LL;
    state.touch_steps=18;state.touch_duration_ns=300000000;
    state.touch_x0=60;state.touch_y0=1018;state.touch_x1=600;state.touch_y1=1018;
    touch_timer_cb(&state);
    assert(timer_deadline==virtual_ns+300000000/18);
    pio(p,query,16,reply);assert(lduw_le_p(reply+1)==58);
    /* Short/malformed data read cannot consume the queued down. */
    pio(p,fetch,8,reply);assert(p->spi.touch.frame_count==1);
    memset(fetch+3,0,sizeof(fetch)-3);
    stw_le_p(fetch+61,touch_sum(fetch,3));
    pio(p,fetch,63,reply);
    assert(!(touch_sum(reply,5)&255));
    assert(lduw_le_p(reply+61)==touch_sum(reply+5,56));
    uint8_t *f=reply+5;
    assert(f[0]==0x74 && f[2]==26 && f[14]==1 && lduw_le_p(f+12)==30);
    assert(f[26]==1 && f[27]==1 && lduw_le_p(f+30)==60 && lduw_le_p(f+32)==118);
    assert(lduw_le_p(f+44)==256 && !state.touch_irq_low);
    for (unsigned i=1;i<=20;i++) {
        virtual_ns=timer_deadline;touch_timer_cb(&state);
        pio(p,query,16,reply);pio(p,fetch,63,reply);
        assert(reply[32]==(i==20?7:i==1?3:4));
        assert(lduw_le_p(reply+35)==(i>=19?600:60+30*(i-1)));
        assert(lduw_le_p(reply+49)==(i==20?0:256));
    }
    assert(p->spi.touch.frame_count==0 && !state.touch_irq_low);
    /* Reproduce the probe84 stop -> BF metadata/read/modify/write path. */
    pio(p,stop,16,reply);
    stw_le_p(fetch+7,touch_sum(fetch,3));pio(p,fetch,9,reply); /* drain fresh unlock notification */
    assert(!touch_reporting_enabled(&p->spi.touch));
    unsigned before=state.touch_step;
    touch_timer_cb(&state);assert(state.touch_step==before);
    uint8_t bfmeta[16]={0xe3,0xbf};
    pio(p,bfmeta,16,reply);
    assert(reply[2]==0 && lduw_le_p(reply+3)==4);
    uint8_t bfget[16]={0xe6,0xbf,1,4};
    pio(p,bfget,16,reply);assert(ldl_le_p(reply+3)==0);
    uint8_t bfset[16]={0xe4,0xbf,4,0x9d,1,9,0};
    stw_le_p(bfset+14,touch_sum(bfset,14));
    pio(p,bfset,16,reply);assert(!p->spi.touch.ack_status);
    assert(touch_reporting_enabled(&p->spi.touch));
    assert(p->spi.touch.reporting==2); /* BF is sufficient */
    pio(p,bfget,16,reply);assert(ldl_le_p(reply+3)==0x0009019d);
    /* Wrong size/checksum must preserve control and return E1 failure. */
    bfset[2]=3;stw_le_p(bfset+14,touch_sum(bfset,14));
    pio(p,bfset,16,reply);assert(p->spi.touch.ack_status);
    assert(ldl_le_p(p->spi.touch.input_control)==0x0009019d);
    bfset[2]=4;bfset[14]^=1;
    pio(p,bfset,16,reply);assert(p->spi.touch.ack_status);
    uint8_t ack[16]={0xe1};pio(p,ack,16,reply);assert(reply[8]==1);
    for (unsigned i=0;i<2;i++) {
        uint8_t control[16]={0xe4,i?0xc8:0xaf,1,i?9:1};
        stw_le_p(control+14,touch_sum(control,14));pio(p,control,16,reply);
        pio(p,ack,16,reply);assert(reply[8]==0);
        uint8_t get[16]={0xe6,control[1],1,1};pio(p,get,16,reply);
        assert(reply[3]==control[3] && lduw_le_p(reply+14)==touch_sum(reply,14));
    }
    /* UI lock must suppress even a retained/rewritten BF finger mask. */
    uint8_t lock[16]={0xe4,0x70,1,1};
    stw_le_p(lock+14,touch_sum(lock,14));pio(p,lock,16,reply);
    bfset[2]=4;stw_le_p(bfset+14,touch_sum(bfset,14));pio(p,bfset,16,reply);
    assert(!touch_reporting_enabled(&p->spi.touch));
    const uint8_t *regions=touch_report(&p->spi.touch,0xd0,&report_len);
    assert(report_len==1 && regions[0]==0); /* valid empty region list */
    /* Opt-in force bypasses policy only; never rewrites report70 or BF. */
    setenv("VPHONE_TOUCH_FORCE","true",1);touch_script_reset(&state);
    assert(!state.touch_force);
    setenv("VPHONE_TOUCH_FORCE","1",1);touch_script_reset(&state);
    assert(state.touch_force);
    pio(p,stop,16,reply);stw_le_p(fetch+7,touch_sum(fetch,3));pio(p,fetch,9,reply);state.touch_step=0;
    touch_timer_cb(&state);assert(p->spi.touch.frame_count==1);
    assert(p->spi.touch.reporting==2 && ldl_le_p(p->spi.touch.input_control)==0);
    pio(p,fetch,63,reply);assert(p->spi.touch.frame_count==0);
    unsetenv("VPHONE_TOUCH_FORCE");touch_script_reset(&state);
    assert(!state.touch_force);
    /* All seven status words in a group feed the same upstream AIC IRQ. */
    stl_le_p(gpio+0x800,0);stl_le_p(gpio+0x800+24,1);
    touch_gpio_update(&state);assert(state.device_irq[3]&(1u<<12));
    stl_le_p(gpio+0x800+24,0);touch_gpio_update(&state);
    touch_gpio_level(&state,false);
    stl_le_p(gpio+88,0x20e);touch_gpio_level(&state,true);
    assert(!(state.device_irq[3]&(1u<<12)));
    stl_le_p(gpio+88,0x20a);touch_gpio_level(&state,false);
    touch_gpio_level(&state,true);assert(state.device_irq[3]&(1u<<12));
    /* Level-low type 6 (flags bit0) latches whenever the line is low. */
    stl_le_p(gpio+0x800,0);stl_le_p(gpio+88,0x206);touch_gpio_update(&state);
    assert(gpio_read(&state,0x800,4)==(1u<<22));
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
