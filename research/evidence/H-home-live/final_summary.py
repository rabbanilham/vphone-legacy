from pathlib import Path
import hashlib
import json
import gzip
import re

root = Path(__file__).resolve().parent
rows = {}

def static_home_changes(folder):
    def pixels(second):
        raw = (folder / f'framebuffer-at-{second}s.ppm').read_bytes()
        data = raw.split(b'\n255\n', 1)[1]
        assert len(data) == 640 * 1136 * 3
        return data
    first, last = pixels(105), pixels(140)
    return sum(first[(y * 640 + x) * 3:(y * 640 + x) * 3 + 3] !=
               last[(y * 640 + x) * 3:(y * 640 + x) * 3 + 3]
               for y in range(40, 1136) for x in range(640)
               if not (184 <= x < 304 and 226 <= y < 346))

for run in ('accept-boot1', 'accept-boot2', 'accept-boot3'):
    folder = root / run
    if not (folder / 'diagnostic-disk-cleanup.json').exists():
        continue
    provenance = json.loads((folder / 'provenance.json').read_text())
    cleanup = json.loads((folder / 'diagnostic-disk-cleanup.json').read_text())
    trace = folder / 'qemu-trace.log'
    opener = open
    if not trace.exists():
        trace = trace.with_suffix('.log.gz')
        opener = gzip.open
    counts = dict(replay=0, rt_queue=0)
    swipe = []
    with opener(trace, 'rt') as source:
        for line in source:
            counts['replay'] += 'AGX replay target' in line
            counts['rt_queue'] += 'AGX RT queue' in line
            if 'A7 TOUCH step' in line and 'y=1036 ' in line and ('state=1 ' in line or 'state=7 ' in line):
                swipe.append(line.strip())
    captures = json.loads((folder / 'framebuffer-captures.json').read_text())
    for item in captures:
        image = Path(item['path']).with_suffix('.png')
        item['path'] = str(image.relative_to(root))
        item['sha256'] = hashlib.sha256(image.read_bytes()).hexdigest()
    rows[run] = dict(
        observations=json.loads((folder / 'observations.json').read_text()),
        exit=json.loads((folder / 'process-exit.json').read_text()),
        counts=counts, swipe=swipe, captures=captures,
        changed_static_home_pixels_105_to_140=static_home_changes(folder),
        binary_sha256=provenance['binary_sha256'],
        source_sha256=provenance['source_sha256'],
        input_sha256={key: item['sha256'] for key, item in provenance['inputs'].items()},
        images_deleted=not (folder / 'images').exists(),
        source_images_unchanged=all(item['source_unchanged'] for item in cleanup.values()),
        launch_load=(root / f'{run}-load.txt').read_text().strip(),
        manual_review=json.loads((root / 'manual-review.json').read_text()).get(run, {}))
(root / 'final-summary.json').write_text(json.dumps(dict(
    base='ae427fc', settle_frames=18,
    tests=dict(unit_total=232, skipped=7, native_agx='PASS', touch_input='PASS', spi_touch='PASS'),
    touch_script=(root / 'awake-touch-script.txt').read_text().strip(),
    runs=rows), indent=2) + '\n')
for run, row in rows.items():
    print(run, row['counts'], row['observations'], 'cleaned', row['images_deleted'])
