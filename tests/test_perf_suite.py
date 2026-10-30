"""Firmware-free fixtures for the live suite's measurement contracts."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from perf_metrics import distribution, parse_line, compare, aggregate, pacing_distribution, aa_variation, summarize, window_phase, clock_discontinuity
from perf_common import LiveLock, environment, host_preflight
from perf_suite import touch


class PerfMetricsTests(unittest.TestCase):
    def test_nearest_rank(self):
        self.assertEqual(distribution(range(1, 101))['p95'], 95)
        self.assertEqual(distribution([4])['p99'], 4)
        self.assertIsNone(distribution([])['p50'])
        self.assertEqual(distribution([None, float('nan'), 0])['max'], 0)

    def test_log_fixtures(self):
        text = '[pacing 5s] swap-complete n=12 interval_ms(p50/p95/p99/max)=16.70/20.10/80.00/90.00'
        self.assertEqual(parse_line(text)['p95'], 20.1)
        self.assertEqual(parse_line(text)['n'], 12)
        player = '[player-stats] FPS: 60.0, present_ms(p50/p95/p99/max): 1.0/2.0/3.0/4.0'
        self.assertEqual(parse_line(player)['p95'], 2)
        payload = {'virtual_ns': 5000000000, 'metrics': {'vblank': {'p95': 17}}}
        self.assertEqual(parse_line('[pacing-json] '+json.dumps(payload))['metrics'], payload['metrics'])
        self.assertIsNone(parse_line('[pacing-json] {incomplete'))
        self.assertIsNone(parse_line('noise'))

    def test_comparison_missing_zero_and_threshold(self):
        base = {k: 100 for k in ('present_ms.p95', 'input_ms.p50', 'cpu_pct.mean', 'boot_host_s.p50')}
        cand = {**base, 'present_ms.p95': 116, 'input_ms.p50': 119}
        rows = compare(cand, base, {k: .01 for k in base})
        self.assertEqual([r['metric'] for r in rows if r['failed']], ['present_ms.p95'])
        self.assertTrue(all(r['status'] == 'unavailable' for r in compare({}, {})))
        self.assertTrue(all(not r['failed'] for r in compare(cand, base)))
        row = next(r for r in compare(cand, {**base, 'present_ms.p95': 0}) if r['metric'] == 'present_ms.p95')
        self.assertEqual(row['status'], 'unavailable')

    def test_repeat_aggregation(self):
        result = aggregate([{'metrics': {'a': v}} for v in (30, 10, 20)])
        self.assertEqual(result['a']['median'], 20)
        self.assertEqual((result['a']['min'], result['a']['max']), (10, 30))

    def test_histogram_merge_and_overflow(self):
        bins = [0]*1024
        bins[166], bins[1023] = 95, 5
        rows = [{'metrics': {'vblank': {'bins_100us': bins, 'max': 700}}}]
        d = pacing_distribution(rows, 'vblank')
        self.assertEqual(d['n'], 100)
        self.assertEqual(d['p95'], 16.7)
        self.assertEqual(d['p99'], 700)
        self.assertIsNone(pacing_distribution([], 'vblank')['p95'])

    def test_bad_or_truncated_percentiles(self):
        self.assertIsNone(parse_line('[player-stats] present_ms(p50/p95/p99/max): 1/2/'))
        rows = compare({'present_ms.p95': float('nan')}, {'present_ms.p95': 10})
        self.assertTrue(all(r['status'] == 'unavailable' for r in rows))
        rows = compare({'present_ms.p95': 115}, {'present_ms.p95': 100})
        self.assertFalse(rows[0]['failed'])

    def test_stats_environment(self):
        with patch.dict(os.environ, {'VPHONE_PERF_STATS': '0'}):
            env = environment('touch')
        self.assertEqual(env['VPHONE_PERF_STATS'], '1')
        self.assertEqual(env['VPHONE_PLAYER_STATS'], '1')
        self.assertEqual(env['VPHONE_TOUCH_SCRIPT'], 'touch')

    def test_stale_lock_and_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'lock'
            path.mkdir()
            (path/'pid').write_text('999999')
            with patch('perf_common.pid_alive', return_value=False):
                with LiveLock('test', path):
                    self.assertEqual((path/'pid').read_text(), str(os.getpid()))
            self.assertFalse(path.exists())

    def test_qmp_matches_player_scaling(self):
        class QMP:
            def execute(self, command, args):
                self.command, self.args = command, args
        qmp = QMP()
        touch(qmp, 'begin', 560, 233)
        self.assertEqual(qmp.command, 'input-send-event')
        self.assertEqual(qmp.args['device'], 'a7-touch-display')
        events = qmp.args['events']
        self.assertEqual(events[0]['data']['value'], 560*32767//640)
        self.assertEqual(events[1]['data']['value'], 243*32767//1136)
        self.assertEqual(events[2]['data']['type'], 'begin')


class StabilizedContractsTests(unittest.TestCase):
    fixtures = Path(__file__).parent/'fixtures/perf'

    def rows(self, name):
        return [parse_line(l) for l in (self.fixtures/name).read_text().splitlines()]

    def test_real_clock_gap_is_host_and_virtual_not_virtual_only(self):
        for name in ('base-clock-gap.log', 'cand-clock-gap.log'):
            before, gap, after = self.rows(name)
            host = (gap['host_ns']-before['host_ns'])/1e9
            guest = (gap['virtual_ns']-before['virtual_ns'])/1e9
            self.assertGreater(host, 900)
            self.assertAlmostEqual(host, guest, places=2)
            self.assertGreater(gap['metrics']['vblank-late']['max'], 900000)
            self.assertGreater(after['metrics']['swap-complete']['max'], 900000)

    def test_legacy_zeros_are_not_present_samples(self):
        rows = self.rows('player-empty-present.log')
        self.assertGreater(rows[-1]['fps'], 1)  # receive FPS != presentation FPS
        result = summarize(rows, [], [])
        self.assertIsNone(result['present_ms.p95'])
        self.assertIsNone(result['player.arrive_to_present_ms.window_p95_max'])
        self.assertGreater(result['player.fps.mean'], 0)
        self.assertEqual(self.rows('ans-lifetime.log')[0]['requests'], 5888)
        self.assertIsNone(summarize(self.rows('ans-lifetime.log'), [], [])['ans.requests'])

    def test_known_player_workload_and_empty_sample_counts(self):
        rows = [{'kind': 'player', 'fps': 60, 'dropped': 2,
                 'sample_counts': {'present_ms': 60, 'recv_ms': 60},
                 'present_ms': {'p95': 16.7}, 'recv_ms': {'p95': 2}},
                {'kind': 'player', 'fps': 0, 'dropped': 0,
                 'sample_counts': {'present_ms': 0}, 'present_ms': {'p95': 0}}]
        result = summarize(rows, [], [])
        self.assertEqual(result['present_ms.p95'], 16.7)
        self.assertEqual(result['player.recv_ms.window_p95_max'], 2)
        self.assertEqual(result['player.dropped.mean'], 1)

    def test_unvalidated_input_cannot_produce_number(self):
        # Known delays alone do not prove that the changed pixels are caused by input.
        for kind in ('tap', 'swipe', 'toggle', 'folder'):
            result = summarize([], [], [{'kind': kind, 'latency_ms': 1.6, 'valid': False},
                                        {'kind': kind, 'latency_ms': 1766, 'valid': False}])
            self.assertIsNone(result['input_ms.p50'])

    def test_boundary_and_boot_exclusion(self):
        markers = [{'name': 'swipe', 'host_s': 110}, {'name': 'settings', 'host_s': 130}]
        self.assertEqual(window_phase(95, 100, markers, 105), 'boot')
        self.assertEqual(window_phase(100, 106, markers, 105), 'boundary')
        self.assertEqual(window_phase(108, 112, markers, 105), 'boundary')
        self.assertEqual(window_phase(115, 120, markers, 105), 'swipe')
        self.assertEqual(window_phase(125, 130, markers, 105), 'boundary')
        bins = [0]*1024; bins[166] = 10
        rows = [{'kind': 'pacing', 'excluded_metrics': ['swap-complete'], 'metrics': {
            'swap-complete': {'n': 10, 'max': 900000, 'bins_100us': bins},
            'vblank': {'n': 10, 'max': 16.7, 'bins_100us': bins}}}]
        result = summarize(rows, [], [])
        self.assertIsNone(result['swap-complete_ms.max'])
        self.assertEqual(result['vblank_ms.p99'], 16.7)

    def test_aa_noise_calibrates_gate(self):
        runs = [{'metrics': {'cpu_pct.mean': x}} for x in (80, 100, 120)]
        noise = aa_variation(runs)
        self.assertAlmostEqual(noise['cpu_pct.mean'], .2)
        def cpu(c):
            return next(r for r in compare({'cpu_pct.mean': c}, {'cpu_pct.mean': 100}, noise)
                        if r['metric'] == 'cpu_pct.mean')
        self.assertFalse(cpu(125)['failed'])
        self.assertTrue(cpu(145)['failed'])
        self.assertEqual(aa_variation(runs[:1]), {})
        self.assertEqual(aggregate([{'metrics': {'missing': None}}])['missing']['available_runs'], 0)

    def test_ans_window_histogram_and_process_steps(self):
        hist = [0]*64; hist[10] = 95; hist[20] = 5
        rows = [{'kind': 'pacing', 'ans': {'requests': 100, 'fsyncs': 3,
                 'background_fsync_bql_ns': 0, 'handler_max_ns': 800000,
                 'handler_bins_log2_ns': hist}}]
        samples = [{'process': 'qemu', 'cpu_pct': x, 'rss_bytes': 1000*x} for x in (10, 20)]
        result = summarize(rows, samples, [])
        self.assertEqual(result['ans.handler_ns.p95_le'], 1023)
        self.assertEqual(result['ans.handler_ns.max'], 800000)
        self.assertEqual(result['ans.fsyncs'], 3)
        self.assertEqual(result['qemu.cpu_pct.p95'], 20)
        self.assertEqual(result['qemu.rss_bytes.max'], 20000)
        self.assertIsNone(result['cpu_pct.mean'])  # missing player != zero CPU

    def test_preflight_refuses_load_and_existing_guests(self):
        from subprocess import CompletedProcess
        with patch('perf_common.os.getloadavg', return_value=(3.1, 2, 1)):
            with self.assertRaisesRegex(RuntimeError, 'load'):
                host_preflight()
        with patch('perf_common.os.getloadavg', return_value=(1, 1, 1)), patch(
                'perf_common.subprocess.run', return_value=CompletedProcess([], 0, '123 /tmp/qemu-system-aarch64\n')):
            with self.assertRaisesRegex(RuntimeError, 'QEMU/player'):
                host_preflight()

    def test_actual_c_pacing_json_with_known_intervals(self):
        import shutil
        import subprocess
        cc = shutil.which('cc')
        if not cc:
            self.skipTest('C compiler unavailable')
        source = (Path(__file__).resolve().parents[1]/'backend/s5l8960x.c').read_text()
        functions = source[source.index('/* Pacing statistics never feed'):source.index('#ifdef CONFIG_VNC\n/* Patch 0006')]
        state = source[source.index('    /* Wall-clock interval histograms:'):source.index('    QEMUTimer *agx_meta_timer;')]
        harness = r'''
#include <stdbool.h>
#include <stdint.h>
#include <inttypes.h>
#include <stdio.h>
#include <string.h>
#define MIN(a,b) ((a)<(b)?(a):(b))
#define MAX(a,b) ((a)>(b)?(a):(b))
#define QEMU_CLOCK_REALTIME 0
#define QEMU_CLOCK_VIRTUAL 1
static int64_t qemu_clock_get_ns(int k) { return k ? 5000000000LL : 10000000000LL; }
#define qemu_mutex_lock(x) ((void)0)
#define qemu_mutex_unlock(x) ((void)0)
typedef struct { bool agx_perf_enabled, ans_perf_enabled, ans_worker_started;
int ans_mutex;
uint64_t ans_fsyncs, ans_requests, ans_report_requests, ans_report_fsyncs;
uint64_t ans_window_max_ns, ans_window_hist[64];
''' + state + '} A7State;\n' + functions + r'''
int main(void) {
 A7State s = { .agx_perf_enabled=true, .ans_perf_enabled=true,
               .ans_requests=100, .ans_window_max_ns=1000 };
 s.ans_window_hist[10]=100;
 pacing_sample(&s,0,1000000000LL);
 pacing_sample(&s,0,1016600000LL);
 pacing_sample(&s,1,1000000000LL);
 pacing_sample(&s,1,1030000000LL);
 pacing_duration(&s,2,2000000);
 pacing_duration(&s,3,8000000);
 pacing_report(&s,10000000000LL);
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path/'test.c').write_text(harness)
            subprocess.run([cc, '-Wall', '-Werror', str(path/'test.c'), '-o', str(path/'test')], check=True, capture_output=True)
            result = subprocess.run([str(path/'test')], capture_output=True, text=True, check=True)
        row = next(parse_line(l) for l in result.stderr.splitlines() if '[pacing-json]' in l)
        self.assertEqual(row['metrics']['swap-request']['n'], 1)
        self.assertEqual(row['metrics']['vblank']['first_start_host_ns'], 1000000000)
        metrics = summarize([row], [], [])
        self.assertEqual(metrics['swap_request_ms.p95'], 8.1)
        self.assertEqual(metrics['vblank_ms.p95'], 16.7)
        self.assertEqual(metrics['ans.requests'], 100)
        self.assertEqual(metrics['ans.handler_ns.p95_le'], 1023)


    def test_backend_clock_leap_is_not_suite_elapsed_time(self):
        rows = json.loads((self.fixtures/'clock-receipts.json').read_text())
        for before, after in rows.values():
            producer = (after['host_ns']-before['host_ns'])/1e9
            suite = after['host_s']-before['host_s']
            self.assertGreater(producer, 900)
            self.assertLess(suite, 3)
            self.assertTrue(clock_discontinuity(producer, suite))
        self.assertFalse(clock_discontinuity(5.01, 5.03))
        self.assertTrue(clock_discontinuity(5, 25))
