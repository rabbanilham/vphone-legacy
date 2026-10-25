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


if __name__ == '__main__':
    unittest.main()
