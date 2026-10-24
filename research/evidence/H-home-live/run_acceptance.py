from pathlib import Path
import argparse
import json
import os
import re
import subprocess
import time

root = Path(__file__).resolve().parent
repo = root.parents[2]
python = repo / 'backend/.venv/bin/python'
parser = argparse.ArgumentParser()
parser.add_argument('--prefix', default='accept-boot')
parser.add_argument('boots', type=int, nargs='+')
args = parser.parse_args()
assert Path(args.prefix).name == args.prefix, 'prefix must be a directory name'
for number in args.boots:
    name = f'{args.prefix}{number}'
    output = root / name
    assert not output.exists(), f'require fresh output: {output}'
    while True:
        load = subprocess.check_output(['uptime'], text=True).strip()
        value = float(re.search(r'load averages?:\s*([0-9.]+)', load)[1])
        if value < 5:
            break
        print('waiting for load < 5:', load, flush=True)
        time.sleep(15)
    (root / f'{name}-load.txt').write_text(load + '\n')
    print(f'boot {number} starting: {load}', flush=True)
    env = os.environ.copy()
    env.update(VPHONE_DISPLAY='cocoa', VPHONE_TCG_THREAD='multi',
               VPHONE_TOUCH_SCRIPT=(root / 'awake-touch-script.txt').read_text().strip())
    args = [str(python), 'backend/display_probe.py',
            '--plan', 'build/virtual-activated-profile-v5/boot/boot_plan.json',
            '--diagnostic-system', 'build/virtual-activated-profile-v5/system.raw',
            '--diagnostic-data', 'build/virtual-activated-profile-v5/data.raw',
            '--seconds', '155', '--stop-condition', 'duration', '--output', str(output)]
    for second in (90, 105, 140, 152):
        args.extend(['--framebuffer-at', str(second)])
    with (root / f'{name}-run.log').open('w') as log:
        subprocess.run(args, cwd=repo, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
    assert json.loads((output / 'process-exit.json').read_text())['reaped']
    subprocess.run([str(python), 'backend/probe_cleanup.py', str(output)], cwd=repo, check=True)
    for second in (90, 105, 140, 152):
        image = output / f'framebuffer-at-{second}s.ppm'
        subprocess.run(['sips', '-s', 'format', 'png', str(image), '--out', str(image.with_suffix('.png'))],
                       check=True, stdout=subprocess.DEVNULL)
    print(f'boot {number} finished; images cleaned', flush=True)
