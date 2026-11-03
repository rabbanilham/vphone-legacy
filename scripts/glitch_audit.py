#!/usr/bin/env python3
"""Scripted UI glitch audit: drive a booted UI bundle over QMP and capture evidence.

  glitch_audit.py run --bundle NAME|PATH --out DIR [--scenarios a,b,c] [--use-running]
  glitch_audit.py analyze DIR [--stuck N]
  glitch_audit.py list

`run` boots an ephemeral copy of the bundle (APFS clone under DIR/_work, so the
source bundle and any VM of the same name are never launched or written) and
drives timed scenarios with QMP `input-send-event` on device `a7-touch-display`
(the same mtt events and qcodes the native player sends), taking `screendump`
after every `shot` step. Frames land in DIR/<scenario>/NN-label.png and timings in
DIR/manifest.json. With --use-running no VM is booted: the script attaches to the
`control.launcher_qmp` socket from `vphone-legacy vm status NAME --json`. The
launcher normally holds that socket (one client at a time), so this only works
if the launcher is not connected; the default ephemeral boot has no such limit.

`analyze` runs the heuristics at the bottom of this file over DIR/*/NN-label.png
and writes DIR/report.json and DIR/report.html (a contact sheet of flagged frames).
Pure stdlib; Pillow is used for image IO when installed.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import zlib

REPO = Path(__file__).resolve().parents[1]
W, H = 640, 1136
DEVICE = 'a7-touch-display'

# ---------------------------------------------------------------------------
# Tunable coordinates (640x1136 guest pixels). The iOS 7 home grid is 4 columns
# by 5 rows plus a dock; these are first estimates, adjust after looking at the
# captured home frame.
# ---------------------------------------------------------------------------
COLS = (92, 244, 396, 548)             # icon centre x (measured on the iOS 7.1.2 5s home frame)
ROWS = (110, 286, 462, 638, 814)       # icon centre y (rows 1..5)
DOCK_Y = 1030
ICON = 120                             # icon tile size in px
COORDS = {
    'unlock_from': (30, 1000),         # lock slider row: starts below y~990 or the slide is ignored
    'unlock_to': (630, 1000),
    'clock_icon': (COLS[0], ROWS[1]),  # Clock: row 2, col 1
    'settings_icon': (COLS[0], ROWS[4]),   # Settings: row 5, col 1
    'safari_icon': (COLS[2], DOCK_Y),  # Safari: dock slot 3
    # A folder icon, if the layout has one. None = no folder: the scenario long-presses
    # the first icon instead (jiggle mode), which exercises the same icon animations.
    'folder_icon': None,
    'long_press_icon': (COLS[0], ROWS[0]),
    'folder_close': (320, 1090),       # tap below the folder sheet to close
    'safari_address_bar': (300, 84),
    'nc_from': (320, 4), 'nc_to': (320, 800),        # pull Notification Center down
    'cc_from': (320, 1131), 'cc_to': (320, 600),     # pull Control Center up
    'settings_row_top': (320, 330),
}
ICON_CELLS = [(x, y) for y in ROWS for x in COLS] + [(x, DOCK_Y) for x in COLS]


def S(**kw):
    return kw


def tap(x, y, hold=0.06): return ('tap', x, y, hold)
def swipe(x1, y1, x2, y2, ms): return ('swipe', x1, y1, x2, y2, ms)
def key(qcode): return ('key', qcode)
def wait(s): return ('wait', s)
def shot(label): return ('shot', label)


c = COORDS
SCENARIOS = {
    # start: 'lock' = needs the lock screen, 'home' = needs the home screen (unlocks if needed).
    'lockscreen': dict(start='lock', steps=[shot('initial'), wait(2), shot('after-2s'),
                                            key('power'), wait(1.5), shot('slept'),
                                            key('power'), wait(2.5), shot('woke')]),
    'unlock-slide': dict(start='lock', steps=[
        key('home'), wait(2), shot('woke'),
        ('swipe', *c['unlock_from'], *c['unlock_to'], 1200), shot('mid-unlock-0'), wait(0.3),
        shot('mid-unlock-1'), wait(2), shot('unlocked')]),
    'home': dict(start='home', steps=[shot('home'), wait(2), shot('home-2s')]),
    'folder-open-close': dict(start='home', steps=(
        [shot('before'), tap(*c['folder_icon']), shot('opening-0'), wait(0.25), shot('opening-1'),
         wait(1.5), shot('open'), tap(*c['folder_close']), shot('closing-0'), wait(0.25),
         shot('closing-1'), wait(1.5), shot('closed')] if c['folder_icon'] else
        [shot('before'), tap(*c['long_press_icon'], hold=1.4), shot('jiggle-0'), wait(0.25),
         shot('jiggle-1'), wait(0.25), shot('jiggle-2'), key('home'), shot('exit-0'), wait(0.3),
         shot('exit-1'), wait(1.5), shot('closed')])),
    'settings-switches': dict(start='home', steps=[
        tap(*c['settings_icon']), wait(4), shot('settings'),
        swipe(320, 800, 320, 400, 400), wait(1), shot('scrolled'),
        tap(*c['settings_row_top']), wait(2), shot('row'), key('home'), wait(2)]),
    'safari-keyboard': dict(start='home', steps=[
        tap(*c['safari_icon']), wait(9), shot('safari'), wait(3), shot('safari-3s'),
        tap(*c['safari_address_bar']), shot('keyboard-0'), wait(0.3), shot('keyboard-1'),
        wait(1.5), shot('keyboard'), key('home'), wait(2)]),
    'notification-center': dict(start='home', steps=[
        shot('before'), swipe(*c['nc_from'], *c['nc_to'], 500), shot('pulled'), wait(1.5),
        shot('settled'), swipe(320, 800, 320, 100, 400), wait(1.5), shot('closed')]),
    'nc-repeat': dict(start='home', steps=[
        swipe(*c['nc_from'], *c['nc_to'], 500), wait(2), shot('settled-a'), swipe(320, 800, 320, 100, 400), wait(2),
        swipe(*c['nc_from'], *c['nc_to'], 500), wait(3), shot('settled-b'), swipe(320, 800, 320, 100, 400), wait(2),
        swipe(*c['nc_from'], *c['nc_to'], 500), wait(5), shot('settled-c'), swipe(320, 800, 320, 100, 400), wait(2)]),
    'nc-dump': dict(start='home', steps=[
        ('dumpon',), swipe(*c['nc_from'], *c['nc_to'], 500), wait(1.5), ('dumpoff',), shot('settled')]),
    'calendar-dump': dict(start='home', steps=[
        ('dumpon',), tap(244, 120), wait(6), ('dumpoff',), shot('calendar'), key('home'), wait(2)]),
    'control-center': dict(start='home', steps=[
        shot('before'), swipe(*c['cc_from'], *c['cc_to'], 500), shot('pulled'), wait(1.5),
        shot('settled'), swipe(320, 300, 320, 900, 400), wait(1.5), shot('closed')]),
    'clock-icon': dict(start='home', steps=[
        shot('before'), wait(1), shot('before-1s'), wait(1), shot('before-2s'),
        tap(*c['clock_icon']), wait(3), shot('clock-app'), key('home'), wait(2), shot('back-home')]),
    'home-button-animation': dict(start='home', steps=[
        tap(*c['clock_icon']), wait(4), shot('app'),
        key('home'), shot('anim-00'), wait(0.1), shot('anim-01'), wait(0.1), shot('anim-02'),
        wait(0.1), shot('anim-03'), wait(0.1), shot('anim-04'), wait(0.1), shot('anim-05'),
        wait(0.2), shot('anim-06'), wait(0.4), shot('anim-07'), wait(1.5), shot('settled')]),
}
# Every home-based scenario begins from a settled home screen: a previous scenario may
# have left an app, a pulled sheet or an animation running, and taps would then land wrong.
for _sc in SCENARIOS.values():
    if _sc['start'] == 'home':
        _sc['steps'] = [key('home'), wait(2), key('home'), wait(1.5)] + list(_sc['steps'])
DEFAULT_ORDER = list(SCENARIOS)


# ---------------------------------------------------------------------------
# QMP
# ---------------------------------------------------------------------------
class QMP:
    def __init__(self, path, timeout=20):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(str(path))
        self.f = self.sock.makefile('rwb', buffering=0)
        if 'QMP' not in self._recv():
            raise ValueError('bad QMP greeting')
        self.execute('qmp_capabilities')

    def _recv(self):
        line = self.f.readline(1 << 20)
        if not line:
            raise ValueError('QMP connection closed')
        return json.loads(line)

    def execute(self, command, arguments=None):
        ident = uuid.uuid4().hex
        req = {'execute': command, 'id': ident}
        if arguments is not None:
            req['arguments'] = arguments
        self.f.write(json.dumps(req).encode() + b'\n')
        while True:
            r = self._recv()
            if r.get('id') == ident:
                if 'error' in r:
                    raise ValueError(f'{command}: {r["error"].get("desc")}')
                return r.get('return')

    def close(self):
        try:
            self.f.close()
            self.sock.close()
        except OSError:
            pass


def mtt_events(kind, points):
    """Same event list as QMPInput.events in sources/vphone-player/main.swift."""
    events = []
    for slot, (x, y) in enumerate(points):
        for axis, value, size in (('x', x, W), ('y', y, H)):
            v = int(max(0, min(size - 1, value))) * 32767 // size
            events.append({'type': 'mtt', 'data': {'type': 'data', 'slot': slot, 'tracking-id': slot,
                                                   'axis': axis, 'value': v}})
        events.append({'type': 'mtt', 'data': {'type': kind, 'slot': slot, 'tracking-id': slot,
                                               'axis': 'x', 'value': 0}})
    return events


def swipe_points(x1, y1, x2, y2, ms, hz=30):
    n = max(2, int(ms / 1000 * hz))
    return [(x1 + (x2 - x1) * i / n, y1 + (y2 - y1) * i / n) for i in range(n + 1)], ms / 1000 / n


class Driver:
    def __init__(self, qmp, out):
        self.q, self.out = qmp, Path(out)

    def touch(self, kind, pts):
        self.q.execute('input-send-event', {'device': DEVICE, 'events': mtt_events(kind, pts)})

    def key(self, qcode):
        for down in (True, False):
            self.q.execute('input-send-event', {'device': DEVICE, 'events': [
                {'type': 'key', 'data': {'down': down, 'key': {'type': 'qcode', 'data': qcode}}}]})
            time.sleep(0.05)

    def tap(self, x, y, hold):
        self.touch('begin', [(x, y)])
        time.sleep(hold)
        self.touch('end', [(x, y)])

    def swipe(self, x1, y1, x2, y2, ms):
        pts, dt = swipe_points(x1, y1, x2, y2, ms)
        self.touch('begin', [pts[0]])
        for p in pts[1:]:
            time.sleep(dt)
            self.touch('update', [p])
        self.touch('end', [pts[-1]])

    def screendump(self, dest_png):
        ppm = self.out / '_tmp.ppm'
        t0 = time.monotonic()
        self.q.execute('screendump', {'filename': str(ppm)})
        dt = time.monotonic() - t0
        ppm_to_png(ppm, dest_png)
        ppm.unlink(missing_ok=True)
        return dt


# ---------------------------------------------------------------------------
# Image IO: PPM -> PNG (Pillow if present, else stdlib zlib writer), PNG reader.
# ---------------------------------------------------------------------------
def read_ppm(path):
    data = Path(path).read_bytes()
    parts, i = [], 0
    while len(parts) < 4:
        while data[i:i + 1].isspace():
            i += 1
        if data[i:i + 1] == b'#':
            while data[i:i + 1] != b'\n':
                i += 1
            continue
        j = i
        while not data[j:j + 1].isspace():
            j += 1
        parts.append(data[i:j])
        i = j
    if parts[0] != b'P6' or parts[3] != b'255':
        raise ValueError('expected P6 255 PPM')
    w, h = int(parts[1]), int(parts[2])
    return w, h, data[i + 1:i + 1 + w * h * 3]


def write_png(path, w, h, rgb):
    def chunk(tag, body):
        return struct.pack('>I', len(body)) + tag + body + struct.pack('>I', zlib.crc32(tag + body))
    stride = w * 3
    raw = b''.join(b'\0' + rgb[y * stride:(y + 1) * stride] for y in range(h))
    Path(path).write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
                           + chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b''))


def ppm_to_png(ppm, png):
    Path(png).parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image
        Image.open(ppm).save(png)
    except ImportError:
        w, h, rgb = read_ppm(ppm)
        write_png(png, w, h, rgb)


def read_png(path):
    """Decode an 8-bit non-interlaced PNG (any colour type but palette)."""
    data = Path(path).read_bytes()
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('not a PNG')
    i, idat, w = 8, [], None
    while i < len(data):
        n, tag = struct.unpack('>I4s', data[i:i + 8])
        body = data[i + 8:i + 8 + n]
        if tag == b'IHDR':
            w, h, depth, ctype, _, _, inter = struct.unpack('>IIBBBBB', body)
            if depth != 8 or inter or ctype not in (0, 2, 4, 6):
                raise ValueError('unsupported PNG')
        elif tag == b'IDAT':
            idat.append(body)
        i += 12 + n
    ch = {0: 1, 2: 3, 4: 2, 6: 4}[ctype]
    raw = zlib.decompress(b''.join(idat))
    stride = w * ch
    rows, prev = [], bytearray(stride)
    for y in range(h):
        f = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for x in range(stride):
            a = line[x - ch] if x >= ch else 0
            b = prev[x]
            cc = prev[x - ch] if x >= ch else 0
            if f == 1: line[x] = (line[x] + a) & 255
            elif f == 2: line[x] = (line[x] + b) & 255
            elif f == 3: line[x] = (line[x] + ((a + b) >> 1)) & 255
            elif f == 4:
                p = a + b - cc
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - cc)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else cc)) & 255
        rows.append(line)
        prev = line
    rgb = bytearray()
    for line in rows:
        if ch == 3: rgb += line
        elif ch == 4:
            for k in range(0, stride, 4): rgb += line[k:k + 3]
        elif ch == 1:
            for v in line: rgb += bytes((v, v, v))
        else:
            for k in range(0, stride, 2): rgb += bytes((line[k],) * 3)
    return Img(w, h, bytes(rgb))


class Img:
    """Packed RGB image with cheap pixel access."""
    def __init__(self, w, h, rgb):
        self.w, self.h, self.rgb = w, h, rgb

    def px(self, x, y):
        x = min(max(x, 0), self.w - 1); y = min(max(y, 0), self.h - 1)
        i = (y * self.w + x) * 3
        return self.rgb[i], self.rgb[i + 1], self.rgb[i + 2]

    @classmethod
    def blank(cls, w, h, color=(0, 0, 0)):
        return cls(w, h, bytes(color) * (w * h))

    def fill(self, x0, y0, x1, y1, color):
        rgb = bytearray(self.rgb)
        row = bytes(color) * max(0, min(x1, self.w) - max(x0, 0))
        for y in range(max(y0, 0), min(y1, self.h)):
            i = (y * self.w + max(x0, 0)) * 3
            rgb[i:i + len(row)] = row
        self.rgb = bytes(rgb)
        return self


def load_image(path):
    path = Path(path)
    if path.suffix == '.ppm':
        return Img(*read_ppm(path))
    try:
        from PIL import Image
        im = Image.open(path).convert('RGB')
        return Img(im.width, im.height, im.tobytes())
    except ImportError:
        return read_png(path)


# ---------------------------------------------------------------------------
# Booting / attaching
# ---------------------------------------------------------------------------
def vms_dir():
    return Path(os.environ.get('VPHONE_LEGACY_VMS_DIR', str(Path.home() / '.vphone-legacy/VMs'))).expanduser()


def resolve_bundle(spec):
    p = Path(spec).expanduser()
    if p.is_dir() and (p / 'vm_manifest.json').exists():
        return p.resolve()
    q = vms_dir() / spec
    if q.is_dir():
        return q.resolve()
    raise SystemExit(f'bundle not found: {spec}')


def find_root(explicit=None):
    """Repo root that has the built backend; a fresh worktree may lack it."""
    cands = [Path(explicit)] if explicit else [Path(os.environ['VPHONE_LEGACY_ROOT'])] \
        if os.environ.get('VPHONE_LEGACY_ROOT') else [REPO, REPO.parent / 'vphone-legacy']
    for r in cands:
        if (r / 'backend/backend.json').exists() and (r / 'scripts/qemu/runtime.py').exists():
            return r.resolve()
    raise SystemExit('no repo root with a built backend (backend/backend.json); pass --root')


STUB = '''import sys, time
from pathlib import Path
root = Path(sys.argv[1]); bundle = sys.argv[2]
sys.path[:0] = [str(root / 'scripts'), str(root / 'backend')]
import qemu.runtime as rt
import live_run
# A stub "player" keeps the input QMP socket free for this harness.
live_run.build_player = lambda root=None: Path(sys.executable)
rt.build_player_command = lambda *a, **k: [sys.executable, '-c', 'import time; time.sleep(1e9)']
sys.exit(rt.launch(bundle, display='player'))
'''


def clone_bundle(src, dst):
    dst.mkdir(parents=True)
    for item in src.iterdir():
        if item.name in ('logs', 'runtime.json', 'runtime.lock', 'original'):
            continue
        subprocess.run(['/bin/cp', '-c', '-R', str(item), str(dst / item.name)], check=True)
    # `original` is only firmware provenance; link it instead of copying GBs.
    if (src / 'original').exists():
        (dst / 'original').symlink_to(src / 'original')
    (dst / 'runtime.lock').touch()


class Ephemeral:
    def __init__(self, bundle, out, root, boot_timeout):
        self.bundle, self.out, self.root, self.timeout = bundle, out, root, boot_timeout
        self.work = Path(tempfile.mkdtemp(prefix='glitch-', dir=out))
        self.proc = None

    def start(self):
        clone = self.work / bundle_name(self.bundle)
        clone_bundle(self.bundle, clone)
        self.clone = clone
        (self.work / 'stub.py').write_text(STUB)
        env = dict(os.environ, VPHONE_LEGACY_ROOT=str(self.root))
        log = (self.work / 'launcher.log').open('wb')
        self.proc = subprocess.Popen([sys.executable, str(self.work / 'stub.py'), str(self.root), str(clone)],
                                     env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise SystemExit('launcher exited:\n' + (self.work / 'launcher.log').read_text()[-1500:])
            rt = clone / 'runtime.json'
            if rt.exists():
                qmp = (json.loads(rt.read_text()).get('control') or {}).get('qmp')
                if qmp and Path(qmp).exists():
                    return qmp
            time.sleep(0.3)
        raise SystemExit('QMP socket never appeared')

    def stage(self):
        try:
            return json.loads((self.clone / 'runtime.json').read_text()).get('boot_stage')
        except (OSError, ValueError):
            return None

    def stop(self):
        """Terminate only the launcher process group this script started."""
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, 15)
                self.proc.wait(timeout=15)
            except Exception:
                try:
                    os.killpg(self.proc.pid, 9)
                except OSError:
                    pass


def bundle_name(b):
    return Path(b).name


def wait_ready(q, eph, timeout):
    """Wait until userspace is up and the digitizer reports touches."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            if q.execute('qom-get', {'path': '/machine', 'property': 'touch-reporting-enabled'}) is True:
                return True
        except ValueError:
            pass
        st = eph.stage() if eph else None
        if st == 'panic':
            raise SystemExit('guest panicked during boot')
        time.sleep(2)
    return False


