from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from probe_summary import summarize, write_summary, ppm_nonzero
from parallel_probe import wait_for_load, main


class ProbeSummaryTests(unittest.TestCase):
    def test_real_format_and_last_write_not_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'breakpoints.json').write_text(json.dumps([{'name': 'meta_wait'}, {'name': 'meta_wait', 'return_of': {}}, {'name': 'swap'}]))
            (root / 'serial.log').write_bytes(b'normal\npanic(cpu 0): failure\nDebugger message: panic\nGPU hang GPU hang\n\xff\n')
            (root / 'qemu-trace.log').write_text('''DISPLAY_MMIO op=write pa=0x206204044 value=0x123
DISPLAY_MMIO op=write pa=0x206204044 value=0x0
DISPLAY_MMIO op=read pa=0x206204044 value=0xffff
DISPLAY_MMIO op=write pa=0x206204070 value=0x02800470
AGX META: alive posted (PIO 39, shared GVA 0xe1c0448000)
AGX META: queue 0x1 stamp 0x2 <- 3 (channel 0)
AGX META: queue 0x2 stamp 0x3 <- 4 (channel 1)
AGX META: queue 0x2 cmd 0x3 type 1
AGX META: host ring 0 type 1 arg 0x4
AGX META: consumed 3 commands, 2 stamps
''')
            (root / 'framebuffer.ppm').write_bytes(b'P6\n# comment\n2 1\n255\n\x00\x01\x00\x20\x0a\xff')
            (root / 'process-exit.json').write_text('{"exit_code": -9, "reaped": true}')
            result, table = write_summary(root, 1)
            self.assertEqual(result['breakpoint_hits'], {'meta_wait': 2, 'swap': 1})
            self.assertEqual(len(result['panic_lines']), 2)
            self.assertEqual(result['gpu_hang_count'], 2)
            self.assertEqual(result['display_mmio_last_writes'], {'0x206204044': '0x0', '0x206204070': '0x2800470'})
            self.assertEqual(result['display_mmio_write_counts'], {'0x206204044': 2, '0x206204070': 1})
            self.assertEqual(result['display_mmio_distinct_write_values'],
                             {'0x206204044': ['0x123', '0x0'], '0x206204070': ['0x2800470']})
            self.assertEqual(result['agx_meta_counts']['queue stamp'], 2)
            self.assertEqual(sum(result['agx_meta_counts'].values()), 6)
            self.assertEqual(result['framebuffer_nonzero_bytes'], 4)
            self.assertEqual(result['exit_code'], -9)
            self.assertEqual(result['probe_exit_code'], 1)
            self.assertEqual(json.loads((root / 'summary.json').read_text()), result)
            self.assertEqual((root / 'summary.txt').read_text(), table)

    def test_fifo_legacy_stderr_and_monitor_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'stderr.log').write_text(
                '[DISP0] layer0 base off=0x4044 val=0x18e0000\n'
                '[DISP0] layer0 base off=0x4044 val=0x0\n'
                '[DISP0] layer0 base off=0x4044 val=0x18e0000\n')
            (root / 'qemu-trace.log').write_text(
                'DISPLAY_MMIO ns=179826254000 origin=host region=a7.disp0 op=read '
                'pa=0x206204044 off=0x4044 size=4 value=0xffff pc=0x0\n')
            (root / 'mmio_registers.json').write_text(json.dumps({
                '0x206204040': '206204040: 0x00001000 0x00000000 0x00000a00 0x00000000\r\n'
                '206204070: 0x02800470 0x00000000 0x00000000 0x00000000\r\n'}))
            result, table = write_summary(root)
            self.assertEqual(result['display_mmio_last_writes'],
                             {'0x206204044': '0x18e0000', '0x206204070': None})
            self.assertEqual(result['display_mmio_write_counts'],
                             {'0x206204044': 3, '0x206204070': 0})
            self.assertEqual(result['display_mmio_distinct_write_values'],
                             {'0x206204044': ['0x18e0000', '0x0'], '0x206204070': []})
            self.assertEqual(result['display_mmio_snapshots'],
                             {'0x206204044': '0x0', '0x206204070': '0x2800470'})
            self.assertIn('0x2800470 (snapshot)', table)
            # Structured records supersede legacy output for that register.
            with (root / 'qemu-trace.log').open('a') as trace:
                trace.write(
                    'DISPLAY_FIFO ns=1 origin=guest region=a7.disp0 op=write '
                    'pa=0x206204044 off=0x4044 size=4 value=0x0 via=fifo\n'
                    'DISPLAY_FIFO ns=1 origin=guest region=a7.disp0 op=write '
                    'pa=0x206204070 off=0x4070 size=4 value=0x2800470 via=fifo\n'
                    'DISPLAY_MMIO ns=123 origin=guest region=a7.disp0 op=write '
                    'pa=0x206204044 off=0x4044 size=4 value=0x18e0000 pc=0xffffff8000f29e9c\n')
            result = summarize(root)
            self.assertEqual(result['display_mmio_write_counts'],
                             {'0x206204044': 2, '0x206204070': 1})
            self.assertEqual(result['display_mmio_last_writes']['0x206204044'], '0x18e0000')
            self.assertEqual(result['display_mmio_distinct_write_values']['0x206204044'],
                             ['0x0', '0x18e0000'])

    def test_missing_files_and_truncated_ppm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = summarize(root)
            self.assertIsNone(result['exit_code'])
            self.assertIsNone(result['framebuffer_nonzero_bytes'])
            self.assertEqual(result['breakpoint_hits'], {})
            ppm = root / 'framebuffer.ppm'
            ppm.write_bytes(b'P6\n1 1\n255\n\0')
            with self.assertRaises(ValueError):
                ppm_nonzero(ppm)

    def test_load_recovers_and_timeout_aborts_without_starting(self):
        with patch('parallel_probe.os.getloadavg', side_effect=[(7, 0, 0), (6, 0, 0)]), patch('parallel_probe.time.sleep') as sleep:
            self.assertTrue(wait_for_load(6, 300))
            sleep.assert_called_once_with(10)
        with patch('parallel_probe.os.getloadavg', return_value=(7, 0, 0)), patch('parallel_probe.subprocess.Popen') as popen, patch('sys.argv', ['parallel_probe', '--out-root', '/unused', '--variant', 'a=', '--load-wait', '0']):
            self.assertEqual(main(), 1)
            popen.assert_not_called()

    def test_queue_reaps_fast_variant_before_slow_one(self):
        class Process:
            def __init__(self, name):
                self.name = name
            def poll(self):
                return None if self.name == 'slow' and 'third' not in launched else 0
            def wait(self):
                finished.append(self.name)
                return 0
        launched, finished = [], []
        def launch(cmd, **kwargs):
            name = Path(cmd[cmd.index('--output') + 1]).name
            self.assertLess(len(launched) - len(finished), 2)
            launched.append(name)
            return Process(name)
        with tempfile.TemporaryDirectory() as tmp, patch('parallel_probe.wait_for_load', return_value=True), patch('parallel_probe.subprocess.Popen', side_effect=launch), patch('sys.argv', ['parallel_probe', '--out-root', tmp, '--variant', 'slow=', '--variant', 'fast=', '--variant', 'third=']):
            self.assertEqual(main(), 0)
            self.assertEqual(launched, ['slow', 'fast', 'third'])
            self.assertEqual(finished[0], 'fast')
            self.assertTrue((Path(tmp) / 'third/summary.json').exists())


if __name__ == '__main__':
    unittest.main()
