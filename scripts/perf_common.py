"""Shared live-run lifecycle. Importing this module never launches a guest."""
import errno
import fcntl
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
import live_run
from display_probe import FB

PROFILE = ROOT / 'build/virtual-activated-profile-v5'
LOCK = Path('/tmp/vphone-perf-live.lock')


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH


class LiveLock:
    """Atomic directory lock; legacy owner-only locks are never auto-reaped."""
    def __init__(self, owner, path=LOCK):
        self.path, self.owner, self.held = Path(path), str(owner), False

    @contextmanager
    def guard(self):
        # Serialize new-protocol stale recovery as well as acquire/release.
        # Keep this inode permanently: unlinking flock files creates races.
        with self.path.with_name(self.path.name + '.guard').open('a') as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(guard, fcntl.LOCK_UN)

    def acquire(self):
        while True:
            with self.guard():
                try:
                    self.path.mkdir()
                except FileExistsError:
                    try:
                        pid = int((self.path / 'pid').read_text())
                        if pid > 0 and not pid_alive(pid):
                            shutil.rmtree(self.path)
                            continue
                    except (OSError, ValueError):
                        pass
                else:
                    try:
                        (self.path / 'owner').write_text(self.owner)
                        (self.path / 'pid').write_text(str(os.getpid()))
                    except BaseException:
                        shutil.rmtree(self.path)
                        raise
                    self.held = True
                    return self
            print('waiting for another live run', flush=True)
            time.sleep(10)

    def release(self):
        with self.guard():
            if self.held:
                if (self.path / 'pid').read_text() == str(os.getpid()):
                    shutil.rmtree(self.path)
                self.held = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *args):
        self.release()


def environment(touch_script=''):
    return {**os.environ, 'VPHONE_TOUCH_SCRIPT': touch_script,
            'VPHONE_PERF_STATS': '1', 'VPHONE_PLAYER_STATS': '1'}


def prepare(work, binary=None, extra=(), storage=None):
    plan = json.loads((PROFILE / 'boot/boot_plan.json').read_text())
    storage = storage or PROFILE / 'storage-before'
    if not storage.exists():
        storage = ROOT / 'build/ios712-validated'
    images = work / 'images'
    images.mkdir()
    for kind, src in [('system', PROFILE / 'system.raw'), ('data', PROFILE / 'data.raw'),
                      ('utility', storage / 'utility.raw')]:
        live_run.clone_or_copy(src.resolve(), images / f'{kind}.raw')
    machine = (f"s5l8960x,boot-el=3,boot-entry={plan['initial_entry_pa']:#x},"
               f"boot-args={plan['initial_boot_args_pa']:#x},framebuffer={FB:#x},"
               f"system-image={images}/system.raw,data-image={images}/data.raw,"
               f"utility-image={images}/utility.raw,system-writable=1")
    cmd = [str(binary or ROOT / 'backend/bin/qemu-system-aarch64'), '-L',
           str(ROOT / 'backend/upstream/pc-bios'), '-machine', machine,
           '-accel', 'tcg,thread=' + os.environ.get('VPHONE_TCG_THREAD', 'multi'),
           '-smp', str(plan.get('cpu_count', 1)), '-m', '1024M', '-kernel', plan['image'],
           '-display', 'none', '-vnc', f'unix:{work}/vnc', '-serial', f'file:{work}/serial.log',
           '-qmp', f'unix:{work}/qmp,server=on,wait=off', *extra]
    for image in plan.get('auxiliary_images', []):
        cmd += ['-device', f"loader,file={image['path']},addr={image['address']:#x},force-raw=on"]
    return cmd


def stop(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


class LiveSession:
    def __init__(self, out, binary=None, touch_script=''):
        self.out, self.binary, self.touch_script = Path(out), binary, touch_script
        self.qemu = self.player = None
        self.logs = []

    def __enter__(self):
        self.out.mkdir(parents=True, exist_ok=True)
        self.work = Path(tempfile.mkdtemp(prefix='vphone-perf-', dir='/tmp'))
        try:
            cmd = prepare(self.work, self.binary)
            player = live_run.build_player()
            env = environment(self.touch_script)
            self.logs = [(self.out / name).open('wb') for name in ('qemu-stderr.log', 'player-stderr.log')]
            self.start = time.monotonic()
            self.qemu = subprocess.Popen(cmd, env=env, stdout=self.logs[0], stderr=subprocess.STDOUT)
            deadline = self.start + 30
            while not (self.work / 'vnc').exists():
                if self.qemu.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('QEMU failed to open VNC; inspect qemu-stderr.log')
                time.sleep(.05)
            self.player = subprocess.Popen([str(player), str(self.work / 'vnc'), str(self.work / 'qmp')],
                                           env=env, stderr=self.logs[1])
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        stop(self.player)
        stop(self.qemu)
        for log in self.logs:
            log.close()
        if (self.work / 'serial.log').exists():
            shutil.copyfile(self.work / 'serial.log', self.out / 'serial.log')
        shutil.rmtree(self.work, ignore_errors=True)
