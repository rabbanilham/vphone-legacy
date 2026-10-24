#!/usr/bin/env python3
"""AGX frame-dump corpus: record it once, then replay it offline in seconds.

  agx_corpus.py capture [--scene NAME ...]   live boots that record the dumps
  agx_corpus.py [replay] [--corpus DIR]      C and Python replay of every dump

capture boots the default virtual-activated profile once per scene, with a
touch script that walks to the screen and VPHONE_AGX_FRAME_DUMP_AT windows
around each step. Only display passes are dumped: they load the previous
frame, so each one replays to the whole screen and compares directly with
the live framebuffer captured at the window's start. Without a Home button a
boot reaches one app, hence one scene per app. Disk clones are deleted after
each boot; the dumps (about 21 MiB each) stay in build/agx-corpus/<scene>/dumps.

replay compiles the renderer core of backend/s5l8960x.c into a host harness
(backend/agx_replay_host.c) and replays every dump with it, chained per scene
(a dump records memory at submission, before the live render thread drew the
passes ahead of it; --single replays each dump on its own), audits every dump
with backend/agx_replay_offline.py, then writes report.html (contact sheet:
C replay next to the live framebuffer) and summary.json (skipped draws by
reason, per frame).
"""

import argparse
import concurrent.futures as futures
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import time
import zlib

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'backend'
PROFILE = ROOT / 'build/virtual-activated-profile-v5'
CORPUS = ROOT / 'build/agx-corpus'
HARNESS_SRC = BACKEND / 'agx_replay_host.c'
CORE_SRC = BACKEND / 's5l8960x.c'

# Script coordinates are about 10 px above the framebuffer position.
UNLOCK = ('at=78;tap 600,900|at=86;tap 600,900|at=94;tap 600,900|'
          'at=95;down 40,1036;move 600,1036 dur=0.7 steps=42;up|at=103;tap 600,900')
SCENES = {
    # Lock, Home, Settings opening, Settings, General opening, General.
    'settings': {
        'touch': UNLOCK + '|at=111;tap 600,900|at=119;tap 600,900|at=127;tap 600,900' +
                 '|at=130;tap 245,815|at=150;tap 200,900',
        'at': [90, 105, 125, 131, 134, 138, 145, 151, 154, 160],
        'seconds': 170,     # display_probe allows at most 180
    },
    # App launch transition and the Clock app (World Clock).
    'clock': {
        'touch': UNLOCK + '|at=110;tap 245,295',
        'at': [111, 113, 116, 121, 130],
        'seconds': 138,
    },
    # Every pass, display and offscreen, through the unlock animation, then
    # the settled Home (icon redraws such as the Clock hands).
    'unlock': {
        'touch': UNLOCK + '|at=106;tap 600,900',
        'at': [95.3, 101],
        'per_at': 90,
        'display_only': False,
        'seconds': 108,
    },
    # The second half of the unlock: Home icons and labels flying in.
    'unlock-home': {
        'touch': UNLOCK + '|at=106;tap 600,900',
        'at': [97.5],
        'per_at': 150,
        'display_only': False,
        'seconds': 106,
    },
    # The blank 98..101 s gap of a normal-speed unlock (icons should fly in).
    'unlock-flyin': {
        'touch': UNLOCK,
        'at': [98.3, 99.3, 100.3],
        'per_at': 15,
        'display_only': False,
        'seconds': 106,
    },
    # Every pass of the settled Settings screen (offscreen switch passes too).
    'settings-all': {
        'touch': UNLOCK + '|at=111;tap 600,900|at=119;tap 600,900|at=127;tap 600,900|at=130;tap 245,815',
        'at': [145],
        'per_at': 40,
        'display_only': False,
        'seconds': 160,
    },
    'calendar': {
        'touch': UNLOCK + '|at=110;tap 245,100',
        'at': [111, 113, 116, 121, 130],
        'seconds': 138,
    },
}


def load_avg():
    return os.getloadavg()[0]


