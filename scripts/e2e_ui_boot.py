#!/usr/bin/env python3
"""Opt-in orchestrator gate: local IPSW -> UI -> three cold launches. Never imported to boot."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from qemu.boot_check import classify_boot
from ui_profile import prepare_ui


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ipsw', type=Path, default=Path('iPhone6,2_7.1.2_11D257_Restore.ipsw'))
    p.add_argument('--name', default='ui-e2e')
    p.add_argument('--bundle', type=Path, required=True,
                   help='Actual bundle path under VPHONE_LEGACY_VMS_DIR')
    p.add_argument('--seconds', type=int, default=180)
    a = p.parse_args()
    if a.bundle.name != a.name:
        p.error('--bundle basename must equal --name')
    os.environ['VPHONE_LEGACY_VMS_DIR'] = str(a.bundle.resolve().parent)
    root = Path(__file__).resolve().parents[1]
    cli = root / 'vphone-legacy'
    subprocess.run([str(cli), 'vm', 'create', a.name, '--ios', '7.1.2', '--ipsw', str(a.ipsw.resolve())], check=True)
    prepare_ui(a.bundle, ipsw=a.ipsw)
    for attempt in range(1, 4):
        subprocess.run([str(cli), 'vm', 'launch', a.name, '--headless', '--probe-seconds', str(a.seconds)], check=True)
        runtime = json.loads((a.bundle / 'runtime.json').read_text())
        logs = Path(runtime['log_directory'])
        stage = classify_boot((logs / 'serial.log').read_text(errors='replace'),
                              (logs / 'framebuffer.ppm').read_bytes())
        print(json.dumps(dict(attempt=attempt, boot_stage=stage, logs=str(logs))))
        if stage != 'home':
            raise RuntimeError(f'Cold boot {attempt}: expected home, got {stage}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
