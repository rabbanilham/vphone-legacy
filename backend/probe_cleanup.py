"""Remove only one reaped probe's independent disk copies, retaining hashes."""
import argparse,json,shutil
from pathlib import Path
from display_probe import digest,save_json

def cleanup(root):
    root=Path(root).resolve();exited=json.loads((root/'process-exit.json').read_text())
    assert exited['reaped'];provenance=json.loads((root/'provenance.json').read_text());record={}
    if not (root/'images').exists():
        recorded=json.loads((root/'diagnostic-disk-cleanup.json').read_text())
        assert all(v['source_unchanged'] for v in recorded.values())
        return
    for kind,entry in provenance['inputs'].items():
        target=Path(entry['copy']).resolve();assert target.is_relative_to(root/'images')
        record[kind]=dict(source=str(entry['source']),source_unchanged=digest(entry['source'])==entry['sha256'],copy_post_sha256=digest(target))
    assert all(v['source_unchanged'] for v in record.values())
    save_json(root/'diagnostic-disk-cleanup.json',record);shutil.rmtree(root/'images')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('output');cleanup(p.parse_args().output)