def capture(args):
    raw_names = parse_scenes_arg(args.scene)
    names = list(raw_names) if raw_names else list(SCENES)
    for name in names:
        if name not in SCENES:
            raise ValueError(f"Unknown capture scene: '{name}'. Available: {list(SCENES)}")
        scene = SCENES[name]
        out = args.corpus / name
        if out.exists():
            shutil.rmtree(out)
        dumps = out / 'dumps'
        dumps.mkdir(parents=True)
        while load_avg() >= 5:
            print(f'{name}: waiting for load < 5 ({load_avg():.1f})', flush=True)
            time.sleep(15)
        env = {**os.environ,
               'VPHONE_DISPLAY': 'cocoa', 'VPHONE_TCG_THREAD': 'multi',
               'VPHONE_TOUCH_SCRIPT': scene['touch'],
               'VPHONE_AGX_FRAME_DUMP': str(dumps),
               'VPHONE_AGX_FRAME_DUMP_COUNT': '1000',
               'VPHONE_AGX_FRAME_DUMP_AT': ','.join(map(str, scene['at'])),
               'VPHONE_AGX_FRAME_DUMP_PER_AT': str(scene.get('per_at', args.per_at)),
               'VPHONE_AGX_FRAME_DUMP_MIN_DRAWS': str(args.min_draws)}
        if scene.get('display_only', True):
            env['VPHONE_AGX_FRAME_DUMP_DISPLAY'] = '1'
        cmd = [sys.executable, str(BACKEND / 'display_probe.py'),
               '--plan', str(PROFILE / 'boot/boot_plan.json'),
               '--diagnostic-system', str(PROFILE / 'system.raw'),
               '--diagnostic-data', str(PROFILE / 'data.raw'),
               '--seconds', str(scene['seconds']), '--stop-condition', 'duration',
               '--output', str(out / 'probe')]
        for t in scene['at']:
            cmd += ['--framebuffer-at', str(round(t))]
        print(f'{name}: boot {scene["seconds"]} s, dump windows {scene["at"]}', flush=True)
        result = subprocess.run(cmd, cwd=ROOT, env=env)
        shutil.rmtree(out / 'probe/images', ignore_errors=True)
        (out / 'recipe.json').write_text(json.dumps(
            {'scene': name, **scene, 'per_at': args.per_at, 'min_draws': args.min_draws,
             'probe_exit': result.returncode}, indent=2) + '\n')
        count = len(list(dumps.glob('frame-*.bin')))
        size = sum(p.stat().st_size for p in dumps.glob('frame-*.bin')) >> 20
        print(f'{name}: probe exit {result.returncode}, {count} dumps, {size} MiB', flush=True)


def png_bytes(w, h, rgba):
    raw = b''.join(b'\x00' + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))

    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress(raw, 1)) + chunk(b'IEND', b''))


def ppm_to_png(src, dst):
    data = src.read_bytes()
    fields, pos = [], 0
    while len(fields) < 4:      # P6, width, height, maxval; no comments in probe output
        while data[pos:pos + 1].isspace():
            pos += 1
        end = pos
        while not data[end:end + 1].isspace():
            end += 1
        fields.append(data[pos:end])
        pos = end
    w, h = int(fields[1]), int(fields[2])
    rgb = data[pos + 1:pos + 1 + w * h * 3]
    rgba = bytearray(w * h * 4)
    rgba[0::4], rgba[1::4], rgba[2::4] = rgb[0::3], rgb[1::3], rgb[2::3]
    rgba[3::4] = b'\xff' * (w * h)
    dst.write_bytes(png_bytes(w, h, bytes(rgba)))


def extract_core():
    """Renderer core of s5l8960x.c, minus the QEMU-state-only snapshot lookup."""
    src = CORE_SRC.read_text()
    begin = src.index('/* AGX_REPLAY_CORE_BEGIN')
    end = src.index('/* AGX_REPLAY_CORE_END */')
    core = src[begin:end]

    def function(text, signature):
        start = text.index(signature)
        stop = text.index('\n}\n', start) + 3
        return start, stop
    a, b = function(core, 'static const uint8_t *agx_ta_snapshot_page(')
    core = core[:a] + core[b:]
    a, b = function(src, 'static bool agx_ctx_write(')
    return ('static bool agx_ctx_read(A7State *s, uint64_t cat, uint64_t gva, void *buf, size_t n);\n' +
            src[a:b] + '\n#line 1 "s5l8960x.c (AGX replay core)"\n' + core)