# ---------------------------------------------------------------------------
# Scenario runner
# ---------------------------------------------------------------------------
def run_steps(drv, name, steps, outdir, log):
    t0 = time.monotonic()
    n = 0
    for st in steps:
        t = round(time.monotonic() - t0, 3)
        op = st[0]
        if op == 'tap': drv.tap(st[1], st[2], st[3])
        elif op == 'swipe': drv.swipe(*st[1:6])
        elif op == 'key': drv.key(st[1])
        elif op == 'wait': time.sleep(st[1])
        elif op in ('dumpon', 'dumpoff'):
            trig = os.environ.get('VPHONE_AGX_FRAME_DUMP_TRIGGER')
            if trig:
                if op == 'dumpon': Path(trig).touch()
                else: Path(trig).unlink(missing_ok=True)
        elif op == 'dump':  # open the AGX frame-dump window (VPHONE_AGX_FRAME_DUMP_TRIGGER) for st[1] s
            trig = os.environ.get('VPHONE_AGX_FRAME_DUMP_TRIGGER')
            if trig:
                Path(trig).touch(); time.sleep(st[1]); Path(trig).unlink(missing_ok=True)
        elif op == 'shot':
            n += 1
            rel = f'{name}/{n:02d}-{st[1]}.png'
            dt = drv.screendump(outdir / rel)
            log.append(dict(op='shot', label=st[1], file=rel, t=t, screendump_s=round(dt, 3)))
            continue
        log.append(dict(op=op, args=list(st[1:]), t=t, took_s=round(time.monotonic() - t0 - t, 3)))


