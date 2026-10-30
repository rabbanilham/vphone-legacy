#!/usr/bin/env python3
"""Input-to-pixel latency on the native player's path (QEMU VNC socket).

  input_latency.py [--qemu-binary BIN] [--swipes N] [--out DIR]

Boots the virtual-activated profile the way backend/live_run.py does
(-display none -vnc unix:...), but uses a Python RFB client in place of the
Swift player. The touch script unlocks to Home. Then home-screen page swipes
are sent as VNC pointer events, which is what the player does for a mouse
drag. A small threaded RFB reader timestamps every framebuffer update. For
each swipe it reports the time from the first pointer move to the first
update that changes the icon area, and from the pointer release to the last
change (the page settling). Takes the same host-wide lock as perf_live.sh.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from display_probe import FB  # noqa: E402
from perf_common import LiveLock, prepare, environment, stop

PROFILE = ROOT / 'build/virtual-activated-profile-v5'
UNLOCK = ('at=78;tap 600,900|at=86;tap 600,900|at=94;tap 600,900|'
          'at=95;down 40,1036;move 600,1036 dur=0.7 steps=42;up|at=103;tap 600,900')
W, H = 640, 1136
LOCK = Path('/tmp/vphone-perf-live.lock')


class RFB:
    def __init__(self, path, rgb_only=False):
        self.rgb_only = rgb_only
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        self.fb = bytearray(W * H * 4)
        self.updates = []       # (host time, rows touched in the icon area, changed bytes)
        self.lock = threading.Lock()
        self.send_lock = threading.Lock()

    def read(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise EOFError
            buf += chunk
        return bytes(buf)

    def send(self, data):
        with self.send_lock:
            self.sock.sendall(data)

    def handshake(self):
        self.read(12)
        self.send(b'RFB 003.008\n')
        n = self.read(1)[0]
        self.read(n)
        self.send(b'\x01')
        self.read(4)
        self.send(b'\x01')
        hdr = self.read(24)
        self.read(struct.unpack('>I', hdr[20:24])[0])
        # Same pixel format and Raw-only encoding as sources/vphone-player.
        self.send(bytes([0, 0, 0, 0, 32, 24, 0, 1, 0, 255, 0, 255, 0, 255, 16, 8, 0, 0, 0, 0]))
        self.send(bytes([2, 0, 0, 1, 0, 0, 0, 0]))
        self.request(False)

    def request(self, incremental):
        self.send(struct.pack('>BBHHHH', 3, 1 if incremental else 0, 0, 0, W, H))

    def pointer(self, x, y, down):
        self.send(struct.pack('>BBHH', 5, 1 if down else 0, x, y))

    def loop(self):
        try:
            while True:
                t = self.read(1)[0]
                if t == 2:
                    continue
                if t == 3:
                    h = self.read(7)
                    self.read(struct.unpack('>I', h[3:7])[0])
                    continue
                count = struct.unpack('>xH', self.read(3))[0]
                changed = 0
                for _ in range(count):
                    x, y, w, h, enc = struct.unpack('>HHHHi', self.read(12))
                    data = self.read(w * h * 4)
                    for row in range(h):
                        yy = y + row
                        off = (yy * W + x) * 4
                        new = data[row * w * 4:(row + 1) * w * 4]
                        # Icon grid only: skip the status bar clock and the dock.
                        if 120 <= yy < 900:
                            old = self.fb[off:off + w * 4]
                            differs = (any(old[c::4] != new[c::4] for c in range(3))
                                       if self.rgb_only else old != new)
                            if differs:
                                changed += 1
                        self.fb[off:off + w * 4] = new
                with self.lock:
                    self.updates.append((time.monotonic(), changed))
                self.request(True)
        except (EOFError, OSError):
            pass


class QMP:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.file = self.sock.makefile('rwb')
        self.file.readline()
        self.execute('qmp_capabilities')

    def execute(self, command, arguments=None):
        msg = {'execute': command} | ({'arguments': arguments} if arguments else {})
        self.file.write(json.dumps(msg).encode() + b'\n')
        self.file.flush()
        while True:
            reply = json.loads(self.file.readline())
            if 'return' in reply or 'error' in reply:
                return reply.get('return', reply.get('error'))

    def touch_ready(self):
        return self.execute('qom-get', {'path': '/machine', 'property': 'touch-reporting-enabled'}) is True


def swipe(rfb, x0, x1, y=600, steps=12, dt=0.016):
    rfb.pointer(x0, y, True)
    time.sleep(dt)
    t_move = time.monotonic()
    for i in range(1, steps + 1):
        rfb.pointer(int(x0 + (x1 - x0) * i / steps), y, True)
        time.sleep(dt)
    rfb.pointer(x1, y, False)
    return t_move, time.monotonic()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--qemu-binary', type=Path, default=ROOT / 'backend/bin/qemu-system-aarch64')
    p.add_argument('--swipes', type=int, default=10)
    p.add_argument('--home-at', type=float, default=115, help='host seconds after launch to start swiping')
    p.add_argument('--out', type=Path)
    p.add_argument('--qemu-arg', action='append', default=[])
    args = p.parse_args()

    live_lock = LiveLock('input_latency').acquire()
    work = Path(tempfile.mkdtemp(prefix='vpl-lat-', dir='/tmp'))
    qemu = None
    try:
        cmd = prepare(work, args.qemu_binary, args.qemu_arg)
        env = environment(UNLOCK)
        start = time.monotonic()
        qemu = subprocess.Popen(cmd, env=env, stdout=(work / 'qemu.log').open('wb'), stderr=subprocess.STDOUT)
        while not (work / 'vnc').exists():
            if qemu.poll() is not None:
                raise RuntimeError((work / 'qemu.log').read_text()[-2000:])
            time.sleep(0.05)
        rfb = RFB(str(work / 'vnc'))
        rfb.handshake()
        threading.Thread(target=rfb.loop, daemon=True).start()
        while time.monotonic() - start < args.home_at:
            time.sleep(0.5)

        def dump(name):
            if args.out:
                args.out.mkdir(parents=True, exist_ok=True)
                rgb = bytearray(W * H * 3)
                rgb[0::3], rgb[1::3], rgb[2::3] = rfb.fb[2::4], rfb.fb[1::4], rfb.fb[0::4]
                (args.out / f'{name}.ppm').write_bytes(b'P6\n%d %d\n255\n' % (W, H) + bytes(rgb))
        qmp = QMP(str(work / 'qmp'))
        print(f'updates so far: {len(rfb.updates)}; digitizer ready: {qmp.touch_ready()}', flush=True)
        dump('before')
        results = []
        for i in range(args.swipes):
            # Settle: no icon-area change for 1.5 s, so the VNC refresh is idle.
            quiet_since = time.monotonic()
            while time.monotonic() - quiet_since < 1.5:
                with rfb.lock:
                    last = max((t for t, c in rfb.updates if c), default=0)
                quiet_since = max(quiet_since, last)
                time.sleep(0.05)
            woke = False
            while not qmp.touch_ready():
                if not woke:
                    # Same wake as the player: Home key over VNC (keysym 0xff50).
                    rfb.send(bytes([4, 1, 0, 0, 0, 0, 0xff, 0x50]))
                    time.sleep(0.1)
                    rfb.send(bytes([4, 0, 0, 0, 0, 0, 0xff, 0x50]))
                    woke = True
                time.sleep(0.1)
            if woke:
                print('digitizer was gated; woke it with Home', flush=True)
                time.sleep(1.5)
            left = i % 2 == 0
            t_move, t_up = swipe(rfb, 520 if left else 120, 120 if left else 520)
            time.sleep(2.0)
            with rfb.lock:
                changes = [t for t, c in rfb.updates if c and t >= t_move]
            first = changes[0] - t_move if changes else None
            settle = changes[-1] - t_up if changes else None
            results.append({'swipe': i, 'direction': 'left' if left else 'right',
                            'first_change_ms': first and round(first * 1000, 1),
                            'settle_after_up_ms': settle and round(settle * 1000, 1),
                            'updates': len(changes)})
            print(json.dumps(results[-1]), flush=True)
            if i == 0:
                dump('after-first-swipe')

        firsts = [r['first_change_ms'] for r in results if r['first_change_ms'] is not None]
        settles = [r['settle_after_up_ms'] for r in results if r['settle_after_up_ms'] is not None]
        summary = {'binary': str(args.qemu_binary), 'swipes': len(results), 'responded': len(firsts),
                   'first_change_ms_median': firsts and statistics.median(firsts),
                   'first_change_ms_max': firsts and max(firsts),
                   'settle_after_up_ms_median': settles and statistics.median(settles),
                   'updates_per_swipe_median': statistics.median(r['updates'] for r in results)}
        print('SUMMARY ' + json.dumps(summary), flush=True)
        if args.out:
            args.out.mkdir(parents=True, exist_ok=True)
            (args.out / 'latency.json').write_text(json.dumps({'summary': summary, 'swipes': results}, indent=1))
            shutil.copy(work / 'serial.log', args.out / 'serial.log')
        return 0 if firsts else 1
    finally:
        stop(qemu)
        shutil.rmtree(work, ignore_errors=True)
        live_lock.release()


if __name__ == '__main__':
    sys.exit(main())