def build_harness(out_dir):
    core = extract_core()
    digest = hashlib.sha256((core + HARNESS_SRC.read_text()).encode()).hexdigest()[:12]
    binary = out_dir / f'agx_replay_host-{digest}'
    if binary.exists():
        return binary
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'agx_replay_core.inc').write_text(core)
    # Same pkg-config and Homebrew glib as scripts/qemu/build_backend.py.
    brew = shutil.which('brew')
    prefix = Path(subprocess.check_output([brew, '--prefix'], text=True).strip()) if brew else None
    pcs = [prefix / 'opt' / lib / 'lib/pkgconfig' for lib in ('glib', 'pcre2')] if prefix else []
    env = {**os.environ,
           'PATH': str(BACKEND / '.venv/bin') + os.pathsep + os.environ.get('PATH', ''),
           'PKG_CONFIG_PATH': os.pathsep.join([*(str(p) for p in pcs if p.exists()), os.environ.get('PKG_CONFIG_PATH', '')])}
    flags = subprocess.run(['pkg-config', '--cflags', '--libs', 'glib-2.0'], env=env,
                           capture_output=True, text=True, check=True).stdout.split()
    subprocess.run(['cc', '-O2', '-std=gnu11', '-w', '-I', str(out_dir), str(HARNESS_SRC),
                    '-o', str(binary), *flags, '-lm'], check=True)
    return binary


def replay_c(binary, dump, out_png):
    rgba = out_png.with_suffix('.rgba')
    result = subprocess.run([str(binary), str(dump), str(rgba)], capture_output=True, text=True)
    if result.returncode:
        return {'error': (result.stderr or f'exit {result.returncode}').strip()[-400:]}
    info = json.loads(result.stdout)
    data = rgba.read_bytes()
    w, h = struct.unpack_from('<II', data, 4)
    out_png.write_bytes(png_bytes(w, h, data[12:]))
    rgba.unlink()
    info['stderr'] = result.stderr.strip().splitlines()[-5:]
    return info


def parse_scenes_arg(scenes_arg):
    if not scenes_arg:
        return None
    res = set()
    for item in scenes_arg:
        for part in str(item).split(','):
            part = part.strip()
            if part:
                res.add(part)
    return res if res else None


def parse_frame_spec(spec: str):
    """Parse a frame filter string into a matcher function: (int, str) -> bool.

    Supports:
      - Single frame: '5' or 'frame-5'
      - Ranges: '5..10', '5-10', '5:10'
      - Open-start: '..10', ':10', '-10'
      - Open-end: '5..', '5:'
      - Comma-separated combinations: '1,3,5..7'
    """
    if not spec:
        return lambda idx, stem: True

    matchers = []
    for raw_part in spec.split(','):
        part = raw_part.strip()
        if not part:
            continue
        if part.startswith('frame-'):
            sub = part[len('frame-'):]
            if sub.isdigit():
                val = int(sub)
                matchers.append(lambda idx, stem, v=val, s=part: idx == v or stem == s)
            else:
                matchers.append(lambda idx, stem, s=part: stem == s)
            continue

        if '..' in part:
            a, b = part.split('..', 1)
            min_v = int(a.strip()) if a.strip() else None
            max_v = int(b.strip()) if b.strip() else None
            matchers.append(lambda idx, stem, lo=min_v, hi=max_v:
                            (lo is None or idx >= lo) and (hi is None or idx <= hi))
        elif ':' in part:
            a, b = part.split(':', 1)
            min_v = int(a.strip()) if a.strip() else None
            max_v = int(b.strip()) if b.strip() else None
            matchers.append(lambda idx, stem, lo=min_v, hi=max_v:
                            (lo is None or idx >= lo) and (hi is None or idx <= hi))
        elif '-' in part:
            if part.startswith('-'):
                hi = int(part[1:].strip())
                matchers.append(lambda idx, stem, hi=hi: idx <= hi)
            elif part.endswith('-'):
                lo = int(part[:-1].strip())
                matchers.append(lambda idx, stem, lo=lo: idx >= lo)
            else:
                a, b = part.split('-', 1)
                min_v = int(a.strip())
                max_v = int(b.strip())
                matchers.append(lambda idx, stem, lo=min_v, hi=max_v: lo <= idx <= hi)
        else:
            if part.isdigit():
                val = int(part)
                matchers.append(lambda idx, stem, v=val: idx == v)
            else:
                matchers.append(lambda idx, stem, s=part: stem == s)

    if not matchers:
        return lambda idx, stem: True

    return lambda idx, stem: any(m(idx, stem) for m in matchers)