UNLOCK_SWIPES = ((30, 1000, 630, 1000, 1200), (30, 1015, 630, 1015, 1500), (20, 1005, 630, 1005, 1000),
                 (30, 995, 630, 995, 1800))
LOCK_REGION = (0, 260, 640, 900)   # wallpaper only on the lock screen; icons on home; clock/status excluded


def region_diff(a, b, box=LOCK_REGION, step=8):
    tot = n = 0
    for y in range(box[1], box[3], step):
        for x in range(box[0], box[2], step):
            p, q = a.px(x, y), b.px(x, y)
            tot += abs(p[0] - q[0]) + abs(p[1] - q[1]) + abs(p[2] - q[2])
            n += 3
    return tot / n


def probe_state(drv, outdir, lock_ref):
    """'dark' (panel off), 'lock' (wallpaper matches the boot lock screen) or 'other' (home/app)."""
    tmp = Path(outdir) / '_probe.png'
    drv.screendump(tmp)
    img = load_image(tmp)
    tmp.unlink(missing_ok=True)
    d = region_diff(img, lock_ref)
    luma = region_diff(img, Img.blank(img.w, img.h))   # mean brightness: 0 while the panel is dark
    state = 'dark' if luma < 60 else 'lock' if d < 6 else 'other'   # a fading panel counts as dark
    print(f'  unlock check: {state} (lock-diff {d:.1f} luma {luma:.1f})', flush=True)
    return state


