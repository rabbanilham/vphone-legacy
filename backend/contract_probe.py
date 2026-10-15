#!/usr/bin/env python3
"""Probe 12 preset and conservative, evidence-linked contract ladder report.

Run with --output PATH, or regenerate a report with --summarize PATH.
Additional verified hooks may be supplied as --breakpoint name=0xVA.
"""
import argparse
import hashlib
import posixpath
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / 'backend/evidence/sep-software-model-20261015'
PROBE11 = EVIDENCE / 'native-art-persisted-probe-11'
PHASES = ('preparation', 'build', 'fixture', 'guest_running', 'paused_inspection', 'cleanup')


def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def integer(value):
    return int(value, 0) if isinstance(value, str) else int(value)


def qualified(event):
    """A retained return must match both its entry thread and stack."""
    ret = event.get('return_of', {})
    regs = event.get('registers', [])
    return bool(ret.get('thread') and len(regs) > 31 and
                event.get('identity', {}).get('thread') == ret['thread'] and
                integer(regs[31]) == integer(ret.get('stack', -1)))


def loader_blocker(events):
    """Identify one native failed open and its errno on the same keybagd thread."""
    expected = '/private/var/keybags/systembag.kb'
    for ei, error in enumerate(events):
        ec = error.get('contract_observation', {})
        identity = error.get('identity', {})
        errno = ec.get('errno', {})
        if (error.get('name') != 'keybagd_errno_before' or error.get('return_of') or
                identity.get('process') != 'keybagd' or not identity.get('thread') or
                ec.get('verified_user_mapping') is not True or
                posixpath.normpath(ec.get('path', '')) != expected or
                errno.get('readable') is not True or integer(errno.get('value', -1)) != 2):
            continue
        for oi in range(ei - 1, -1, -1):
            opened = events[oi]
            oc = opened.get('contract_observation', {})
            identity_open = opened.get('identity', {})
            if opened.get('name') != 'keybagd_open_result_before' or identity_open.get('thread') != identity['thread']:
                continue
            # The nearest open on this thread must be the same failed operation.
            if (not opened.get('return_of') and identity_open.get('process') == 'keybagd' and
                    oc.get('verified_user_mapping') is True and
                    posixpath.normpath(oc.get('path', '')) == expected and
                    integer(oc.get('fd', 0)) & 0xffffffffffffffff == 0xffffffffffffffff):
                return dict(kind='missing_restored_systembag', errno=2, errno_name='ENOENT',
                            path=ec['path'], normalized_path=expected,
                            detail='Native keybagd open of the restored systembag .kb input returned -1; its matching errno read is ENOENT. No loaded keybag or selector 5 success is established.',
                            evidence=[f'breakpoints.json#/{oi}', f'breakpoints.json#/{ei}'])
            break
    return None