def resolve_report_dir(args) -> Path:
    if getattr(args, 'output', None):
        report = Path(args.output)
        report.mkdir(parents=True, exist_ok=True)
        return report

    default_report = args.corpus / 'report'
    if getattr(args, 'unique_output', False) or default_report.exists():
        timestamp = time.strftime('%Y%m%d-%H%M%S')
        unique_name = f'report-{timestamp}'
        report = args.corpus / unique_name
        if report.exists():
            report = args.corpus / f'report-{timestamp}-{os.getpid()}'
        report.mkdir(parents=True, exist_ok=True)
        if default_report.exists() and not getattr(args, 'unique_output', False):
            print(f'Preserving existing {default_report}; writing to new report directory {report}', flush=True)
        return report

    default_report.mkdir(parents=True, exist_ok=True)
    return default_report


def replay_c_chain(binary, frames, report):
    """One scene in dump order: each pass sees what the earlier ones drew."""
    prefix = report / f'chain-{frames[0]["scene"]}'
    result = subprocess.run([str(binary), '--chain', str(prefix), *[str(f['dump']) for f in frames]],
                            capture_output=True, text=True)
    lines = result.stdout.splitlines()
    infos = []
    for i, f in enumerate(frames):
        if i >= len(lines):
            infos.append({'error': (result.stderr or f'exit {result.returncode}').strip()[-400:]})
            continue
        info = json.loads(lines[i])
        rgba = Path(f'{prefix}-{i}.rgba')
        if f.get('selected', True) and rgba.exists():
            data = rgba.read_bytes()
            w, h = struct.unpack_from('<II', data, 4)
            (report / f'{f["scene"]}-{f["frame"]}.png').write_bytes(png_bytes(w, h, data[12:]))
        if rgba.exists():
            rgba.unlink()
        infos.append(info)
    return infos


def audit_python(dump):
    sys.path.insert(0, str(BACKEND))
    from agx_frame import Frame
    from agx_replay_offline import audit_frame
    try:
        frame = Frame(dump)
        frame.require_matching_ta()
        outcomes = audit_frame(frame)
    except Exception as e:      # noqa: BLE001 - one bad dump must not stop the corpus
        return {'error': f'{type(e).__name__}: {e}'}
    return {'draws': len(outcomes),
            'outcomes': [[did, res] for did, res, _ in outcomes]}