def ensure_home(drv, outdir, lock_ref=None):
    """Wake and unlock, verified against the lock wallpaper; wakes a dark panel and retries
    other slides. Harmless when already on the home screen (the slide lands in the dock)."""
    drv.key('home'); time.sleep(1.5)
    if lock_ref is None:
        drv.swipe(*UNLOCK_SWIPES[0]); time.sleep(2.5)
        drv.key('home'); time.sleep(2)
        return True
    swipes = 0
    for _ in range(12):
        st = probe_state(drv, outdir, lock_ref)
        if st == 'other':
            drv.key('home'); time.sleep(2)
            if probe_state(drv, outdir, lock_ref) == 'other':   # confirm it did not re-lock
                return True
            continue
        if st == 'dark':
            drv.key('home'); time.sleep(2.5)
            continue
        time.sleep(1.0 if swipes else 0)
        drv.swipe(*UNLOCK_SWIPES[swipes % len(UNLOCK_SWIPES)]); swipes += 1
        time.sleep(2.5)
    print('WARNING: could not unlock the guest', flush=True)
    return False


def cmd_run(a):
    out = Path(a.out).resolve(); out.mkdir(parents=True, exist_ok=True)
    names = a.scenarios.split(',') if a.scenarios else DEFAULT_ORDER
    bad = [n for n in names if n not in SCENARIOS]
    if bad:
        raise SystemExit(f'unknown scenarios {bad}; have {DEFAULT_ORDER}')
    eph = None
    if a.use_running:
        p = subprocess.run([str(REPO / 'vphone-legacy'), 'vm', 'status', a.bundle, '--json'],
                           capture_output=True, text=True, check=True)
        qpath = (json.loads(p.stdout).get('control') or {}).get('launcher_qmp')
        if not qpath:
            raise SystemExit('vm status has no control.launcher_qmp (VM not running in player mode?)')
    else:
        eph = Ephemeral(resolve_bundle(a.bundle), out, find_root(a.root), a.boot_timeout)
        qpath = eph.start()
    manifest = dict(bundle=a.bundle, started=time.strftime('%Y-%m-%dT%H:%M:%S'), qmp=qpath,
                    use_running=a.use_running, scenarios={})
    try:
        q = QMP(qpath)
        drv = Driver(q, out)
        print('waiting for touch-ready guest...', flush=True)
        if not wait_ready(q, eph, a.boot_timeout):
            print('WARNING: digitizer never reported ready; continuing anyway', flush=True)
        time.sleep(a.settle)
        state = 'lock'
        lock_ref = None
        try:   # the guest boots to the lock screen: keep its wallpaper to verify unlocks against
            drv.screendump(out / '_lock_ref.png'); lock_ref = load_image(out / '_lock_ref.png')
            (out / '_lock_ref.png').unlink()
        except Exception:
            pass
        manifest['boot_stage'] = eph.stage() if eph else None
        for name in names:
            sc = SCENARIOS[name]
            if sc['start'] == 'home' and state != 'home':
                ensure_home(drv, out, lock_ref); state = 'home'
            log = []
            print(f'scenario {name}', flush=True)
            run_steps(drv, name, sc['steps'], out, log)
            manifest['scenarios'][name] = dict(start=sc['start'], steps=log)
            if sc['start'] == 'lock':
                state = 'home' if name == 'unlock-slide' else 'lock'
            elif any(s[0] == 'key' and s[1] == 'power' for s in sc['steps']):
                state = 'lock'
            (out / 'manifest.json').write_text(json.dumps(manifest, indent=1))
    finally:
        (out / 'manifest.json').write_text(json.dumps(manifest, indent=1))
        if eph:
            eph.stop()
            for f in ('launcher.log',):
                if (eph.work / f).exists():
                    shutil.copy(eph.work / f, out / f)
            # keep logs of the clone for debugging, drop the disk clones
            for p in eph.work.glob('*/*.raw'):
                p.unlink(missing_ok=True)
            shutil.rmtree(eph.work, ignore_errors=True)
    print(f'captured into {out}')
    return 0