def summarize(output, phases_path=None):
    output = Path(output).resolve()
    events = read(output / 'breakpoints.json', [])
    observations = read(output / 'observations.json', {})
    phases = read(Path(phases_path), {}) if phases_path else read(output / 'phases.json', {})
    preparation = read(output / 'preparation-timing.json', {})
    if 'seconds' in preparation:
        phases['preparation'] = preparation['seconds']
    # Clock starts after media preparation. Report the measured pause overhead,
    # never equate subprocess wall duration with actual guest execution.
    if 'guest_running_seconds' in observations:
        phases['guest_running'] = observations['guest_running_seconds']
        phases['paused_inspection'] = max(0, observations['wall_seconds'] - observations['guest_running_seconds'])
    milestones = []

    def milestone(name, indices, reason):
        milestones.append(dict(name=name, status='observed' if indices else 'unobserved',
                               evidence=[f'breakpoints.json#/{i}' for i in indices], detail=reason))

    selector14 = [i for i, e in enumerate(events) if e.get('name') == 'aks_external_method' and
                  qualified(e) and integer(e['registers'][0]) & 0xffffffff == 0xe00002f0 and
                  integer(e['return_of'].get('argument1', -1)) == 14 and
                  e.get('identity', {}).get('process') == 'keybagd']
    milestone('selector14_not_found', selector14, 'Qualified keybagd selector 14 return equals 0xe00002f0.')
    branches = [i for i, e in enumerate(events) if e.get('name') == 'keybagd_systembag_load_before' and
                not e.get('return_of') and e.get('identity', {}).get('process') == 'keybagd' and
                e.get('contract_observation', {}).get('verified_user_mapping') is True]
    milestone('keybagd_systembag_loading', branches, 'Requires a verified executable mapping at the systembag-loading branch; strings alone do not qualify.')
    # Ready is stored inside the unlock handler; the userclient wakes waiters
    # after it returns. Keep those distinct observations linked by provider.
    readiness = []
    wake_index = None
    for wi, wake in enumerate(events):
        wc = wake.get('contract_observation', {})
        provider = wc.get('provider')
        if (wake.get('name') != 'aks_wakeup_before' or wake.get('return_of') or
                not provider or integer(provider) == 0 or wc.get('ready_flag') != 1):
            continue
        ready = [i for i, e in enumerate(events[:wi]) if e.get('name') == 'aks_ready_before' and
                 e.get('contract_observation', {}).get('provider') == provider and
                 e.get('contract_observation', {}).get('ready_flag') == 1]
        unlock = [i for i, e in enumerate(events[:wi]) if e.get('name') == 'aks_system_unlock' and
                  qualified(e) and integer(e['registers'][0]) & 0xffffffff == 0 and
                  e.get('contract_observation', {}).get('provider') == provider and
                  e.get('identity', {}).get('process') == 'keybagd']
        pairs = [(ri, ui) for ri in ready for ui in unlock if ri < ui]
        if pairs:
            readiness = [*pairs[0], wi]
            wake_index = wi
            break
    milestone('selector5_ready_and_wakeup', readiness, 'Requires ready byte 1, successful qualified keybagd unlock return, and subsequent wake observation for the same provider.')
    for process in ('installd', 'lsd', 'backboardd', 'SpringBoard'):
        indices = []
        if wake_index is not None:
            for i, e in enumerate(events[wake_index + 1:], wake_index + 1):
                if e.get('identity', {}).get('process') != process:
                    continue
                direct = e.get('contract_observation', {}).get('progress_after_ready') is True
                if process == 'installd':
                    direct = direct or (e.get('name') == 'aks_external_method' and qualified(e) and
                                        integer(e['return_of'].get('argument1', -1)) == 17 and
                                        integer(e['registers'][0]) & 0xffffffff == 0)
                else:
                    progress_hooks = {'lsd': 'ls_lookup', 'backboardd': 'ls_plugin_store', 'SpringBoard': 'bks_checkin'}
                    direct = direct or (e.get('name') == progress_hooks[process] and qualified(e))
                if direct:
                    indices.append(i)
        milestone(f'{process}_progress', indices, 'Requires direct progress after the matched readiness/wake transition; process existence alone does not qualify.')
    frame = read(output / 'framebuffer_analysis.json', {})
    candidate = read(output / 'ca-scanout-candidate.json', {})
    attribution = read(output / 'ca-frame-attribution.json', {})
    # The existing candidate gate is necessary but explicitly not final attribution.
    accepted = bool(candidate and frame.get('non_zero_rgb_bytes', 0) > 0 and
                    attribution.get('surface_commit_attributed') is True and
                    attribution.get('physical_swap_completed') is True and
                    attribution.get('visually_verified') is True and
                    attribution.get('framebuffer_sha256') == frame.get('sha256'))
    milestones.append(dict(name='ca_physical_scanout', status='observed' if accepted else 'unobserved',
                           evidence=[p for p in ('ca-scanout-candidate.json', 'framebuffer_analysis.json', 'ca-frame-attribution.json') if (output / p).exists()],
                           detail='Acceptance requires matching framebuffer hash, CA attribution, physical completion, nonzero RGB and visual verification.'))
    first = next((m['name'] for m in milestones if m['status'] != 'observed'), None)
    blocker = loader_blocker(events)
    loader_stop = read(output / 'loader-stop.json', None)
    contract_path = output.parent / 'native-contract.json'
    native_contract = None
    if contract_path.exists():
        contract = read(contract_path, {})
        native_contract = dict(evidence='../native-contract.json', sha256=hashlib.sha256(contract_path.read_bytes()).hexdigest(),
                               kernel_sha256=contract.get('kernel_sha256'), boot_image_sha256=contract.get('boot_image_sha256'))
    baseline = read(PROBE11 / 'observations.json', {})
    media = read(output / 'diagnostic-disk-cleanup.json', {})
    media_verified = (set(media) == {'system', 'data', 'utility'} and
                      all(v.get('source_unchanged') is True for v in media.values()))
    report = dict(schema_version=1, blocker=blocker, native_contract=native_contract,
                  outcome=dict(kernel_panic=observations.get('kernel_panic'), loader_stop=loader_stop,
                               budget_semantics='Stopped at the captured loader failure; unused guest budget does not establish boot success.' if loader_stop else 'Guest duration is measured execution time; the configured budget is a maximum.'),
                  next_hypothesis=dict(
                      statement='After the corrected SEP query reaches native systembag loading, the restored systembag .kb input is missing (ENOENT); the current blocker is missing loader input, not a selector 14 query-status mismatch.' if blocker and selector14 and branches else 'Continue from the first unobserved milestone; missing instrumentation is not a proven guest failure.',
                      loaded_state_support='No import/load contract has been proven by these observations. Keep loaded-state support unclaimed until native callers establish the import contract; do not force a loaded or unlocked state.'),
                  hypothesis=dict(
                  requirement='Absent system keybag must return signed SEP status -3 (0xfd), yielding selector 14 kIOReturnNotFound; command 25 returns exactly eight bytes.',
                  expected='keybagd takes the systembag-loading branch, selector 5 establishes readiness and wakes waiters, followed by application CA scanout.',
                  falsification='A qualified selector 14 status other than 0xe00002f0, or a later directly observed failed milestone, rejects the corresponding hypothesis.',
                  native_contract_evidence='Supply hash-bound guest instruction and error-table evidence with the run; this report does not infer native contracts from emulator output.'), first_unobserved_milestone=first,
                  interpretation='Unobserved means the retained evidence does not prove the milestone; it does not prove the guest failed to reach it.',
                  milestones=milestones, phases_seconds={k: phases.get(k) for k in PHASES},
                  timing_semantics='paused_inspection is all measured clock overhead while guest execution was paused; null means no retained measurement.',
                  probe11_seconds=dict(guest_running=baseline.get('guest_running_seconds'), probe_wall=baseline.get('wall_seconds')),
                  probe_seconds=dict(guest_running=observations.get('guest_running_seconds'), probe_wall=observations.get('wall_seconds')),
                  performance_claim='No speedup claim: Probe 11 has no retained preparation/build/fixture/cleanup phase measurements.',
                  process_reaped=read(output / 'process-exit.json', {}).get('reaped', False),
                  source_media_verified=media_verified)
    (output / 'contract-report.json').write_text(json.dumps(report, indent=2) + '\n')
    lines = ['# Probe 12 contract ladder', '', report['interpretation'], '', f'First unobserved milestone: **{first or "none"}**', '']
    if blocker:
        lines += [blocker['detail'], '', 'Blocker evidence: ' + ', '.join(f'[{ref}]({ref})' for ref in blocker['evidence']), '', report['next_hypothesis']['statement'], '', report['next_hypothesis']['loaded_state_support'], '']
    if native_contract:
        lines += [f'Static native contract: [native-contract.json](../native-contract.json), SHA-256 `{native_contract["sha256"]}`.', '']
    for m in milestones:
        links = ', '.join(f'[{ref}]({ref})' for ref in m['evidence']) or 'No qualifying evidence'
        lines.append(f'- **{m["name"]}: {m["status"]}** — {links}. {m["detail"]}')
    lines += ['', 'Phase durations (seconds): ' + ', '.join(f'{k}={report["phases_seconds"][k]}' for k in PHASES), '', report['performance_claim'], '']
    (output / 'contract-report.md').write_text('\n'.join(lines))
    return report


