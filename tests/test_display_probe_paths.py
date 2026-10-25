"""Evidence referenced by user hook JSON must resolve in either checkout."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import display_probe


class DisplayProbePathTests(unittest.TestCase):
    def test_local_precedence_main_fallback_and_absolute_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local, main = root / 'worktree', root / 'main'
            local.mkdir(); main.mkdir()
            relative = Path('backend/evidence/cache-index.json')
            for tree in (local, main):
                (tree / relative).parent.mkdir(parents=True)
            (main / relative).write_text('{}')
            with patch.object(display_probe, 'ROOT', local), patch.object(display_probe, 'MAIN_CHECKOUT', main):
                self.assertEqual(display_probe.resolve_hook_path(str(relative)), main / relative)
                (local / relative).write_text('{}')
                self.assertEqual(display_probe.resolve_hook_path(str(relative)), local / relative)
                self.assertEqual(display_probe.resolve_hook_path('/explicit/missing'), Path('/explicit/missing'))
                self.assertEqual(display_probe.resolve_hook_path('missing'), local / 'missing')

    def test_hook_references_are_normalized_before_any_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local, main = root / 'worktree', root / 'main'
            local.mkdir(); main.mkdir()
            for name in ('cache', 'index', 'SpringBoard'):
                (main / name).write_text('{}')
            hooks = local / 'hooks.json'
            hooks.write_text(json.dumps({'cache_path': 'cache', 'index_path': 'index',
                                         'exe_paths': {'SpringBoard': 'SpringBoard'},
                                         'symbols': {'unchanged': '0x123'}}))
            with patch.object(display_probe, 'ROOT', local), patch.object(display_probe, 'MAIN_CHECKOUT', main):
                result = display_probe.load_user_hooks(hooks)
            self.assertEqual(result['cache_path'], str(main / 'cache'))
            self.assertEqual(result['index_path'], str(main / 'index'))
            self.assertEqual(result['exe_paths']['SpringBoard'], str(main / 'SpringBoard'))
            self.assertEqual(result['symbols'], {'unchanged': '0x123'})
