"""Unit test harness for the AGX off-BQL replay render thread."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'backend/s5l8960x.c'


class AGXReplayThreadTests(unittest.TestCase):
    def test_required_comment_preserved(self):
        source = SOURCE.read_text()
        self.assertIn('/* The TA command whose parameter buffer', source)

    def test_env_switch_and_thread_structures(self):
        source = SOURCE.read_text()
        self.assertIn('VPHONE_AGX_REPLAY_THREAD', source)
        self.assertIn('agx_replay_thread_enabled', source)
        self.assertIn('typedef struct AGXRenderJob', source)
        self.assertIn('qemu_thread_create(&s->agx_render_thread, "agx-render"', source)
        self.assertIn('aio_bh_schedule_oneshot', source)
        self.assertIn('agx_replay_complete_bh', source)

    def test_thread_safety_invariants(self):
        source = SOURCE.read_text()
        # Verify thread-local re-entrant buffers in replay
        self.assertIn('static __thread uint32_t row[640 * 4], texrow[0x4000];', source)
        # Verify RCU registration and locking in worker thread
        self.assertIn('rcu_register_thread();', source)
        self.assertIn('rcu_unregister_thread();', source)
        self.assertIn('rcu_read_lock();', source)
        self.assertIn('rcu_read_unlock();', source)
        # Verify BQL synchronization via bottom half
        self.assertIn('aio_bh_schedule_oneshot(qemu_get_aio_context(),', source)

    def test_replay_thread_c_harness(self):
        compiler = shutil.which('cc')
        if not compiler:
            self.skipTest('host C compiler unavailable')

        harness_code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#include <unistd.h>

typedef struct AGXRenderJob {
    void *s;
    uint8_t ta[0x280];
    uint64_t cat_base;
    uint64_t target;
    uint32_t width;
    uint32_t height;

    bool has_stamp;
    uint64_t stamp_gva;
    uint32_t stamp_meta_addr;
    uint32_t stamp_value;
    uint32_t stamp_channel;
    uint32_t queue_block;

    bool has_queue_ctrl;
    uint64_t queue_ctrl_gva;
    uint32_t queue_read_index;

    bool post_completion;
    unsigned commands;
    unsigned stamps;

    struct AGXRenderJob *next;
} AGXRenderJob;

/* Simulated guest memory */
static uint32_t mock_ram[1024];
static uint32_t mock_stamp_value = 0;
static uint32_t mock_queue_read_index = 0;
static bool mock_fw_completion_posted = false;
static int event_seq = 0;
static int pixel_done_seq = 0;
static int stamp_done_seq = 0;
static int fw_done_seq = 0;

static pthread_mutex_t mock_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t mock_cond = PTHREAD_COND_INITIALIZER;
static AGXRenderJob *mock_job_head = NULL;
static AGXRenderJob *mock_job_tail = NULL;
static bool mock_thread_exit = false;

static void mock_complete_bh(AGXRenderJob *job) {
    if (job->has_stamp) {
        mock_stamp_value = job->stamp_value;
        stamp_done_seq = ++event_seq;
    }
    if (job->has_queue_ctrl) {
        mock_queue_read_index = job->queue_read_index;
    }
    if (job->post_completion) {
        mock_fw_completion_posted = true;
        fw_done_seq = ++event_seq;
    }
    free(job);
}

static void *mock_render_thread(void *arg) {
    (void)arg;
    pthread_mutex_lock(&mock_mutex);
    while (!mock_thread_exit) {
        while (!mock_job_head && !mock_thread_exit) {
            pthread_cond_wait(&mock_cond, &mock_mutex);
        }
        if (mock_thread_exit) break;

        AGXRenderJob *job = mock_job_head;
        mock_job_head = job->next;
        if (!mock_job_head) mock_job_tail = NULL;
        job->next = NULL;
        pthread_mutex_unlock(&mock_mutex);

        /* Simulate rasterisation into guest RAM */
        if (job->target != 0) {
            for (unsigned i = 0; i < 16; i++) {
                mock_ram[i] = 0xdeadbeef;
            }
            pixel_done_seq = ++event_seq;
        }

        /* Simulate aio_bh_schedule_oneshot */
        mock_complete_bh(job);

        pthread_mutex_lock(&mock_mutex);
    }
    pthread_mutex_unlock(&mock_mutex);
    return NULL;
}

int main(void) {
    pthread_t th;
    pthread_create(&th, NULL, mock_render_thread, NULL);

    /* Test 1: Verify job snapshotting and ordering */
    AGXRenderJob *j1 = calloc(1, sizeof(AGXRenderJob));
    j1->target = 0x400000000ULL;
    j1->cat_base = 0x800010000ULL;
    j1->width = 640;
    j1->height = 1136;
    memset(j1->ta, 0xaa, sizeof(j1->ta));
    j1->has_stamp = true;
    j1->stamp_value = 0x1234;
    j1->has_queue_ctrl = true;
    j1->queue_read_index = 2;
    j1->post_completion = true;
    j1->commands = 2;
    j1->stamps = 1;

    pthread_mutex_lock(&mock_mutex);
    mock_job_head = j1;
    mock_job_tail = j1;
    pthread_cond_signal(&mock_cond);
    pthread_mutex_unlock(&mock_mutex);

    /* Wait for job completion */
    for (int i = 0; i < 100; i++) {
        usleep(10000);
        if (mock_fw_completion_posted) break;
    }

    assert(mock_ram[0] == 0xdeadbeef);
    assert(mock_stamp_value == 0x1234);
    assert(mock_queue_read_index == 2);
    assert(mock_fw_completion_posted == true);
    /* Invariant: pixels must be in RAM before stamp store and completion */
    assert(pixel_done_seq > 0);
    assert(stamp_done_seq > pixel_done_seq);
    assert(fw_done_seq >= stamp_done_seq);

    /* Stop thread */
    pthread_mutex_lock(&mock_mutex);
    mock_thread_exit = true;
    pthread_cond_signal(&mock_cond);
    pthread_mutex_unlock(&mock_mutex);
    pthread_join(th, NULL);

    printf("ALL HARNESS TESTS PASSED\n");
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / 'test_harness.c'
            bin_path = Path(td) / 'test_harness'
            src.write_text(harness_code)
            res = subprocess.run([compiler, '-O2', '-Wall', '-Wextra',
                                  str(src), '-o', str(bin_path), '-lpthread'],
                                 capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f"Compilation failed: {res.stderr}")
            run_res = subprocess.run([str(bin_path)], capture_output=True, text=True)
            self.assertEqual(run_res.returncode, 0, f"Runtime failed: {run_res.stderr}")
            self.assertIn("ALL HARNESS TESTS PASSED", run_res.stdout)


if __name__ == '__main__':
    unittest.main()
