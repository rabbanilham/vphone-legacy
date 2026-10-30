"""Exercise the actual ANS C handler with real files and instrumented syscalls.

No VM is booted. QEMU RAM, clocks and notifiers are bounded host stubs.
"""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'backend/s5l8960x.c'


class ANSStorageTests(unittest.TestCase):
    def test_writeback_batching_and_failures(self):
        cc = shutil.which('cc')
        if not cc:
            self.skipTest('host C compiler unavailable')
        source = SOURCE.read_text()

        def extract(a, b):
            return source[source.index(a):source.index(b)]

        harness = r'''
#define _POSIX_C_SOURCE 200809L
#include <assert.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <inttypes.h>
#include <errno.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/stat.h>
typedef pthread_mutex_t QemuMutex;
typedef pthread_cond_t QemuCond;
typedef pthread_t QemuThread;
#define QEMU_THREAD_JOINABLE 0
#define qemu_mutex_init(m) pthread_mutex_init(m,NULL)
#define qemu_mutex_lock pthread_mutex_lock
#define qemu_mutex_unlock pthread_mutex_unlock
#define qemu_mutex_destroy pthread_mutex_destroy
#define qemu_cond_init(c) pthread_cond_init(c,NULL)
#define qemu_cond_wait pthread_cond_wait
#define qemu_cond_signal pthread_cond_signal
#define qemu_cond_broadcast pthread_cond_broadcast
#define qemu_cond_destroy pthread_cond_destroy
#define qemu_thread_create(t,n,f,o,j) pthread_create(t,NULL,f,o)
#define qemu_thread_join(t) pthread_join(*(t),NULL)
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define MIN(a,b) ((a)<(b)?(a):(b))
#define RAM_BASE UINT64_C(0x800000000)
#define GiB (1024ULL*1024*1024)
#define ANS_SYS_START 8
#define ANS_SECTOR_SIZE 4096
#define QEMU_CLOCK_REALTIME 0
#define QEMU_CLOCK_VIRTUAL 1
#define NANOSECONDS_PER_SECOND 1000000000LL
#define MEMTXATTRS_UNSPECIFIED 0
#define MEMTX_OK 0
#define LOG_UNIMP 0
#define LOG_GUEST_ERROR 1
#define HWADDR_PRIx PRIx64
#define qemu_log_mask(...) ((void)0)
#define g_malloc malloc
#define g_free free
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
static unsigned errors;
#define error_report(...) ((void)++errors)
typedef uint64_t hwaddr;
typedef struct { int64_t deadline; bool pending; } QEMUTimer;
typedef struct { void (*notify)(void *, void *); } Notifier;
static int64_t host_ms=1000;
static int64_t qemu_clock_get_ms(int c) { (void)c; return host_ms; }
static int64_t qemu_clock_get_ns(int c) { (void)c; return host_ms*1000000; }
static void timer_mod(QEMUTimer *t, int64_t n) { t->pending=true;t->deadline=n; }
static void timer_del(QEMUTimer *t) { t->pending=false; }
static uint8_t ram[16*1024*1024];
static int address_space_memory;
static int address_space_read(void *as, hwaddr a, int attrs, void *b, size_t n) {
    (void)as;(void)attrs;
    if(a<RAM_BASE || a-RAM_BASE>sizeof(ram) || n>sizeof(ram)-(a-RAM_BASE)) return 1;
    memcpy(b,ram+(a-RAM_BASE),n);return 0;
}
static int address_space_write(void *as, hwaddr a, int attrs, const void *b, size_t n) {
    (void)as;(void)attrs;
    if(a<RAM_BASE || a-RAM_BASE>sizeof(ram) || n>sizeof(ram)-(a-RAM_BASE)) return 1;
    memcpy(ram+(a-RAM_BASE),b,n);return 0;
}
static uint32_t ldl_le_p(const void *b) {
    const uint8_t *p=b;return p[0]|p[1]<<8|p[2]<<16|(uint32_t)p[3]<<24;
}
static void stl_le_p(void *b,uint32_t n) {
    uint8_t *p=b;p[0]=n;p[1]=n>>8;p[2]=n>>16;p[3]=n>>24;
}
static unsigned reads,writes,syncs;
static int fail_sync=-1;
static bool interrupt_sync, interrupt_io, fail_io;
static size_t short_io;
static int write_budget=-1;
static ssize_t test_pread(int fd,void *b,size_t n,off_t off) {
    reads++;
    if(interrupt_io) {interrupt_io=false;errno=EINTR;return -1;}
    if(short_io && n>short_io)n=short_io;
    return pread(fd,b,n,off);
}
static ssize_t test_pwrite(int fd,const void *b,size_t n,off_t off) {
    writes++;
    if(interrupt_io) {interrupt_io=false;errno=EINTR;return -1;}
    if(fail_io || write_budget==0) {errno=EIO;return -1;}
    if(write_budget>0)write_budget--;
    if(short_io && n>short_io)n=short_io;
    return pwrite(fd,b,n,off);
}
static pthread_mutex_t gate_mutex=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t gate_cond=PTHREAD_COND_INITIALIZER;
static bool gate_block,gate_entered;
static int test_fsync(int fd) {
    pthread_mutex_lock(&gate_mutex);
    gate_entered=true;pthread_cond_broadcast(&gate_cond);
    while(gate_block)pthread_cond_wait(&gate_cond,&gate_mutex);
    pthread_mutex_unlock(&gate_mutex);
    syncs++;
    if(interrupt_sync) {interrupt_sync=false;errno=EINTR;return -1;}
    if(fd==fail_sync) {errno=EIO;return -1;}
    return fsync(fd);
}
#define pread test_pread
#define pwrite test_pwrite
#define fsync test_fsync
typedef struct A7State {
    struct { hwaddr asp_cmd_phys; } ans;
    bool system_writable;
'''
        harness += extract('    int sys_fd;', '\n};\n\n/* Diagnostic only.')
        harness += '\n} A7State;\n'
        harness += extract('static bool ans_ram_range(', '/* Model-private logical utility')
        harness += extract('#define ANS_UTILITY_BYTES', 'static const uint8_t utility_magic')
        harness += extract('static bool ans_utility_ready(', 'static void ans_init_utility(')
        harness += extract('static const AnsUtilityRegion *ans_utility_region(', 'static void ans_handle_inbox_msg(')
        harness += r'''
static int backing(void) {
    char name[]="/tmp/ans-host-XXXXXX";
    int fd=mkstemp(name);assert(fd>=0);unlink(name);
    assert(!ftruncate(fd,16*4096));return fd;
}
static uint32_t request(A7State *s,unsigned op,unsigned lba,unsigned n) {
    memset(ram,0,0x21000);ram[0]=op;
    stl_le_p(ram+4,lba);stl_le_p(ram+8,n);
    /* tag31 supports 2048 pages; scatter addresses reverse adjacent pairs. */
    ram[2]=8;
    for(unsigned i=0;i<n && i<2048;i++)
        stl_le_p(ram+0x30+i*4,(RAM_BASE+0x100000+(i^1)*4096)>>12);
    return ans_execute(s,31);
}
static void pattern(unsigned n) {
    for(unsigned i=0;i<n;i++)memset(ram+0x100000+(i^1)*4096,(i%251)+1,4096);
}
static bool barrier_started,barrier_done,barrier_ok;
static void *barrier_thread(void *opaque) {
    pthread_mutex_lock(&gate_mutex);barrier_started=true;
    pthread_cond_broadcast(&gate_cond);pthread_mutex_unlock(&gate_mutex);
    bool ok=ans_flush(opaque);
    pthread_mutex_lock(&gate_mutex);barrier_ok=ok;barrier_done=true;
    pthread_cond_broadcast(&gate_cond);pthread_mutex_unlock(&gate_mutex);
    return NULL;
}
int main(void) {
    QEMUTimer timer={0};
    A7State s={.sys_fd=backing(),.data_fd=backing(),.utility_fd=backing(),
        .sys_lbas=16,.data_lbas=16,.data_start=24,.total_lbas=48,
        .ans_flush_timer=&timer,.ans_perf_enabled=true};
    s.ans.asp_cmd_phys=RAM_BASE-0xfcc0;
    pattern(8);
    assert(request(&s,0x11,24,8)==0);
    assert(writes==1 && syncs==0 && s.ans_dirty==1);
    assert(timer.pending && timer.deadline==1300);
    host_ms=1100;
    assert(request(&s,0x11,24,8)==0);
    assert(writes==2 && syncs==0 && timer.deadline==1300);
    memset(ram+0x100000,0,8*4096);
    assert(request(&s,0x10,24,8)==0 && reads==1);
    for(unsigned i=0;i<8;i++)for(unsigned j=0;j<4096;j++)
        assert(ram[0x100000+(i^1)*4096+j]==i+1);
    assert(s.ans_bytes==3*8*4096 && s.ans_requests==3);
    assert(request(&s,0x13,0,0)==0 && syncs==1 && !s.ans_dirty && !timer.pending);
    assert(request(&s,0x13,0,0)==0 && syncs==1); /* clean flush */
    /* Read-only System: validate every page before writing anything. */
    unsigned before=writes;
    assert(request(&s,0x11,23,2)==5 && writes==before);
    /* Writable System/Data boundary produces exactly two batches. */
    s.system_writable=true;pattern(2);
    assert(request(&s,0x11,23,2)==0 && writes==before+2 && s.ans_dirty==5);
    fail_sync=s.data_fd;
    assert(request(&s,0x13,0,0)==5 && s.ans_dirty==1);
    fail_sync=-1;interrupt_sync=true;
    assert(request(&s,0x13,0,0)==0 && !s.ans_dirty);
    /* Invalid last DMA page must prevent all file writes. */
    before=writes;request(&s,0x13,0,0);
    ram[0]=0x11;stl_le_p(ram+4,24);stl_le_p(ram+8,2);
    stl_le_p(ram+0x30,(RAM_BASE+0x100000)>>12);stl_le_p(ram+0x34,1);
    assert(ans_execute(&s,31)==1 && writes==before);
    /* Failed pwrite is dirty, not successful, and retried by timer. */
    fail_io=true;
    assert(request(&s,0x11,24,2)==5 && s.ans_dirty==1);
    fail_io=false;fail_sync=s.data_fd;
    ans_flush_timer_cb(&s);
    qemu_mutex_lock(&s.ans_mutex);
    while(s.ans_worker_busy || s.ans_worker_pending)
        qemu_cond_wait(&s.ans_cond,&s.ans_mutex);
    qemu_mutex_unlock(&s.ans_mutex);
    assert(s.ans_dirty==1 && timer.pending);
    fail_sync=-1;assert(!ans_flush(&s)); /* sticky background failure */
    assert(ans_flush(&s) && !s.ans_dirty && !timer.pending);
    /* A failed batched write may have written a prefix: it must stay dirty. */
    pattern(2);short_io=4096;write_budget=1;before=writes;
    assert(request(&s,0x11,24,2)==5 && writes==before+2 && s.ans_dirty==1);
    short_io=0;write_budget=-1;
    assert(request(&s,0x13,0,0)==0);
    /* EINTR and short writes finish fully before success. */
    pattern(2);interrupt_io=true;short_io=1024;
    assert(request(&s,0x11,24,2)==0);short_io=0;
    /* EOF copies only complete prefix sectors; later RAM is untouched. */
    assert(!ftruncate(s.data_fd,4096+128));
    memset(ram+0x100000,0xaa,2*4096);
    assert(request(&s,0x10,24,2)==5);
    assert(ram[0x101000]==1 && ram[0x100000]==0xaa);
    assert(!ftruncate(s.data_fd,16*4096));
    /* All three dirty files are attempted, even if the first fails. */
    ans_mark_dirty(&s,s.utility_fd);ans_mark_dirty(&s,s.sys_fd);
    before=syncs;fail_sync=s.data_fd;
    assert(!ans_flush(&s) && syncs==before+3 && s.ans_dirty==1);
    fail_sync=-1;ans_exit_notify(&s.ans_exit_notifier,NULL);assert(!s.ans_dirty);
    ans_mark_dirty(&s,s.utility_fd);
    ans_shutdown_notify(&s.ans_shutdown_notifier,NULL);assert(!s.ans_dirty);
    ans_mark_dirty(&s,s.data_fd);ans_drain(&s);assert(!s.ans_dirty);
    /* Utility requests use one header check plus one contiguous payload I/O. */
    assert(!ftruncate(s.utility_fd,ANS_UTILITY_BYTES));
    s.utility_image="utility";pattern(3);before=writes;
    assert(request(&s,0x41,0,3)==0 && writes==before+1 && s.ans_dirty==2);
    before=reads;memset(ram+0x100000,0,3*4096);
    assert(request(&s,0x31,0,3)==0 && reads==before+2);
    assert(ram[0x101000]==1 && ram[0x100000]==2 && ram[0x103000]==3);
    before=syncs;assert(request(&s,0x13,0,0)==0 && syncs==before+1);
    before=writes;assert(request(&s,0x41,2047,2)==1 && writes==before);
    /* Virtual GPT sectors remain synthesized alongside file-backed runs. */
    s.gpt_lba0[0]=0x55;s.gpt_lba1[0]=0x66;s.gpt_lba2[0]=0x77;
    assert(request(&s,0x10,0,3)==0);
    assert(ram[0x101000]==0x55 && ram[0x100000]==0x66 && ram[0x103000]==0x77);
    /* Full native limit stays one syscall, including scattered DMA pages. */
    assert(!ftruncate(s.data_fd,2048*4096));s.data_lbas=2048;s.total_lbas=2080;
    pattern(2048);before=writes;
    assert(request(&s,0x11,24,2048)==0 && writes==before+1);
    ans_drain(&s);
    /* Freeze fsync and complete a new write/read while the worker is inside it. */
    ans_mark_dirty(&s,s.data_fd);
    pthread_mutex_lock(&gate_mutex);gate_block=true;gate_entered=false;
    pthread_mutex_unlock(&gate_mutex);
    ans_flush_timer_cb(&s);
    pthread_mutex_lock(&gate_mutex);
    while(!gate_entered)pthread_cond_wait(&gate_cond,&gate_mutex);
    pthread_mutex_unlock(&gate_mutex);
    pattern(2);assert(request(&s,0x11,24,2)==0);
    assert(request(&s,0x10,24,2)==0 && ram[0x101000]==1);
    pthread_mutex_lock(&gate_mutex);gate_block=false;
    pthread_cond_broadcast(&gate_cond);pthread_mutex_unlock(&gate_mutex);
    qemu_mutex_lock(&s.ans_mutex);
    while(s.ans_worker_busy || s.ans_worker_pending)
        qemu_cond_wait(&s.ans_cond,&s.ans_mutex);
    assert(s.ans_dirty==1); /* old fsync cannot clear the new generation */
    qemu_mutex_unlock(&s.ans_mutex);
    before=syncs;assert(ans_flush(&s) && syncs==before+1);
    /* Barrier cannot finish until a blocked worker has completed fsync. */
    ans_mark_dirty(&s,s.data_fd);
    pthread_mutex_lock(&gate_mutex);gate_block=true;gate_entered=false;
    pthread_mutex_unlock(&gate_mutex);
    ans_flush_timer_cb(&s);
    pthread_mutex_lock(&gate_mutex);
    while(!gate_entered)pthread_cond_wait(&gate_cond,&gate_mutex);
    pthread_mutex_unlock(&gate_mutex);
    pthread_t barrier;assert(!pthread_create(&barrier,NULL,barrier_thread,&s));
    pthread_mutex_lock(&gate_mutex);
    while(!barrier_started)pthread_cond_wait(&gate_cond,&gate_mutex);
    assert(!barrier_done);gate_block=false;
    pthread_cond_broadcast(&gate_cond);pthread_mutex_unlock(&gate_mutex);
    assert(!pthread_join(barrier,NULL));assert(barrier_done && barrier_ok);
    uint64_t hist[64]={0};hist[1]=1;hist[4]=19;
    assert(ans_percentile(hist,0,50)==0);
    assert(ans_percentile(hist,20,5)==1 && ans_percentile(hist,20,95)==15);
    ans_worker_stop(&s);
    close(s.sys_fd);close(s.data_fd);close(s.utility_fd);
    return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix='ans-storage-') as directory:
            root = Path(directory)
            (root / 'test.c').write_text(harness)
            build = subprocess.run([cc, '-std=c11', '-Wall', '-Wextra', '-Werror',
                                    '-pthread', '-fsanitize=' + os.environ.get('ANS_SANITIZER', 'address,undefined'), str(root / 'test.c'),
                                    '-o', str(root / 'test')], capture_output=True, text=True)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(root / 'test')], capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stderr)