# ---------------------------------------------------------------------------
# Analysis heuristics (pure functions on Img)
#
# All work on a coarse BLOCK x BLOCK grid so a 640x1136 frame costs ~10^4
# operations in pure Python. Thresholds are module constants.
# ---------------------------------------------------------------------------
BLOCK = 16
FLAT_RANGE = 6           # max per-channel spread inside a block to call it "near-uniform"
DARK_LUMA = 70           # mean luma below this counts as "dark"
GREY_SAT = 14            # max-min of the mean channels below this counts as "grey"
MIN_RECT_BLOCKS = (5, 5)  # smallest reported rectangle (80x80 px)
RECT_FILL = 0.9          # component must fill >= this much of its bounding box
RECT_NEW = 0.5           # >= this share of its blocks must be new vs the previous frame
BLACK_LUMA = 6
BLACK_SHARE = 0.995
SAME_DIFF = 0.5          # mean abs diff (0..255) below which two frames are "unchanged"


def block_grid(img, block=BLOCK):
    """Return (cols, rows, cells) where cells[r][c] = (mean_rgb, spread) from 4x4 samples."""
    cols, rows = img.w // block, img.h // block
    step = max(1, block // 4)
    cells = []
    for r in range(rows):
        line = []
        for c in range(cols):
            vals = [img.px(c * block + i * step, r * block + j * step) for j in range(4) for i in range(4)]
            mean = tuple(sum(v[k] for v in vals) / len(vals) for k in range(3))
            spread = max(max(v[k] for v in vals) - min(v[k] for v in vals) for k in range(3))
            line.append((mean, spread))
        cells.append(line)
    return cols, rows, cells


def flat_dark_mask(img, block=BLOCK):
    """Set of (col,row) blocks that are near-uniform AND dark or grey."""
    cols, rows, cells = block_grid(img, block)
    mask = set()
    for r in range(rows):
        for c in range(cols):
            (R, G, B), spread = cells[r][c]
            if spread > FLAT_RANGE:
                continue
            luma = 0.299 * R + 0.587 * G + 0.114 * B
            if luma < DARK_LUMA or max(R, G, B) - min(R, G, B) < GREY_SAT:
                mask.add((c, r))
    return mask


def components(mask):
    seen, out = set(), []
    for start in mask:
        if start in seen:
            continue
        stack, comp = [start], []
        seen.add(start)
        while stack:
            c, r = stack.pop()
            comp.append((c, r))
            for n in ((c + 1, r), (c - 1, r), (c, r + 1), (c, r - 1)):
                if n in mask and n not in seen:
                    seen.add(n); stack.append(n)
        out.append(comp)
    return out


def find_new_flat_rects(img, prev, block=BLOCK):
    """(a) Axis-aligned near-uniform dark/grey rectangles absent from the previous frame.

    Returns [{'bbox': [x0,y0,x1,y1], 'new_share': f, 'color': [r,g,b]}]. With no
    previous frame nothing is reported (every flat area would be "new").
    """
    if prev is None:
        return []
    cur, old = flat_dark_mask(img, block), flat_dark_mask(prev, block)
    found = []
    for comp in components(cur):
        cs, rs = [c for c, _ in comp], [r for _, r in comp]
        bw, bh = max(cs) - min(cs) + 1, max(rs) - min(rs) + 1
        if bw < MIN_RECT_BLOCKS[0] or bh < MIN_RECT_BLOCKS[1] or len(comp) < RECT_FILL * bw * bh:
            continue
        new = sum(1 for b in comp if b not in old) / len(comp)
        if new < RECT_NEW:
            continue
        x0, y0 = min(cs) * block, min(rs) * block
        found.append(dict(bbox=[x0, y0, (max(cs) + 1) * block, (max(rs) + 1) * block],
                          new_share=round(new, 2), color=list(img.px(x0 + bw * block // 2, y0 + bh * block // 2))))
    return found


def _dist(a, b):
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2]))


TILE_MIN, TILE_MAX = 96, 144
EDGE_MIN_GRAD = 12       # min gradient across a tile edge to accept it
CORNER_DIFF = 24         # colour distance treated as "different"


def find_tile_bbox(img, cx, cy, half=ICON // 2, slack=22):
    """Locate an icon-sized tile around an expected centre by strongest edge per side.

    Looks for the largest colour step along the centre row/column within +-slack
    of the expected edge. Returns (x0,y0,x1,y1) or None if there is no clear edge.
    """
    def best(points):
        top, pos = 0, None
        for p, a, b in points:
            g = _dist(img.px(*a), img.px(*b))
            if g > top:
                top, pos = g, p
        return pos if top >= EDGE_MIN_GRAD else None

    def side(sign, horizontal):
        pts = []
        for d in range(half - slack, half + slack):
            if horizontal:
                x = cx + sign * d
                pts.append((x, (x - 1, cy), (x, cy)))
            else:
                y = cy + sign * d
                pts.append((y, (cx, y - 1), (cx, y)))
        return best(pts)
    x0, x1, y0, y1 = side(-1, True), side(1, True), side(-1, False), side(1, False)
    if None in (x0, x1, y0, y1):
        return None
    if not (TILE_MIN <= x1 - x0 <= TILE_MAX and TILE_MIN <= y1 - y0 <= TILE_MAX):
        return None
    return x0, y0, x1, y1


def hard_corners(img, bbox):
    """(b) Count tile corners that look square instead of rounded.

    For every corner take the pixel just inside the bbox corner, the wallpaper 4px
    diagonally outside it, and the tile body 32px diagonally inside. A rounded corner
    shows the wallpaper (close to `outside`, far from `body`). A square corner shows
    tile body (close to `body`, far from `outside`). Returns the hard corner names.
    """
    x0, y0, x1, y1 = bbox
    x1, y1 = x1 - 1, y1 - 1
    hard = []
    for name, (cx_, cy_, sx, sy) in dict(tl=(x0, y0, -1, -1), tr=(x1, y0, 1, -1),
                                         bl=(x0, y1, -1, 1), br=(x1, y1, 1, 1)).items():
        corner = img.px(cx_ - sx, cy_ - sy)
        outside = img.px(cx_ + sx * 4, cy_ + sy * 4)
        body = img.px(cx_ - sx * 32, cy_ - sy * 32)
        if _dist(body, outside) < CORNER_DIFF:
            continue          # no contrast between tile and wallpaper: cannot tell
        if _dist(corner, outside) >= CORNER_DIFF and _dist(corner, body) < CORNER_DIFF:
            hard.append(name)
    return hard


def find_hard_cornered_tiles(img, cells=None, min_hard=3):
    """Run find_tile_bbox + hard_corners over expected icon cells."""
    res = []
    for cx, cy in (cells if cells is not None else ICON_CELLS):
        box = find_tile_bbox(img, cx, cy)
        if not box:
            continue
        hc = hard_corners(img, box)
        if len(hc) >= min_hard:
            res.append(dict(bbox=list(box), corners=hc))
    return res


def is_black(img, step=8):
    n = dark = 0
    for y in range(0, img.h, step):
        for x in range(0, img.w, step):
            r, g, b = img.px(x, y)
            n += 1
            dark += (0.299 * r + 0.587 * g + 0.114 * b) < BLACK_LUMA
    return dark / n >= BLACK_SHARE


def frame_diff(a, b, step=8):
    if a is None or b is None or (a.w, a.h) != (b.w, b.h):
        return 255.0
    tot = n = 0
    for y in range(0, a.h, step):
        for x in range(0, a.w, step):
            p, q = a.px(x, y), b.px(x, y)
            tot += abs(p[0] - q[0]) + abs(p[1] - q[1]) + abs(p[2] - q[2])
            n += 3
    return tot / n


def analyze_sequence(images, stuck=3):
    """Flag a scenario's ordered frames. `images` is a list of Img. Returns list of flag-lists."""
    flags, run, prev = [], 0, None
    for img in images:
        f = []
        if is_black(img):
            f.append(dict(kind='black'))
        if frame_diff(prev, img) < SAME_DIFF:
            run += 1
            if run > stuck:
                f.append(dict(kind='unchanged', run=run))
        else:
            run = 0
        for r in find_new_flat_rects(img, prev):
            f.append(dict(kind='flat-rect', **r))
        for t in find_hard_cornered_tiles(img):
            f.append(dict(kind='hard-corners', **t))
        flags.append(f)
        prev = img
    return flags


def cmd_analyze(a):
    d = Path(a.dir)
    scenarios = {}
    for sd in sorted(p for p in d.iterdir() if p.is_dir() and not p.name.startswith('_')):
        files = sorted(sd.glob('*.png')) or sorted(sd.glob('*.ppm'))
        if not files:
            continue
        flags = analyze_sequence([load_image(f) for f in files], a.stuck)
        scenarios[sd.name] = [dict(file=str(f.relative_to(d)), flags=fl) for f, fl in zip(files, flags)]
    total = sum(1 for fr in scenarios.values() for x in fr if x['flags'])
    (d / 'report.json').write_text(json.dumps(dict(flagged_frames=total, scenarios=scenarios), indent=1))
    (d / 'report.html').write_text(render_html(scenarios, total))
    print(f'{total} flagged frames; wrote {d / "report.json"} and {d / "report.html"}')
    return 0


def render_html(scenarios, total):
    import html
    cards = []
    for sc, frames in scenarios.items():
        for fr in frames:
            if not fr['flags']:
                continue
            boxes = ''.join(
                f'<div class="box" style="left:{b[0] / W * 100:.2f}%;top:{b[1] / H * 100:.2f}%;'
                f'width:{(b[2] - b[0]) / W * 100:.2f}%;height:{(b[3] - b[1]) / H * 100:.2f}%"></div>'
                for fl in fr['flags'] for b in [fl.get('bbox')] if b)
            why = '<br>'.join(html.escape(fl['kind'] + (' ' + str(fl['bbox']) if 'bbox' in fl else ''))
                              for fl in fr['flags'])
            cards.append(f'<figure><div class="img"><img src="{html.escape(fr["file"])}" loading="lazy">{boxes}</div>'
                         f'<figcaption><b>{html.escape(fr["file"])}</b><br>{why}</figcaption></figure>')
    body = ''.join(cards) or '<p>No frames flagged.</p>'
    return ('<!doctype html><meta charset=utf-8><title>Glitch audit</title><style>'
            'body{font:14px system-ui;margin:16px;background:#111;color:#ddd}'
            '.grid{display:flex;flex-wrap:wrap;gap:12px}figure{margin:0;width:200px}'
            '.img{position:relative}.img img{width:100%;display:block}'
            '.box{position:absolute;border:2px solid #f0f;box-sizing:border-box}'
            'figcaption{font-size:12px;word-break:break-all}</style>'
            f'<h1>Glitch audit: {total} flagged frame(s)</h1><div class=grid>{body}</div>')


def cmd_list(a):
    for n in DEFAULT_ORDER:
        print(f'{n:24s} start={SCENARIOS[n]["start"]:5s} steps={len(SCENARIOS[n]["steps"])}')
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run')
    r.add_argument('--bundle', required=True, help='bundle name under VPHONE_LEGACY_VMS_DIR or a path')
    r.add_argument('--out', required=True)
    r.add_argument('--scenarios', help='comma list; default all (see `list`)')
    r.add_argument('--use-running', action='store_true', help='attach to control.launcher_qmp of a running VM')
    r.add_argument('--root', help='repo root with the built backend (default: this repo, else sibling vphone-legacy)')
    r.add_argument('--boot-timeout', type=float, default=420)
    r.add_argument('--settle', type=float, default=5, help='seconds to wait after the digitizer is ready')
    r.set_defaults(fn=cmd_run)
    an = sub.add_parser('analyze')
    an.add_argument('dir')
    an.add_argument('--stuck', type=int, default=3, help='flag frames identical for more than N steps')
    an.set_defaults(fn=cmd_analyze)
    sub.add_parser('list').set_defaults(fn=cmd_list)
    a = p.parse_args()
    return a.fn(a)


if __name__ == '__main__':
    sys.exit(main())