def replay(args):
    start_total = time.monotonic()
    report = resolve_report_dir(args)

    t0_build = time.monotonic()
    binary = build_harness(args.corpus / '.harness')
    t_build = time.monotonic() - t0_build

    scene_filter = parse_scenes_arg(args.scene)
    frame_matcher = parse_frame_spec(args.frames) if getattr(args, 'frames', None) else None

    scene_dirs = sorted(p for p in args.corpus.iterdir() if (p / 'dumps').is_dir())
    if scene_filter:
        scene_dirs = [p for p in scene_dirs if p.name in scene_filter]

    if not scene_dirs:
        if scene_filter:
            print(f'no matching scenes found under {args.corpus} for {sorted(scene_filter)}')
        else:
            print(f'no dumps under {args.corpus}; run "{Path(sys.argv[0]).name} capture" first')
        return 1

    scene_chains = {}
    selected_frames = []
    dependency_prefix = {}

    for scene_dir in scene_dirs:
        recipe = json.loads((scene_dir / 'recipe.json').read_text()) if (scene_dir / 'recipe.json').exists() else {}
        dumps = sorted((scene_dir / 'dumps').glob('frame-*.bin'), key=lambda p: int(p.stem.split('-')[1]))
        at = recipe.get('at', [])

        scene_frame_objs = []
        for dump in dumps:
            meta = json.loads(dump.with_suffix('.json').read_text()) if dump.with_suffix('.json').exists() else {}
            window = meta.get('dump_window', 0)
            idx = int(dump.stem.split('-')[1])
            is_selected = frame_matcher(idx, dump.stem) if frame_matcher else True
            scene_frame_objs.append({
                'scene': scene_dir.name,
                'frame': dump.stem,
                'dump': dump,
                'vclock_s': round(meta.get('vclock_ns', 0) / 1e9, 2),
                'window': window,
                'at': at[window - 1] if 0 < window <= len(at) else None,
                'capped': meta.get('capped'),
                'live': scene_dir / 'probe' / f'framebuffer-at-{round(at[window - 1])}s.ppm'
                        if 0 < window <= len(at) else None,
                'selected': is_selected,
                'is_prefix': False,
            })

        matching_indices = [i for i, f in enumerate(scene_frame_objs) if f['selected']]
        if not matching_indices:
            continue

        if args.single:
            chosen = [scene_frame_objs[i] for i in matching_indices]
            scene_chains[scene_dir.name] = chosen
            selected_frames.extend(chosen)
        else:
            last_idx = max(matching_indices)
            chain = scene_frame_objs[:last_idx + 1]
            prefix_names = []
            for f in chain:
                if not f['selected']:
                    f['is_prefix'] = True
                    prefix_names.append(f['frame'])
                else:
                    selected_frames.append(f)
            if prefix_names:
                dependency_prefix[scene_dir.name] = prefix_names
            scene_chains[scene_dir.name] = chain

    if not selected_frames:
        print(f'no dumps matched the specified filter (scenes: {args.scene}, frames: {getattr(args, "frames", None)})')
        return 1

    t0_c = time.monotonic()
    with futures.ThreadPoolExecutor(args.jobs) as threads:
        if args.single:
            c_jobs = {threads.submit(replay_c, binary, f['dump'],
                                     report / f'{f["scene"]}-{f["frame"]}.png'): [f] for f in selected_frames}
        else:
            c_jobs = {threads.submit(replay_c_chain, binary, chain, report): chain
                      for chain in scene_chains.values()}
        for job, group in c_jobs.items():
            result = job.result()
            for f, info in zip(group, result if isinstance(result, list) else [result]):
                f['c'] = info
    t_c = time.monotonic() - t0_c

    t0_py = time.monotonic()
    if not getattr(args, 'c_only', False):
        with futures.ProcessPoolExecutor(args.jobs) as processes:
            py_jobs = {processes.submit(audit_python, f['dump']): f for f in selected_frames}
            for job, f in py_jobs.items():
                f['py'] = job.result()
    else:
        for f in selected_frames:
            f['py'] = {'draws': 0, 'outcomes': [], 'suppressed': True}
    t_py = time.monotonic() - t0_py

    t0_export = time.monotonic()
    with futures.ThreadPoolExecutor(args.jobs) as threads:
        live = {}
        for f in selected_frames:
            if f['live'] and f['live'].exists() and f['live'] not in live:
                dst = report / f'live-{f["scene"]}-{round(f["at"])}s.png'
                live[f['live']] = threads.submit(ppm_to_png, f['live'], dst), dst.name
        for f in selected_frames:
            f['live_png'] = live[f['live']][1] if f['live'] in live else None
        for job, _ in live.values():
            job.result()

    total_prefix_frames = sum(len(chain) - sum(1 for f in chain if f.get('selected'))
                              for chain in scene_chains.values())
    totals = {
        'frames': len(selected_frames),
        'selected_frames': len(selected_frames),
        'prefix_frames': total_prefix_frames,
        'c_only': bool(getattr(args, 'c_only', False)),
        'c_errors': 0,
        'c_draws': 0,
        'c_skips': {},
        'py_errors': 0,
        'py_draws': 0,
        'py_outcomes': {},
    }
    for f in selected_frames:
        c, py = f['c'], f['py']
        if 'error' in c:
            totals['c_errors'] += 1
        else:
            totals['c_draws'] += c['draws']
            for reason, n in c.get('skips', {}).items():
                totals['c_skips'][reason] = totals['c_skips'].get(reason, 0) + n
        if 'error' in py:
            totals['py_errors'] += 1
        elif not py.get('suppressed'):
            totals['py_draws'] += py.get('draws', 0)
            for _, res in py.get('outcomes', []):
                totals['py_outcomes'][res] = totals['py_outcomes'].get(res, 0) + 1
        f['py_skipped'] = [[d, r] for d, r in py.get('outcomes', []) if r.startswith('skipped')]

    t_export = time.monotonic() - t0_export
    t_total = time.monotonic() - start_total

    totals['timings'] = {
        'build_s': round(t_build, 3),
        'c_replay_s': round(t_c, 3),
        'py_audit_s': round(t_py, 3),
        'export_s': round(t_export, 3),
        'total_s': round(t_total, 3),
    }
    totals['seconds'] = round(t_total, 1)

    summary = {
        'totals': totals,
        'dependency_prefix': dependency_prefix,
        'filter': {
            'scenes': sorted(list(scene_filter)) if scene_filter else None,
            'frames': getattr(args, 'frames', None),
            'c_only': bool(getattr(args, 'c_only', False)),
        },
        'frames': [
            {k: (str(v) if isinstance(v, Path) else v) for k, v in f.items()
             if k not in ('py', 'live')} | {
                 'py_draws': None if f['py'].get('suppressed') else f['py'].get('draws'),
                 'py_error': f['py'].get('error'),
             }
            for f in selected_frames
        ]
    }
    (report / 'summary.json').write_text(json.dumps(summary, indent=1) + '\n')
    write_html(report / 'index.html', selected_frames, totals)

    timings = totals['timings']
    prefix_str = f' ({total_prefix_frames} dependency passes)' if total_prefix_frames else ''
    print(f'{len(selected_frames)} dumps{prefix_str} in {timings["total_s"]} s -> {report / "index.html"}')
    print(f'Timings: build {timings["build_s"]}s, C replay {timings["c_replay_s"]}s, '
          f'Python audit {timings["py_audit_s"]}s, export {timings["export_s"]}s')
    print(f'C replay: {totals["c_draws"]} draws drawn, {totals["c_errors"]} harness errors')
    for reason, n in sorted(totals['c_skips'].items(), key=lambda kv: -kv[1]):
        print(f'  C skipped {n:5}  {reason}')
    if getattr(args, 'c_only', False):
        print('Python audit: suppressed (--c-only)')
    else:
        print(f'Python audit: {totals["py_draws"]} draws, {totals["py_errors"]} dump errors')
        for res, n in sorted(totals['py_outcomes'].items(), key=lambda kv: -kv[1]):
            print(f'  {n:5}  {res}')
    failing = [f for f in selected_frames if 'error' in f['c'] or f['c'].get('skips') or f['py_skipped'] or 'error' in f['py']]
    for f in failing:
        c_skip = sum(f['c'].get('skips', {}).values())
        print(f'  {f["scene"]}/{f["frame"]} t={f["vclock_s"]}s: C skipped {c_skip}, '
              f'Python skipped {len(f["py_skipped"])}' +
              (f', C error {f["c"]["error"]}' if 'error' in f['c'] else '') +
              (f', Python error {f["py"]["error"]}' if 'error' in f['py'] else ''))
    return 0


