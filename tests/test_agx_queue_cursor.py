"""Empty queue polls must preserve the guest cursor while replay is in flight."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class QueueCursorTests(unittest.TestCase):
    def test_empty_poll_keeps_private_cursor_private(self):
        compiler = shutil.which('cc')
        if not compiler:
            self.skipTest('C compiler required')
        src = (ROOT / 'backend/s5l8960x.c').read_text()
        job = src[src.index('typedef struct AGXRenderJob {'):src.index('#define AGX_PDS_HEAP')]
        fn = src[src.index('static unsigned agx_drain_queue('):src.index('/* Ring 3 has no doorbell')]
        harness = r'''
#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <assert.h>
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define g_new0(t,n) ((t*)calloc(n,sizeof(t)))
#define qemu_log_mask(...) ((void)0)
#define QEMU_CLOCK_VIRTUAL 0
#define timer_mod(...) ((void)0)
#define qemu_clock_get_ns(...) 0
#define smp_wmb() ((void)0)
#define LOG_UNIMP 0
#define LOG_GUEST_ERROR 0
typedef struct AGXReplaySnapshot {} AGXReplaySnapshot;
typedef struct {
 struct { bool valid; uint64_t control; uint32_t index; } agx_host_read[16];
 unsigned agx_inflight_jobs;
 uint8_t agx_last_ta[16][0x280], agx_ta_ring[16][256][0x280], agx_ta_ring_next[16];
 AGXReplaySnapshot *agx_ta_snapshot[16][256];
 bool agx_last_ta_valid[16];
 uint32_t agx_last_ta_queue[16];
 uint64_t agx_ctx_cat[16], agx_cat_base;
 void *agx_kick_timer;
} A7State;
static unsigned writes;
static uint32_t guest_cursor;
static uint64_t agx_meta_gva(uint32_t x) { return x; }
static bool agx_gpu_access(A7State*s,uint64_t addr,void*buf,size_t n,bool write) {
 if(write){writes++;if(addr==0x200)memcpy(&guest_cursor,buf,4);return true;}
 if(addr==0x104){uint32_t ptrs[2]={0x200,0x300};memcpy(buf,ptrs,n);return true;}
 uint32_t value=addr==0x200?guest_cursor:addr==0x220?2:addr==0x230?4:0;
 memcpy(buf,&value,n);return true;
}
static bool agx_replay_enabled(void){return true;}
static bool agx_replay_thread_enabled(void){return true;}
static void agx_snapshot_unref(AGXReplaySnapshot*p){}
static AGXReplaySnapshot* agx_capture_ta(A7State*s,uint64_t c,const uint8_t*t){return NULL;}
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            code = harness + job + '\nstatic void agx_trace_rt(A7State*s,uint32_t b,uint32_t a,AGXRenderJob**j){}\n' + fn + r'''
int main(void){
 A7State*s=calloc(1,sizeof(*s));
 s->agx_host_read[0].valid=true;s->agx_host_read[0].control=0x200;s->agx_host_read[0].index=2;
 s->agx_inflight_jobs=1;
 AGXRenderJob *head=NULL,*tail=NULL;unsigned stamps=0;
 assert(agx_drain_queue(s,0x100,&stamps,&head,&tail)==0);
 assert(guest_cursor==0 && writes==0 && head==NULL && tail==NULL);
 assert(s->agx_host_read[0].valid && s->agx_host_read[0].index==2);
 free(s);return 0;
}
'''
            (path / 'test.c').write_text(code)
            subprocess.run([compiler, '-O1', str(path / 'test.c'), '-o', str(path / 'test')], check=True)
            subprocess.run([str(path / 'test')], check=True)
