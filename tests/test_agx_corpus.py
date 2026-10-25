"""The offline host harness and fast iteration tools for AGX replay."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import agx_corpus  # noqa: E402


class AGXCorpusHarnessTests(unittest.TestCase):
    def test_core_extraction(self):
        core = agx_corpus.extract_core()
        self.assertIn('static void agx_replay_frame(', core)
        self.assertIn('static bool agx_ctx_write(', core)
        self.assertIn('AGX_REPLAY_SKIP(', core)
        # Needs QEMU's TA ring state; the harness reads snapshots from the dump.
        self.assertNotIn('agx_ta_snapshot_page(', core)

    def test_harness_compiles(self):
        if not shutil.which('cc') or not (ROOT / 'backend/.venv/bin/pkg-config').exists():
            self.skipTest('host C compiler or backend pkg-config unavailable')
        with tempfile.TemporaryDirectory() as td:
            binary = agx_corpus.build_harness(Path(td))
            result = subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('usage', result.stderr)


class AGXCorpusIterationFastTests(unittest.TestCase):
    """Behavioral tests for fast iteration: filtering, prefix chaining, C-only, output preservation, and timings."""

    def test_parse_scenes_arg(self):
        self.assertIsNone(agx_corpus.parse_scenes_arg(None))
        self.assertIsNone(agx_corpus.parse_scenes_arg([]))
        self.assertEqual(agx_corpus.parse_scenes_arg(['settings']), ['settings'])
        self.assertEqual(agx_corpus.parse_scenes_arg(['settings', 'clock']), ['settings', 'clock'])
        self.assertEqual(agx_corpus.parse_scenes_arg(['settings,clock', 'calendar']), ['settings', 'clock', 'calendar'])
        # Order preservation & deduplication
        self.assertEqual(agx_corpus.parse_scenes_arg(['clock,settings', 'clock']), ['clock', 'settings'])

    def test_parse_frame_spec(self):
        m_single = agx_corpus.parse_frame_spec('5')
        self.assertTrue(m_single(5, 'frame-5'))
        self.assertFalse(m_single(4, 'frame-4'))
        self.assertFalse(m_single(6, 'frame-6'))

        m_named = agx_corpus.parse_frame_spec('frame-5')
        self.assertTrue(m_named(5, 'frame-5'))
        self.assertFalse(m_named(4, 'frame-4'))

        for spec in ('5..10', '5-10', '5:10'):
            m_range = agx_corpus.parse_frame_spec(spec)
            self.assertFalse(m_range(4, 'frame-4'))
            self.assertTrue(m_range(5, 'frame-5'))
            self.assertTrue(m_range(7, 'frame-7'))
            self.assertTrue(m_range(10, 'frame-10'))
            self.assertFalse(m_range(11, 'frame-11'))

        for spec in (':10', '-10', '..10'):
            m_open_start = agx_corpus.parse_frame_spec(spec)
            self.assertTrue(m_open_start(1, 'frame-1'))
            self.assertTrue(m_open_start(10, 'frame-10'))
            self.assertFalse(m_open_start(11, 'frame-11'))

        for spec in ('5:', '5..', '5-'):
            m_open_end = agx_corpus.parse_frame_spec(spec)
            self.assertFalse(m_open_end(4, 'frame-4'))
            self.assertTrue(m_open_end(5, 'frame-5'))
            self.assertTrue(m_open_end(20, 'frame-20'))

        m_multi = agx_corpus.parse_frame_spec('1, 3, 5..7')
        self.assertTrue(m_multi(1, 'frame-1'))
        self.assertFalse(m_multi(2, 'frame-2'))
        self.assertTrue(m_multi(3, 'frame-3'))
        self.assertFalse(m_multi(4, 'frame-4'))
        self.assertTrue(m_multi(6, 'frame-6'))

    def test_parse_frame_spec_rejects_invalid(self):
        invalid_specs = [
            '10..5',       # min > max
            '10-5',        # min > max
            'abc',         # non-numeric
            'frame-xyz',   # non-numeric stem
            '5-10-15',     # malformed range
            '..',          # empty range
            ':',           # empty range
            '5,,10',       # empty segment
        ]
        for bad in invalid_specs:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    agx_corpus.parse_frame_spec(bad)

    def test_resolve_report_dir_rejects_nonempty_existing(self):
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td) / 'my_existing_dir'
            output_dir.mkdir()
            precious = output_dir / 'report.txt'
            precious.write_text('important content')

            args = type('Args', (), {
                'corpus': Path(td),
                'output': output_dir,
                'unique_output': False,
            })()
            with self.assertRaises(FileExistsError):
                agx_corpus.resolve_report_dir(args)
            self.assertTrue(precious.exists(), 'Precious file must not be modified or deleted')

    def test_resolve_report_dir_atomic_allocation_fixed_time(self):
        with tempfile.TemporaryDirectory() as td:
            corpus = Path(td)
            args = type('Args', (), {
                'corpus': corpus,
                'output': None,
                'unique_output': True,
            })()
            with patch('time.strftime', return_value='20261025-120000'):
                dir1 = agx_corpus.resolve_report_dir(args)
                dir2 = agx_corpus.resolve_report_dir(args)
                dir3 = agx_corpus.resolve_report_dir(args)

            self.assertEqual(dir1.name, 'report-20261025-120000')
            self.assertEqual(dir2.name, 'report-20261025-120000-1')
            self.assertEqual(dir3.name, 'report-20261025-120000-2')
            self.assertTrue(dir1.exists() and dir2.exists() and dir3.exists())

    def test_output_preservation_never_deletes_existing(self):
        with tempfile.TemporaryDirectory() as td:
            corpus = Path(td)
            existing_report = corpus / 'report'
            existing_report.mkdir()
            precious_file = existing_report / 'precious_summary.json'
            precious_file.write_text('{"important": true}')

            args = type('Args', (), {
                'corpus': corpus,
                'output': None,
                'unique_output': False,
            })()
            resolved = agx_corpus.resolve_report_dir(args)
            self.assertTrue(precious_file.exists(), 'Existing report directory was modified or deleted!')
            self.assertNotEqual(resolved, existing_report)
            self.assertTrue(resolved.name.startswith('report-'))
            self.assertTrue(resolved.exists())

    def test_unknown_scene_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            corpus = Path(td)
            (corpus / 'clock' / 'dumps').mkdir(parents=True)
            args = type('Args', (), {
                'corpus': corpus,
                'scene': ['clock,clok'],
                'frames': None,
                'output': None,
                'unique_output': False,
            })()
            with patch('agx_corpus.build_harness', side_effect=AssertionError('invalid filters must not compile')):
                exit_code = agx_corpus.replay(args)
                self.assertEqual(exit_code, 1, 'Replay must reject unknown scenes even when one matches')

    def test_c_only_and_report_fields_integration(self):
        sibling_corpus = ROOT.parent / 'vphone-legacy/build/agx-corpus'
        if not (sibling_corpus / 'clock/dumps/frame-1.bin').exists():
            self.skipTest('Sibling clock corpus dumps unavailable')

        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / 'test_c_only_report'
            args = type('Args', (), {
                'corpus': sibling_corpus,
                'scene': ['clock'],
                'frames': '5',
                'c_only': True,
                'output': out_dir,
                'unique_output': False,
                'jobs': 2,
                'single': False,
            })()
            exit_code = agx_corpus.replay(args)
            self.assertEqual(exit_code, 0)

            summary_path = out_dir / 'summary.json'
            self.assertTrue(summary_path.exists())
            summary = json.loads(summary_path.read_text())

            totals = summary['totals']
            self.assertEqual(totals['frames'], 1)
            self.assertEqual(totals['selected_frames'], 1)
            self.assertEqual(totals['prefix_frames'], 4)
            self.assertEqual(totals['prefix_errors'], 0)
            self.assertTrue(totals['c_only'])
            self.assertEqual(totals['py_draws'], 0)
            self.assertEqual(totals['py_errors'], 0)

            # Separated timings
            timings = totals['timings']
            self.assertIn('build_s', timings)
            self.assertIn('c_replay_s', timings)
            self.assertIn('py_audit_s', timings)
            self.assertIn('export_s', timings)
            self.assertIn('total_s', timings)
            self.assertEqual(timings['py_audit_s'], 0.0)

            # Dependency prefix
            self.assertIn('clock', summary['dependency_prefix'])
            self.assertEqual(summary['dependency_prefix']['clock']['frames'],
                             ['frame-1', 'frame-2', 'frame-3', 'frame-4'])
            self.assertEqual(summary['dependency_prefix']['clock']['prefix_errors'], 0)

            # Frame record
            self.assertEqual(len(summary['frames']), 1)
            frame_obj = summary['frames'][0]
            self.assertEqual(frame_obj['frame'], 'frame-5')
            self.assertTrue(frame_obj['selected'])
            self.assertFalse(frame_obj['is_prefix'])
            self.assertIsNone(frame_obj['py_draws'])
            self.assertGreater(frame_obj['c']['carried_pages'], 0)

            # HTML exists and notes c-only
            html_text = (out_dir / 'index.html').read_text()
            self.assertIn('Python audit suppressed (--c-only)', html_text)
            self.assertIn('Timings:', html_text)

    def test_dependency_prefix_pixel_match(self):
        """Verify selected C pixels with prefix match full chained C replay."""
        sibling_corpus = ROOT.parent / 'vphone-legacy/build/agx-corpus'
        if not (sibling_corpus / 'clock/dumps/frame-1.bin').exists():
            self.skipTest('Sibling clock corpus dumps unavailable')

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            out_full = td / 'full'
            out_prefix = td / 'prefix'
            out_alone = td / 'alone'

            # 1. Full chain 1..10
            args_full = type('Args', (), {
                'corpus': sibling_corpus,
                'scene': ['clock'],
                'frames': '1..10',
                'c_only': True,
                'output': out_full,
                'unique_output': False,
                'jobs': 2,
                'single': False,
            })()
            self.assertEqual(agx_corpus.replay(args_full), 0)

            # 2. Selected frame 5 with dependency prefix (1..4)
            args_prefix = type('Args', (), {
                'corpus': sibling_corpus,
                'scene': ['clock'],
                'frames': '5',
                'c_only': True,
                'output': out_prefix,
                'unique_output': False,
                'jobs': 2,
                'single': False,
            })()
            self.assertEqual(agx_corpus.replay(args_prefix), 0)

            # 3. Selected frame 5 alone (single mode, no prefix)
            args_alone = type('Args', (), {
                'corpus': sibling_corpus,
                'scene': ['clock'],
                'frames': '5',
                'c_only': True,
                'output': out_alone,
                'unique_output': False,
                'jobs': 2,
                'single': True,
            })()
            self.assertEqual(agx_corpus.replay(args_alone), 0)

            full_hash = hashlib.sha256((out_full / 'clock-frame-5.png').read_bytes()).hexdigest()
            prefix_hash = hashlib.sha256((out_prefix / 'clock-frame-5.png').read_bytes()).hexdigest()
            alone_hash = hashlib.sha256((out_alone / 'clock-frame-5.png').read_bytes()).hexdigest()

            self.assertEqual(full_hash, prefix_hash, 'Selected frame with prefix must match full chain replay!')
            self.assertNotEqual(full_hash, alone_hash, 'Frame replayed alone lacks carried state and must not match!')

    def test_mocked_harness_failure_and_prefix_error_propagation(self):
        """Simulate harness failures and prerequisite errors without private firmware."""
        with tempfile.TemporaryDirectory() as td:
            corpus = Path(td)
            dumps_dir = corpus / 'testscene' / 'dumps'
            dumps_dir.mkdir(parents=True)
            for i in range(1, 4):
                (dumps_dir / f'frame-{i}.bin').write_bytes(b'DUMP')
                (dumps_dir / f'frame-{i}.json').write_text('{"vclock_ns": 0}')

            report_dir = corpus / 'test_report'

            # Case A: Harness exits with error code 1 even if lines were emitted
            mock_proc = MagicMock()
            mock_proc.returncode = 1
            mock_proc.stdout = '{"draws": 1, "skips": {}}\n'
            mock_proc.stderr = 'fatal harness crash'

            with patch('subprocess.run', return_value=mock_proc), \
                 patch('agx_corpus.build_harness', return_value=Path('/bin/true')):
                args = type('Args', (), {
                    'corpus': corpus,
                    'scene': ['testscene'],
                    'frames': '1',
                    'c_only': True,
                    'output': report_dir,
                    'unique_output': False,
                    'jobs': 1,
                    'single': False,
                })()
                exit_code = agx_corpus.replay(args)
                self.assertEqual(exit_code, 1, 'Nonzero harness returncode must cause nonzero exit')
                summary = json.loads((report_dir / 'summary.json').read_text())
                self.assertGreater(summary['totals']['c_errors'], 0)

            # Case B: Prefix pass fails (frame-1 fails, frame-2 selected)
            shutil.rmtree(report_dir)
            mock_proc_fail = MagicMock()
            mock_proc_fail.returncode = 0
            # frame-1 has an error, frame-2 succeeds
            mock_proc_fail.stdout = (
                '{"draws": 0, "skips": {}, "error": "broken texture decode"}\n'
                '{"draws": 5, "skips": {}}\n'
            )
            mock_proc_fail.stderr = ''

            # Create dummy rgba for frame-2
            def fake_run(cmd, *args, **kwargs):
                if '--chain' in cmd:
                    prefix = cmd[cmd.index('--chain') + 1]
                    # write prefix-1.rgba for frame-2
                    rgba = Path(f'{prefix}-1.rgba')
                    rgba.write_bytes(b'RGBA\x01\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00')
                    return mock_proc_fail
                return mock_proc_fail

            with patch('subprocess.run', side_effect=fake_run), \
                 patch('agx_corpus.build_harness', return_value=Path('/bin/true')):
                args2 = type('Args', (), {
                    'corpus': corpus,
                    'scene': ['testscene'],
                    'frames': '2',
                    'c_only': True,
                    'output': report_dir,
                    'unique_output': False,
                    'jobs': 1,
                    'single': False,
                })()
                exit_code2 = agx_corpus.replay(args2)
                self.assertEqual(exit_code2, 1, 'Prerequisite prefix failure must cause nonzero exit')
                summary2 = json.loads((report_dir / 'summary.json').read_text())
                self.assertEqual(summary2['totals']['prefix_errors'], 1)
                self.assertIn('Dependency prerequisite error', summary2['frames'][0]['c']['error'])

            # A skipped prerequisite draw also invalidates dependent pixels.
            shutil.rmtree(report_dir)
            mock_proc_fail.stdout = (
                '{"draws": 0, "skips": {"unsupported blend": 1}}\n'
                '{"draws": 5, "skips": {}}\n'
            )
            with patch('subprocess.run', side_effect=fake_run), \
                 patch('agx_corpus.build_harness', return_value=Path('/bin/true')):
                self.assertEqual(agx_corpus.replay(args2), 1)
                summary3 = json.loads((report_dir / 'summary.json').read_text())
                self.assertEqual(summary3['totals']['prefix_skips'], {'unsupported blend': 1})
                self.assertIn('skipped 1 prerequisite draw', summary3['frames'][0]['c']['error'])


if __name__ == '__main__':
    unittest.main()
