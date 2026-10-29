"""Firmware-free fixtures for the live suite's measurement contracts."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from perf_metrics import distribution, parse_line, compare, aggregate, pacing_distribution
from perf_common import LiveLock, environment
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
        rows = compare(cand, base)
        self.assertEqual([r['metric'] for r in rows if r['failed']], ['present_ms.p95'])
        self.assertTrue(all(r['failed'] for r in compare({}, {})))
        self.assertTrue(compare(cand, {**base, 'present_ms.p95': 0})[0]['failed'])

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
        self.assertTrue(rows[0]['failed'])
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
