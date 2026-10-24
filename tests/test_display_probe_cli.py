"""Firmware-free unit tests for display_probe CLI options, bounds, and timing semantics."""
import io
from pathlib import Path
import math
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import display_probe
from probe_state import RunClock


class TestDisplayProbeCli(unittest.TestCase):
    def setUp(self):
        self.parser = display_probe.build_parser()

    def parse_and_validate(self, args_list):
        args = self.parser.parse_args(args_list)
        return display_probe.validate_args(self.parser, args), args

    def assert_validation_error(self, args_list, expected_pattern):
        # parser.error raises SystemExit(2)
        stderr_buf = io.StringIO()
        with patch('sys.stderr', stderr_buf):
            with self.assertRaises(SystemExit) as ctx:
                self.parse_and_validate(args_list)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn(expected_pattern, stderr_buf.getvalue())

    def test_default_args(self):
        _, args = self.parse_and_validate(['--output', '/tmp/dummy-out'])
        self.assertEqual(args.seconds, 45.0)
        self.assertEqual(args.stop_condition, 'first-pixel')
        self.assertFalse(args.long_run)
        self.assertEqual(args.step_count, 1)

    def test_normal_limit_boundaries(self):
        # Positive values <= 180 are accepted with default/any stop-condition without --long-run
        for sec in (0.001, 1.0, 45.0, 179.9, 180.0):
            for sc in ('first-pixel', 'ca-scanout', 'duration'):
                _, args = self.parse_and_validate(['--output', '/tmp/dummy-out', '--seconds', str(sec), '--stop-condition', sc])
                self.assertAlmostEqual(args.seconds, sec)

        # > 180 without --long-run must fail
        for sec in (180.001, 181.0, 900.0, 1200.0):
            self.assert_validation_error(
                ['--output', '/tmp/dummy-out', '--seconds', str(sec)],
                'without --long-run'
            )

    def test_non_positive_and_non_finite_seconds(self):
        # 0 and negative values must fail
        for arg in ('--seconds=0', '--seconds=0.0', '--seconds=-0.0', '--seconds=-1', '--seconds=-180'):
            self.assert_validation_error(['--output', '/tmp/dummy-out', arg], 'finite positive')

        # NaN and infinities must fail
        for arg in ('--seconds=nan', '--seconds=NaN', '--seconds=inf', '--seconds=-inf', '--seconds=Infinity', '--seconds=-Infinity'):
            self.assert_validation_error(['--output', '/tmp/dummy-out', arg], 'finite positive')

    def test_long_run_opt_in_requires_stop_condition_duration(self):
        # --long-run without --stop-condition duration must fail even if seconds <= 180
        self.assert_validation_error(
            ['--output', '/tmp/dummy-out', '--long-run', '--seconds', '45', '--stop-condition', 'first-pixel'],
            '--long-run requires --stop-condition duration'
        )
        self.assert_validation_error(
            ['--output', '/tmp/dummy-out', '--long-run', '--seconds', '900', '--stop-condition', 'ca-scanout'],
            '--long-run requires --stop-condition duration'
        )
        # Default stop condition is first-pixel, so --long-run without explicit --stop-condition duration must fail
        self.assert_validation_error(
            ['--output', '/tmp/dummy-out', '--long-run', '--seconds', '900'],
            '--long-run requires --stop-condition duration'
        )

    def test_long_run_duration_boundaries(self):
        # With --long-run and --stop-condition duration, seconds up to 1200 are accepted
        for sec in (0.1, 180.0, 180.001, 600.0, 900.0, 1199.9, 1200.0):
            _, args = self.parse_and_validate([
                '--output', '/tmp/dummy-out',
                '--long-run',
                '--stop-condition', 'duration',
                '--seconds', str(sec)
            ])
            self.assertTrue(args.long_run)
            self.assertEqual(args.stop_condition, 'duration')
            self.assertAlmostEqual(args.seconds, sec)

        # > 1200 with --long-run must fail
        for sec in (1200.001, 1201.0, 1500.0):
            self.assert_validation_error(
                ['--output', '/tmp/dummy-out', '--long-run', '--stop-condition', 'duration', '--seconds', str(sec)],
                'at most 1200 with --long-run'
            )

        # Non-finite and <= 0 with --long-run must also fail
        for arg in ('--seconds=0', '--seconds=-5', '--seconds=nan', '--seconds=inf', '--seconds=-inf'):
            self.assert_validation_error(
                ['--output', '/tmp/dummy-out', '--long-run', '--stop-condition', 'duration', arg],
                'finite positive'
            )

    def test_framebuffer_at_bounds(self):
        # Valid framebuffer-at within budget
        self.parse_and_validate(['--output', '/tmp/dummy-out', '--seconds', '180', '--framebuffer-at', '170'])
        self.parse_and_validate([
            '--output', '/tmp/dummy-out',
            '--long-run', '--stop-condition', 'duration',
            '--seconds', '900',
            '--framebuffer-at', '100',
            '--framebuffer-at', '850'
        ])

        # Exceeding budget or <= 0 must fail
        self.assert_validation_error(
            ['--output', '/tmp/dummy-out', '--seconds', '180', '--framebuffer-at', '190'],
            '--framebuffer-at must be positive and less than --seconds'
        )
        self.assert_validation_error(
            ['--output', '/tmp/dummy-out', '--seconds', '180', '--framebuffer-at', '0'],
            '--framebuffer-at must be positive and less than --seconds'
        )
        self.assert_validation_error(
            ['--output', '/tmp/dummy-out', '--seconds', '180', '--framebuffer-at=-1'],
            '--framebuffer-at must be positive and less than --seconds'
        )

    def test_step_count_bounds(self):
        self.parse_and_validate(['--output', '/tmp/dummy-out', '--step-count', '1'])
        self.parse_and_validate(['--output', '/tmp/dummy-out', '--step-count', '64'])
        self.assert_validation_error(['--output', '/tmp/dummy-out', '--step-count', '0'], 'between 1 and 64')
        self.assert_validation_error(['--output', '/tmp/dummy-out', '--step-count', '65'], 'between 1 and 64')

    def test_python_optimization_cannot_bypass_validation(self):
        """Verify python -O (assert stripped) still enforces validation via parser.error."""
        probe_py = str(Path(__file__).resolve().parents[1] / 'backend/display_probe.py')

        cases = [
            # Exceeding normal limit without --long-run
            (['-O', probe_py, '--output', '/tmp/nonexistent-dummy', '--seconds', '200'],
             b'without --long-run'),
            # NaN seconds
            (['-O', probe_py, '--output', '/tmp/nonexistent-dummy', '--seconds', 'nan'],
             b'finite positive number'),
            # Negative seconds
            (['-O', probe_py, '--output', '/tmp/nonexistent-dummy', '--seconds', '-10'],
             b'finite positive number'),
            # Exceeding 1200 with --long-run
            (['-O', probe_py, '--output', '/tmp/nonexistent-dummy', '--long-run', '--stop-condition', 'duration', '--seconds', '1250'],
             b'at most 1200 with --long-run'),
            # --long-run with wrong stop-condition
            (['-O', probe_py, '--output', '/tmp/nonexistent-dummy', '--long-run', '--stop-condition', 'first-pixel', '--seconds', '900'],
             b'--long-run requires --stop-condition duration'),
        ]

        for args, expected_err in cases:
            res = subprocess.run([sys.executable, *args], capture_output=True)
            self.assertEqual(res.returncode, 2, f"Expected exit code 2 under -O for {args}, got {res.returncode}")
            self.assertIn(expected_err, res.stderr, f"Expected {expected_err} in stderr for {args}")

    def test_run_clock_semantics_host_elapsed_excluding_pauses(self):
        """Verify RunClock tracks host monotonic elapsed time excluding pauses."""
        # Simulated host monotonic clock
        fake_time = [1000.0]
        clock = RunClock(now=lambda: fake_time[0])
        self.assertEqual(clock.elapsed(), 0.0)
        self.assertEqual(clock.wall(), 0.0)

        # Resume execution: 10s of active host time
        clock.resume()
        fake_time[0] += 10.0
        self.assertEqual(clock.elapsed(), 10.0)
        self.assertEqual(clock.wall(), 10.0)

        # Pause execution (e.g. debugger inspection / GDB hook / QMP freeze): 50s host time passes while paused
        clock.pause()
        fake_time[0] += 50.0
        # Active elapsed must still be 10.0 (paused interval excluded)
        self.assertEqual(clock.elapsed(), 10.0)
        # Wall time reflects full host time
        self.assertEqual(clock.wall(), 60.0)

        # Resume again: 5s more of active time
        clock.resume()
        fake_time[0] += 5.0
        self.assertEqual(clock.elapsed(), 15.0)
        self.assertEqual(clock.wall(), 65.0)


if __name__ == '__main__':
    unittest.main()
