import io
import json
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from cli_json import Emitter, StageID, parse_ndjson

FIXTURES = Path(__file__).parent / 'fixtures/gui'


class Events(unittest.TestCase):
    def test_fixture_roundtrip(self):
        for fixture in FIXTURES.glob('*.ndjson'):
            events = parse_ndjson(fixture.read_text())
            output = io.StringIO()
            emitter = Emitter(output)
            for value in events:
                emitter.emit(value['event'], **{k: v for k, v in value.items() if k not in ('event', 'schema')})
            self.assertEqual(parse_ndjson(output.getvalue()), events)
            self.assertEqual(sum(e['event'] == 'result' for e in events), 1)

    def test_threads_and_final_result(self):
        output = io.StringIO()
        emitter = Emitter(output)
        threads = [threading.Thread(target=lambda: [emitter.log('hello') for _ in range(30)]) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        emitter.stage(StageID.PREFLIGHT, 'done', fraction=1)
        emitter.result(True, {})
        self.assertEqual(len(parse_ndjson(output.getvalue())), 122)
        with self.assertRaises(ValueError):
            emitter.result(True, {})

    def test_invalid_fraction(self):
        with self.assertRaises(ValueError):
            Emitter(io.StringIO()).stage('extract', 'progress', fraction=float('nan'))