def command(output, seconds=90, extra_hooks=()):
    invocation = read(PROBE11 / 'probe-invocation.json')
    if not invocation:
        raise FileNotFoundError('Probe 11 invocation is required for the persisted-ART preset')
    user_hooks = ROOT / 'backend/evidence/keybag-contract-20261015/user-hooks.json'
    def argument(name):
        return invocation[invocation.index(name) + 1]
    return [sys.executable, str(ROOT / 'backend/display_probe.py'), '--output', str(output),
            '--plan', argument('--plan'), '--diagnostic-system', argument('--diagnostic-system'),
            '--user-hooks', str(user_hooks) if user_hooks.exists() else argument('--user-hooks'), '--returns', '--seconds', str(seconds),
            '--stop-condition', 'ca-scanout',
            '--breakpoint', 'sep_start=0xffffff8000d4748c',
            '--breakpoint', 'userclient_start=0xffffff8000c5cf18',
            '--breakpoint', 'swap_submit=0xffffff8000c5d810',
            '--breakpoint', 'aks_external_method=0xffffff8000daee48',
            '--breakpoint', 'aks_system_unlock=0xffffff8000dab8d0',
            '--breakpoint', 'aks_ready_before=0xffffff8000dab964',
            '--breakpoint', 'aks_wakeup_before=0xffffff8000dae450',
            *[v for hook in extra_hooks for v in ('--breakpoint', hook)]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--output', type=Path)
    modes.add_argument('--summarize', type=Path)
    parser.add_argument('--seconds', type=float, default=90)
    parser.add_argument('--phases', type=Path, help='JSON object of measured phase seconds; missing phases stay null')
    parser.add_argument('--breakpoint', action='append', default=[])
    parser.add_argument('--keybagd-loader-hooks', action='store_true')
    args = parser.parse_args()
    if args.summarize:
        print(json.dumps(summarize(args.summarize, args.phases), indent=2))
        return
    if not 0 < args.seconds <= 180:
        parser.error('--seconds must be greater than 0 and at most 180')
    output = args.output.resolve()
    if output.exists():
        parser.error('--output must not already exist')
    invocation = command(output, args.seconds, args.breakpoint)
    if args.keybagd_loader_hooks:
        invocation.append('--keybagd-loader-hooks')
    started = time.monotonic()
    result = None
    try:
        result = subprocess.run(invocation, cwd=ROOT, check=False)
    finally:
        if output.exists():
            phase_data = read(args.phases, {}) if args.phases else {}
            phase_data['runner_wall_before_cleanup'] = time.monotonic() - started
            exited = read(output / 'process-exit.json', {})
            if exited.get('reaped') and (output / 'provenance.json').exists():
                from probe_cleanup import cleanup
                cleanup_started = time.monotonic()
                cleanup(output)
                phase_data['cleanup'] = time.monotonic() - cleanup_started
            (output / 'phases.json').write_text(json.dumps(phase_data, indent=2) + '\n')
            summarize(output)
    if result is not None:
        raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()
