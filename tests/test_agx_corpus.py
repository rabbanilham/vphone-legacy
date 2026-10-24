"""The offline host harness must keep compiling the renderer QEMU runs."""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import agx_corpus  # noqa: E402


"""The offline host harness and fast iteration tools for AGX replay."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

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
        self.assertEqual(agx_corpus.parse_scenes_arg(['settings']), {'settings'})
        self.assertEqual(agx_corpus.parse_scenes_arg(['settings', 'clock']), {'settings', 'clock'})
        self.assertEqual(agx_corpus.parse_scenes_arg(['settings,clock', 'calendar']), {'settings', 'clock', 'calendar'})

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

    def test_output_unique_and_explicit(self):
        with tempfile.TemporaryDirectory() as td:
            corpus = Path(td)
            args_unique = type('Args', (), {
                'corpus': corpus,
                'output': None,
                'unique_output': True,
            })()
            out1 = agx_corpus.resolve_report_dir(args_unique)
            self.assertTrue(out1.name.startswith('report-'))

            custom_dir = corpus / 'custom_output_dir'
            args_explicit = type('Args', (), {
                'corpus': corpus,
                'output': custom_dir,
                'unique_output': False,
            })()
            out2 = agx_corpus.resolve_report_dir(args_explicit)
            self.assertEqual(out2, custom_dir)
            self.assertTrue(custom_dir.exists())

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
            self.assertEqual(summary['dependency_prefix']['clock'],
                             ['frame-1', 'frame-2', 'frame-3', 'frame-4'])

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


if __name__ == '__main__':
    unittest.main()