def write_html(path, frames, totals):
    import html
    cards = []
    for f in frames:
        c = f['c']
        lines = [f'<b>{f["scene"]}/{f["frame"]}</b> t={f["vclock_s"]}s window {f["window"]} (at {f["at"]}s)']
        if 'error' in c:
            lines.append(f'<span class=bad>C error: {html.escape(c["error"])}</span>')
        else:
            lines.append(f'{c["width"]}x{c["height"]} {"clear" if c["clear"] else "load"} '
                         f'{"strip" if c["strip"] else "pitch " + str(c["pitch"])}, C draws {c["draws"]}')
            for reason, n in c.get('skips', {}).items():
                lines.append(f'<span class=bad>C skipped {n}: {html.escape(reason)}</span>')
            if c.get('missing_rows'):
                lines.append(f'<span class=bad>{c["missing_rows"]} target rows unmapped</span>')
        if not f.get('py', {}).get('suppressed'):
            if 'error' in f['py']:
                lines.append(f'<span class=bad>Python: {html.escape(f["py"]["error"])}</span>')
            for did, res in f.get('py_skipped', []):
                lines.append(f'<span class=bad>py draw {did}: {html.escape(res)}</span>')
        imgs = '' if 'error' in c else f'<img src="{f["scene"]}-{f["frame"]}.png" title="C replay">'
        if f.get('live_png'):
            imgs += f'<img src="{f["live_png"]}" title="live framebuffer at {f["at"]}s">'
        cards.append(f'<div class=card><div class=imgs>{imgs}</div><p>{"<br>".join(lines)}</p></div>')
    skip_rows = ''.join(f'<tr><td>{html.escape(r)}</td><td>{n}</td></tr>'
                        for r, n in sorted(totals['c_skips'].items(), key=lambda kv: -kv[1]))
    if totals.get('c_only'):
        py_section = '<p><em>Python audit suppressed (--c-only)</em></p>'
    else:
        py_rows = ''.join(f'<tr><td>{html.escape(r)}</td><td>{n}</td></tr>'
                          for r, n in sorted(totals['py_outcomes'].items(), key=lambda kv: -kv[1]))
        py_section = f'<table>{py_rows}</table>'
    timings = totals.get('timings', {})
    timings_str = (f'build {timings.get("build_s", 0)}s | C replay {timings.get("c_replay_s", 0)}s | '
                   f'Python audit {timings.get("py_audit_s", 0)}s | export {timings.get("export_s", 0)}s')
    prefix_str = f' ({totals.get("prefix_frames", 0)} dependency passes)' if totals.get('prefix_frames') else ''
    path.write_text(f'''<!doctype html><meta charset=utf-8><title>AGX corpus</title>
<style>body{{font:13px system-ui;margin:16px;background:#fff;color:#111}}
.grid{{display:flex;flex-wrap:wrap;gap:12px}}.card{{width:340px}}
.imgs{{display:flex;gap:4px;align-items:flex-start}}.imgs img{{width:160px;border:1px solid #ccc}}
.bad{{color:#b00}}td{{padding:1px 8px}}.meta{{color:#555;margin-bottom:12px}}</style>
<h1>AGX corpus: {totals["frames"]} dumps{prefix_str}, {totals["seconds"]} s</h1>
<p class=meta>Timings: {timings_str}</p>
<p>Left: C replay of the dumped pass (the renderer QEMU runs). Right: live framebuffer at the window's capture time.</p>
<h2>C replay skipped draws</h2><table>{skip_rows or "<tr><td>none</td></tr>"}</table>
<h2>Python audit</h2>{py_section}
<h2>Frames</h2><div class=grid>{"".join(cards)}</div>
''')


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', nargs='?', default='replay', choices=('capture', 'replay'))
    parser.add_argument('--corpus', type=Path, default=CORPUS)
    parser.add_argument('--scene', action='append',
                        help='scene to capture or replay (repeatable or comma-separated)')
    parser.add_argument('--frames', '--frame', dest='frames', type=str, default=None,
                        help='frame or range of frames to replay (e.g. 5, 5..10, 5-10, :10, 10.., frame-5, or comma-separated)')
    parser.add_argument('--c-only', '--fast', dest='c_only', action='store_true',
                        help='C-only fast mode: suppress Python offline audit')
    parser.add_argument('--output', '-o', type=Path, default=None,
                        help='explicit directory for report output (never deletes existing reports)')
    parser.add_argument('--unique-output', action='store_true',
                        help='always create a unique timestamped report directory')
    parser.add_argument('--per-at', type=int, default=2)
    parser.add_argument('--min-draws', type=int, default=1)
    parser.add_argument('--jobs', type=int, default=os.cpu_count())
    parser.add_argument('--single', action='store_true',
                        help='replay each dump on its own memory instead of chaining a scene')
    args = parser.parse_args()
    if args.command == 'capture':
        capture(args)
    else:
        sys.exit(replay(args))


if __name__ == '__main__':
    main()
