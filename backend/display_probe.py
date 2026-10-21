#!/usr/bin/env python3
"""Bounded native display evidence on independent disks; never synthesizes pixels."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import select
import re
import shutil
import socket
import struct
import sys
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET

from smoke_test import command
from breakpoint_probe import rsp, receive_rsp
from probe_state import RunClock, ReturnRegistry, ScanoutTracker, pixel_present, instruction_mapped

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = Path('/Users/bagas/.gemini/antigravity/brain/4f2c8eb8-288b-4247-84a2-e745314f5c38/scratch')
PLAN = ROOT / 'build/ios712-validated/boot/boot_plan.json'
FB = 0x8018e0000
FB_SIZE = 2560 * 1136


def digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def save_json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def memory(gs, addr, size):
    reply = rsp(gs, f'm{addr:x},{size:x}')
    # QMP stop may queue the outstanding GDB continue's stop reply ahead
    # of this memory response. Consume it without issuing a second read.
    while reply.startswith(('T','S')):
        reply=receive_rsp(gs)
    return bytes.fromhex(reply) if not reply.startswith('E') else b''


def parse_hook_dumps(items):
    """Bounded opt-in reads; multiple registers may be selected per hook."""
    result = {}
    for item in items:
        name, spec = item.split('=', 1)
        address, length = spec.split(':', 1)
        match = re.fullmatch(r'(x(?:[0-9]|[12][0-9]|30)|sp)([+-](?:0x[0-9a-fA-F]+|[0-9]+))?', address)
        if not name or not match:
            raise ValueError('hook dump requires name=x0..x30|sp[+/-offset]:length')
        reg, displacement = match.groups()
        offset = int(displacement, 0) if displacement else 0
        if abs(offset) > 4096:
            raise ValueError('hook dump offset must be -4096..4096')
        length = int(length, 0)
        if not 1 <= length <= 4096:
            raise ValueError('hook dump length must be 1..4096')
        result.setdefault(name, []).append((31 if reg == 'sp' else int(reg[1:]), length, offset))
    return result


def capture_hook_dumps(gs, registers, specs):
    result = []
    for reg, length, offset in specs:
        base = registers[reg]
        addr = base + offset
        data = memory(gs, addr, length) if base and 0 < addr < (1 << 64) and addr + length <= (1 << 64) else b''
        result.append(dict(register='sp' if reg == 31 else f'x{reg}', offset=offset, address=hex(addr),
                           requested=length, readable=len(data) == length,
                           bytes=data.hex()))
    return result


def u64(gs, addr):
    data = memory(gs, addr, 8)
    return int.from_bytes(data, 'little') if len(data) == 8 else 0


def memory_state(gs,addr,size=8):
    data=memory(gs,addr,size)
    return dict(readable=len(data)==size, value=hex(int.from_bytes(data,'little')) if len(data)==size else None, bytes=data.hex())


def dispatch_release_evidence(gs, registers):
    """Read the caller's surviving block/continuation; do not infer their types.

    At call_block_and_release's BLR, x19 saves the block. A tail-called
    dispatch_release preserves that register, but other callers need not.
    """
    result = {}
    for label, reg in (('object', 0), ('block_candidate', 19),
                       ('continuation_candidate', 27), ('stack', 31)):
        addr = registers[reg]
        data = memory(gs, addr, 128) if 0 < addr < (1 << 64) - 128 else b''
        result[label] = dict(address=hex(addr), bytes=data.hex(), readable=len(data) == 128)
        if label == 'block_candidate' and len(data) >= 32:
            invoke, descriptor = struct.unpack_from('<QQ', data, 16)
            result[label].update(invoke=hex(invoke), descriptor=hex(descriptor))
            for key, ptr in (('invoke_bytes', invoke), ('descriptor_bytes', descriptor)):
                result[label][key] = memory(gs, ptr, 64).hex() if 0 < ptr < (1 << 64) - 64 else ''
    # The observed drain keeps the pthread/TLS base in x24. Its current
    # queue slot is +0xa0; verify the captured header before interpreting it.
    tls = registers[24]
    slot = memory(gs, tls + 0xa0, 8) if 0 < tls < (1 << 64) - 0xa8 else b''
    if len(slot) == 8:
        queue = int.from_bytes(slot, 'little')
        head = memory(gs, queue, 128) if 0 < queue < (1 << 64) - 128 else b''
        result['queue_candidate'] = dict(address=hex(queue), bytes=head.hex(), readable=len(head) == 128)
        if len(head) >= 0x68:
            label = int.from_bytes(head[0x60:0x68], 'little')
            result['queue_candidate']['label_bytes'] = memory(gs, label, 128).hex() if 0 < label < (1 << 64) - 128 else ''
    return result


def derive_exe_slide(gs, mem_fn, proc, expected_header, known_sites=None, lr=None, hook_name=None, max_scan_bytes=0x400000, preferred_base=0x100000000):
    """Derive executable ASLR slide from verified LR at a known return site or by scanning text pages."""
    if known_sites and hook_name and hook_name in known_sites and lr:
        preferred = int(known_sites[hook_name], 0)
        cand = lr - preferred
        if cand >= 0 and (cand & 0xfff) == 0:
            hdr = mem_fn(gs, preferred_base + cand, len(expected_header))
            if len(hdr) == len(expected_header) and hdr == expected_header:
                return cand
    for step in (0x4000, 0x1000):
        for s in range(0, max_scan_bytes, step):
            hdr = mem_fn(gs, preferred_base + s, len(expected_header))
            if len(hdr) == len(expected_header) and hdr == expected_header:
                return s
    return None


def install_exe_hooks(gs, mem_fn, rsp_fn, proc, slide, hooks_dict, expected_dict, bps, user_expected, exe_path=None, preferred_base=0x100000000):
    """Verify instruction bytes against expected and install Z0 breakpoints for executable hooks."""
    disk_data = Path(exe_path).read_bytes() if exe_path and Path(exe_path).exists() else None
    verified = []
    for uname, spec in hooks_dict.items():
        if isinstance(spec, dict):
            static = int(spec.get('address') or spec.get('static'), 0)
            exp_hex = spec.get('expected')
        else:
            static = int(spec, 0)
            exp_hex = expected_dict.get(uname)
        if exp_hex:
            expected = bytes.fromhex(exp_hex)
        elif disk_data:
            off = static - preferred_base
            expected = disk_data[off:off+16] if 0 <= off <= len(disk_data) - 16 else b''
        else:
            expected = b''
        runtime = static + slide
        actual = mem_fn(gs, runtime, len(expected)) if expected else b''
        assert expected and (not actual or actual == expected), (uname, actual.hex(), expected.hex())
        assert rsp_fn(gs, f'Z0,{runtime:x},4') == 'OK'
        bps[runtime] = uname
        user_expected[runtime] = expected
        verified.append(dict(name=uname, static=hex(static), runtime=hex(runtime),
                             bytes=actual.hex(), expected=expected.hex(),
                             mapped_at_install=bool(actual), exe=True, process=proc))
    return verified


def sysreg_numbers(gs):
    # XML negotiation also enables access to registers outside the core packet.
    rsp(gs, 'qSupported:qXfer:features:read+')
    xml = ''; offset = 0
    while True:
        reply = rsp(gs, f'qXfer:features:read:system-registers.xml:{offset:x},1000')
        assert reply and reply[0] in 'ml', reply
        xml += reply[1:]; offset += len(reply) - 1
        if reply[0] == 'l': break
    return {r.attrib['name']: int(r.attrib['regnum']) for r in ET.fromstring(xml).iter('reg')}


def identity(gs, regs):
    number = regs.get('TPIDR_EL1')
    if number is None: return dict(unresolved='TPIDR_EL1 not exported')
    reply = rsp(gs, f'p{number:x}')
    if reply.startswith('E'): return dict(unresolved=reply)
    thread = int.from_bytes(bytes.fromhex(reply), 'little')
    task = u64(gs, thread + 0x6b0)
    result = dict(thread=hex(thread), thread_id=u64(gs, thread + 0x740), task=hex(task))
    mask, table = u64(gs, 0xffffff800067c978), u64(gs, 0xffffff800067c980)
    if not table or mask > 4095: return result
    heads = memory(gs, table, (mask + 1) * 8)
    for bucket in range(0, len(heads), 8):
        proc = int.from_bytes(heads[bucket:bucket+8], 'little'); seen = set()
        while proc and proc not in seen and len(seen) < 256:
            seen.add(proc)
            if u64(gs, proc + 0x18) == task:
                result.update(proc=hex(proc), pid=int.from_bytes(memory(gs, proc+0x10, 4), 'little'),
                              process=memory(gs, proc+0x254, 16).split(b'\0')[0].decode(errors='replace'))
                return result
            proc = u64(gs, proc + 0xb0)
    return result


def properties(gs, service):
    table = u64(gs, service + 0x20)
    return dictionary(gs, table)


def dictionary(gs, table):
    count = int.from_bytes(memory(gs, table + 0x20, 4), 'little')
    entries = u64(gs, table + 0x18)
    if count > 256 or not entries: return dict(table=hex(table), count=count)
    result = {}
    for n in range(count):
        key, value = u64(gs, entries+n*16), u64(gs, entries+n*16+8)
        name = memory(gs, u64(gs, key+0x18), 128).split(b'\0')[0].decode(errors='replace')
        if not name: continue
        head = memory(gs, value, 0x30)
        result[name] = dict(object=hex(value), head=head.hex())
        if len(head)>=0x20 and int.from_bytes(head[:8],'little') == 0xffffff80005f1570:
            length = int.from_bytes(head[0x18:0x1c],'little')
            ptr = int.from_bytes(head[0x10:0x18],'little')
            if length<=1024: result[name]['data_hex'] = memory(gs,ptr,length).hex()
        candidate = u64(gs, value+0x18)
        if candidate >> 56 == 0xff:
            text = memory(gs, candidate, 128).split(b'\0')[0]
            if text and all(32 <= c < 127 for c in text): result[name]['text'] = text.decode()
    return result


def capture(fp, out, elapsed):
    raw = out / 'framebuffer_0x8018e0000.raw'
    command(fp, 'pmemsave', {'val': FB, 'size': FB_SIZE, 'filename': str(raw)})
    data = raw.read_bytes()
    assert len(data) == FB_SIZE
    nz = len(data) - data.count(0)
    rgb = bytearray(FB_SIZE // 4 * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = data[2::4], data[1::4], data[0::4]
    result = dict(physical_address=hex(FB), fb_size=len(data), non_zero_bytes=nz,
                  non_zero_rgb_bytes=len(rgb) - rgb.count(0), elapsed_seconds=elapsed,
                  capture_time_utc=datetime.now(timezone.utc).isoformat(),
                  sha256=hashlib.sha256(data).hexdigest())
    save_json(out / 'framebuffer_analysis.json', result)
    if nz:
        (out / 'framebuffer.ppm').write_bytes(b'P6\n640 1136\n255\n' + rgb)
    return nz


def main():
    preparation_started = time.monotonic()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--seconds', type=float, default=45)
    p.add_argument('--breakpoint', action='append', default=[], help='name=0xVA')
    p.add_argument('--returns', action='store_true')
    p.add_argument('--hook-dump', action='append', default=[], help='name=xN|sp[+/-offset]:length, bounded read-only register-relative dump (repeatable; offset +/-4096, length 1..4096)')
    p.add_argument('--user-hooks', type=Path, help='Static shared-cache hook addresses, installed after verified runtime slide')
    p.add_argument('--stop-condition', choices=('first-pixel','ca-scanout','duration'), default='first-pixel')
    p.add_argument('--step-count', type=int, default=1, help='Bounded instruction steps after each hook')
    p.add_argument('--diagnostic-system', type=Path, help='Alternative diagnostic system image')
    p.add_argument('--diagnostic-data', type=Path, help='Alternative diagnostic data image')
    p.add_argument('--plan', type=Path, help='Alternative boot_plan.json path')
    p.add_argument('--keybagd-loader-hooks', action='store_true', help='Trace native systembag file loading and stop on a proven open error')
    p.add_argument('--diagnostic-poke', action='append', default=[], choices=('meta_alive','stamps','device_status','drain'),
                   help='Diagnostic-only guest writes used to map later blockers; never acceptance evidence')
    p.add_argument('--meta-dump', action='store_true', help='On the first meta_wait, save AGXMetaFirmware state, pointer translations and guest RAM')
    p.add_argument('--snapshot-at', type=float, action='append', default=[], help='Guest-running seconds at which to save an extra CA thread snapshot (needs --user-hooks)')
    p.add_argument('--max-hits', action='append', default=[], help='name=N hit cap before a hook is removed (0 keeps it installed)')
    p.add_argument('--user-va-hook', action='append', default=[], help='name=0xVA absolute user-space breakpoint, installed with the user hooks (no cache verification)')
    p.add_argument('--qemu-arg', action='append', default=[], help='Extra QEMU argument, repeatable (e.g. --qemu-arg=-icount --qemu-arg=shift=4,sleep=off)')
    args = p.parse_args()
    try:
        hook_dumps = parse_hook_dumps(args.hook_dump)
    except ValueError as exc:
        p.error(str(exc))
    assert 0 < args.seconds <= 180
    assert 1 <= args.step_count <= 64
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    images = out / 'images'; images.mkdir()
    save_json(out/'probe-invocation.json',[sys.executable,*sys.argv])
    plan_path = args.plan.resolve() if args.plan else PLAN
    storage = plan_path.parent.parent / 'storage-before'
    if not storage.exists():
        storage = ROOT / 'build/ios712-validated'
    system_src = args.diagnostic_system.resolve() if args.diagnostic_system else storage / 'system.raw'
    data_src = args.diagnostic_data.resolve() if args.diagnostic_data else SCRATCH / 'data-seeded-clean.raw'
    sources = dict(system=system_src, data=data_src,
                   utility=storage / 'utility.raw')
    provenance = dict(plan=str(plan_path), plan_sha256=digest(plan_path), inputs={})
    def clone_or_copy(src, dst):
        try:
            import ctypes
            libc = ctypes.CDLL(None)
            if hasattr(libc, 'clonefile'):
                libc.clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
                libc.clonefile.restype = ctypes.c_int
                if libc.clonefile(str(src).encode(), str(dst).encode(), 0) == 0:
                    return
        except Exception:
            pass
        shutil.copyfile(src, dst)

    for kind, source in sources.items():
        target = images / (kind + '.raw')
        clone_or_copy(source, target)
        sha = digest(source)
        assert digest(target) == sha
        provenance['inputs'][kind] = dict(source=str(source), copy=str(target), sha256=sha)
    plan = json.loads(plan_path.read_text())
    binary = ROOT / 'backend/bin/qemu-system-aarch64'
    provenance.update(binary_sha256=digest(binary), source_sha256=digest(ROOT / 'backend/s5l8960x.c'))
    assert digest(plan['image']) == plan['image_sha256']
    provenance['boot_image_sha256'] = plan['image_sha256']
    provenance['auxiliary_images'] = []
    for image in plan.get('auxiliary_images', []):
        assert digest(image['path']) == image['sha256']
        assert Path(image['path']).stat().st_size == image['size']
        provenance['auxiliary_images'].append(image)
    save_json(out / 'provenance.json', provenance)
    bps = {int(item.split('=', 1)[1], 0): item.split('=', 1)[0] for item in args.breakpoint}
    # Every GDB stop costs a host round trip; high-rate hooks such as
    # swap_advancer turned 75 guest seconds into ~600 wall seconds.
    poke = set(args.diagnostic_poke)
    max_hits = {'accel_': 0, 'meta_': 0}
    max_hits.update({k: int(v, 0) for k, v in (item.split('=', 1) for item in args.max_hits)})
    def hit_cap(name, pc):
        for prefix in sorted(max_hits, key=len, reverse=True):
            if name.startswith(prefix):return max_hits[prefix]
        return 24 if pc in user_expected else 8
    user_hooks = json.loads(args.user_hooks.read_text()) if args.user_hooks else None
    user_installed = False
    user_expected = {}
    exe_installed = {}
    exe_slides = {}
    keybagd_installed = False
    ready_providers = set()
    wake_providers = set()
    events = []
    scanout=ScanoutTracker();pixel_hashes=set()
    sep_managers=set()
    kernel_expected={}
    with Path(plan['image']).open('rb') as boot_image:
        for addr in bps:
            if addr>=plan['virt_base']:
                offset=plan['kernel_phys_base']-plan['dram_base']+addr-plan['virt_base']
                boot_image.seek(offset);kernel_expected[addr]=boot_image.read(16)
    with tempfile.TemporaryDirectory(prefix='display-', dir='/tmp') as tmp:
        qmp = Path(tmp) / 'qmp'; gdb = Path(tmp) / 'gdb'
        machine = (f"s5l8960x,boot-el=3,boot-entry={plan['initial_entry_pa']:#x},"
                   f"boot-args={plan['initial_boot_args_pa']:#x},framebuffer={FB:#x},display-trace=1,"
                   f"system-image={images}/system.raw,data-image={images}/data.raw,"
                   f"utility-image={images}/utility.raw,system-writable=1")
        cmd = [str(binary), '-machine', machine, '-accel', 'tcg,thread=single',
               '-smp', '1', '-m', '1024M', '-kernel', plan['image'], '-display', 'none',
               '-serial', f'file:{out}/serial.log', '-S', '-qmp', f'unix:{qmp},server=on,wait=off',
               '-d', 'unimp,guest_errors', '-D', str(out / 'qemu-trace.log')] + args.qemu_arg
        if bps:
            cmd += ['-gdb', f'unix:{gdb},server=on,wait=off']
        for image in plan.get('auxiliary_images', []):
            cmd += ['-device', f"loader,file={image['path']},addr={image['address']:#x},force-raw=on"]
        save_json(out / 'command.json', cmd)
        with (out / 'stdout.log').open('wb') as stdout, (out / 'stderr.log').open('wb') as stderr:
            proc = subprocess.Popen(cmd, stdout=stdout, stderr=stderr)
            save_json(out / 'process.json', dict(pid=proc.pid))
            qs = gs = None
            try:
                deadline = time.monotonic() + 10
                while not qmp.exists() or (bps and not gdb.exists()):
                    assert proc.poll() is None, 'QEMU exited during startup'
                    assert time.monotonic() < deadline, 'QEMU socket timeout'
                    time.sleep(.01)
                qs = socket.socket(socket.AF_UNIX); qs.settimeout(5); qs.connect(str(qmp))
                with qs.makefile('rwb') as fp:
                    json.loads(fp.readline()); command(fp, 'qmp_capabilities')
                    save_json(out/'preparation-timing.json',dict(seconds=time.monotonic()-preparation_started))
                    clock=RunClock()
                    if bps:
                        gs = socket.socket(socket.AF_UNIX); gs.settimeout(5); gs.connect(str(gdb))
                        sysregs = sysreg_numbers(gs)
                        save_json(out / 'system-register-numbers.json', sysregs)
                        for addr in bps:
                            assert rsp(gs, f'Z0,{addr:x},4') == 'OK'
                        # Send continue without blocking; receive its reply only when readable.
                        def cont():
                            clock.resume()
                            gs.sendall(b'$c#63')
                        cont()
                    else:
                        command(fp, 'cont');clock.resume()
                    started = time.monotonic(); next_sample = started + 1
                    temporary = ReturnRegistry()
                    hook_counts={}
                    snapshot_at=sorted(args.snapshot_at)
                    serial_offset=0; serial_tail=b''; panicked=False
                    def freeze():
                        if gs and clock.resumed is not None:
                            gs.sendall(b'\x03');stop=receive_rsp(gs)
                            assert stop.startswith(('T','S')),stop
                        command(fp,'stop');clock.pause()
                    def pixel_stop():
                        analysis=json.loads((out/'framebuffer_analysis.json').read_text())
                        if args.stop_condition == 'duration': return False
                        if not pixel_present(analysis,args.stop_condition):return False
                        # Preserve transient console fallback even when continuing
                        # to seek an application CA scanout.
                        if analysis['sha256'] not in pixel_hashes:
                            pixel_hashes.add(analysis['sha256']);archive=out/'pixel-captures'/f'{len(pixel_hashes):03d}'
                            archive.mkdir(parents=True)
                            for file in ('framebuffer_analysis.json','framebuffer_0x8018e0000.raw','framebuffer.ppm'):
                                shutil.copyfile(out/file,archive/file)
                            command(fp,'screendump',{'filename':str(archive/'qmp-framebuffer.ppm')})
                        if args.stop_condition=='first-pixel':return True
                        views={}
                        for field,addr in (('base',0x206204044),('token',0x206201048)):
                            raw=command(fp,'human-monitor-command',{'command-line':f'xp /1xw {addr:#x}'})
                            values=re.findall(r'0x([0-9a-fA-F]+)',raw);views[field]=int(values[-1],16) if values else None
                        eligible=scanout.eligible(analysis['non_zero_rgb_bytes'],views['base'],views['token'])
                        if eligible:save_json(out/'ca-scanout-candidate.json',dict(needs_surface_commit_attribution=True,registers=views))
                        return eligible
                    while clock.elapsed() < args.seconds:
                        with (out/'serial.log').open('rb') as serial:
                            serial.seek(serial_offset); update=serial.read()
                            serial_offset=serial.tell()
                        serial_tail=(serial_tail+update)[-max(4096,len(update)):]
                        if b'panic(cpu' in serial_tail or b'Debugger message: panic' in serial_tail:
                            panicked=True;break
                        if gs:
                            if time.monotonic() >= next_sample:
                                # Observe without issuing stop/cont while GDB has
                                # a pending continue. On any pixel, freeze and
                                # recapture through the normal final path.
                                if capture(fp,out,clock.elapsed()):
                                    freeze();capture(fp,out,clock.elapsed())
                                    if pixel_stop():break
                                    cont()
                                if snapshot_at and clock.elapsed()>=snapshot_at[0] and user_hooks and (out/'user-hook-mapping.json').exists():
                                    at=snapshot_at.pop(0);freeze()
                                    from ca_probe_state import snapshot
                                    snapshot(gs,memory,u64,out,out/'user-hook-mapping.json',Path(user_hooks['index_path']),f'ca-thread-state-{int(at):03d}s.json')
                                    cont()
                                next_sample=time.monotonic()+1
                            if not select.select([gs], [], [], max(0, min(.1, args.seconds - clock.elapsed())))[0]:
                                continue
                            # rsp expects to send a request; receive the pending continue manually.
                            first = gs.recv(1)
                            if first == b'+':
                                continue
                            assert first == b'$', first
                            clock.pause()
                            payload=receive_rsp(gs,started=True)
                            regs = bytes.fromhex(rsp(gs, 'g'))
                            x = struct.unpack_from('<33Q', regs)
                            pc, lr = x[32], x[30]
                            if pc in kernel_expected:
                                actual=memory(gs,pc,16)
                                assert actual==kernel_expected[pc], (hex(pc),actual.hex(),kernel_expected[pc].hex())
                            name = bps.get(pc, 'return' if pc in temporary else 'unknown')
                            if name=='sep_ep_init' and pc in bps and x[5]!=7:
                                assert rsp(gs,'s').startswith('T');cont();continue
                            if name=='sep_handle' and pc in bps and x[3]!=7:
                                assert rsp(gs,'s').startswith('T');cont();continue
                            if name=='sep_ep_gate_before' and x[20]!=7:
                                assert rsp(gs,'s').startswith('T');cont();continue
                            if name=='sep_ready_wait_before' and x[19] not in sep_managers:
                                assert rsp(gs,'s').startswith('T');cont();continue
                            if name=='keystore_wait' and pc in bps:
                                matching=dictionary(gs,x[0])
                                if not any(v.get('text')=='AppleSEPManager' for v in matching.values() if isinstance(v,dict)):
                                    assert rsp(gs,'s').startswith('T');cont();continue
                            if name=='sep_publish' and pc in bps:
                                sep_props=properties(gs,x[0])
                                if sep_props.get('IOClass',{}).get('text')!='AppleSEPManager' and u64(gs,x[0])!=0xffffff8000d563e0:
                                    assert rsp(gs,'s').startswith('T');cont();continue
                            caller_identity = identity(gs,sysregs)
                            pending=temporary.match(pc,caller_identity.get('thread'),hex(x[31]))
                            if pc in temporary and pending is None and pc not in bps:
                                assert rsp(gs,'s').startswith('T');cont();continue
                            if pending:name=pending['name']
                            # Filter before hook expiry/return registration. The
                            # global UserClient entry is shared by many callers.
                            if name == 'aks_external_method' and not pending:
                                if caller_identity.get('process') not in ('keybagd', 'installd') or x[1] not in (5,14,17):
                                    assert rsp(gs,'s').startswith('T');cont();continue
                            if (name == 'aks_external_method' and not pending and
                                    x[1] == 14 and caller_identity.get('process') == 'keybagd' and not keybagd_installed):
                                # Resolve this executable from a native saved
                                # userspace frame, not the shared-cache slide.
                                binary_path = ROOT / 'backend/evidence/sep-publication-20261014-164800/keybagd'
                                binary_bytes = binary_path.read_bytes()
                                cursor=32; segments=[]
                                for _ in range(struct.unpack_from('<I',binary_bytes,16)[0]):
                                    cmd,size=struct.unpack_from('<II',binary_bytes,cursor)
                                    if cmd==25: segments.append(struct.unpack_from('<4Q',binary_bytes,cursor+24))
                                    cursor+=size
                                state=memory(gs,int(caller_identity['thread'],16)+0x328,33*8)
                                if len(state)==33*8:
                                    user=struct.unpack('<33Q',state);frame=user[29]
                                    for _ in range(20):
                                        raw=memory(gs,frame,16)
                                        if len(raw)!=16:break
                                        frame,ret=struct.unpack('<QQ',raw)
                                        slide=ret-0x100003478
                                        if slide<0 or slide&0xfff:continue
                                        if memory(gs,0x100000000+slide,16)!=binary_bytes[:16]:continue
                                        hooks={'keybagd_systembag_load_before':0x100003488}
                                        if args.keybagd_loader_hooks:
                                            hooks.update(keybagd_kb_load=0x100006a44,keybagd_read_plist=0x1000022d8,keybagd_open_result_before=0x100002300,keybagd_errno_before=0x100002310)
                                        verified=[]
                                        for hook,static in hooks.items():
                                            expected=next((binary_bytes[off+static-va:off+static-va+16] for va,_,off,filesize in segments if va<=static<va+filesize),b'')
                                            actual=memory(gs,static+slide,16)
                                            assert expected and (not actual or actual==expected),(hook,actual.hex(),expected.hex())
                                            assert rsp(gs,f'Z0,{static+slide:x},4')=='OK'
                                            bps[static+slide]=hook
                                            user_expected[static+slide]=expected
                                            verified.append(dict(name=hook,runtime=hex(static+slide),expected=expected.hex(),actual=actual.hex()))
                                        keybagd_installed=True
                                        save_json(out/'keybagd-hook-mapping.json',dict(slide=hex(slide),hooks=verified,binary=str(binary_path),sha256=digest(binary_path)))
                                        break
                            if name == 'execve_entry' and not pending:
                                fname_ptr = u64(gs, x[1]) if x[1] else 0
                                fname = memory(gs, fname_ptr, 64).split(b'\0')[0].decode(errors='replace') if fname_ptr else ''
                                if 'init_data_protection' not in fname and 'seputil' not in fname:
                                    assert rsp(gs,'s').startswith('T');cont();continue
                                caller_identity['process'] = 'init_data_prote'
                                caller_identity['exec_fname'] = fname
                            if name == 'execve_entry' and pending:
                                caller_identity['process'] = 'init_data_prote'
                            if name == 'sep_open1':
                                pname = memory(gs, x[0] + 0x254, 16).split(b'\0')[0].decode(errors='replace') if x[0] else ''
                                if pname != 'init_data_prote':
                                    assert rsp(gs,'s').startswith('T');cont();continue
                                caller_identity['process'] = pname
                                caller_identity['pid'] = int.from_bytes(memory(gs, x[0] + 0x10, 4), 'little')
                                caller_identity['proc'] = hex(x[0])
                            if name == 'sep_vnode_open':
                                path = memory(gs, x[0], 64).split(b'\0')[0].decode(errors='replace') if x[0] else ''
                                if 'sep-firmware' not in path:
                                    assert rsp(gs,'s').startswith('T');cont();continue
                                caller_identity['process'] = 'init_data_prote'
                                caller_identity['vnode_path'] = path
                            if name == 'keystore_new_client' and caller_identity.get('process') not in ('installd','keybagd','init_data_prote'):
                                assert rsp(gs,'s').startswith('T');cont();continue
                            if user_hooks and not user_installed and caller_identity.get('process') == 'backboardd':
                                # A Mach IPC trap frame returns to mach_msg_trap+8.
                                # Require both the slid cache header and exact target
                                # instruction bytes before installing any user hook.
                                frame=x[29]; candidates=[]
                                for _ in range(16):
                                    data=memory(gs,frame,16)
                                    if len(data)!=16: break
                                    frame,ret=struct.unpack('<QQ',data)
                                    if 0x180000000 <= ret < 0x200000000:
                                        candidates.append(ret-0x18e279ca0)
                                cache=Path(user_hooks['cache_path']).read_bytes()
                                index=json.loads(Path(user_hooks['index_path']).read_text())
                                for slide in candidates:
                                    if slide<0 or slide&0xfff: continue
                                    if memory(gs,0x180000000+slide,16)!=cache[:16]: continue
                                    verified=[]
                                    for uname, static in user_hooks.get('symbols', {}).items():
                                        static=int(static,0); expected=b''
                                        for va,size,off,*_ in index['mappings']:
                                            if va<=static<va+size:
                                                expected=cache[off+static-va:off+static-va+16];break
                                        actual=memory(gs,static+slide,16)
                                        assert expected and (not actual or actual==expected), (uname,actual.hex(),expected.hex())
                                        assert rsp(gs,f'Z0,{static+slide:x},4')=='OK'
                                        bps[static+slide]=uname
                                        user_expected[static+slide]=expected
                                        verified.append(dict(name=uname,static=hex(static),runtime=hex(static+slide),bytes=actual.hex(),expected=expected.hex(),mapped_at_install=bool(actual)))
                                    for item in args.user_va_hook:
                                        uname, va = item.split('=', 1); va = int(va, 0)
                                        assert rsp(gs,f'Z0,{va:x},4')=='OK'
                                        bps[va]=uname
                                        verified.append(dict(name=uname,runtime=hex(va),absolute=True))
                                    mapping_dict = dict(slide=hex(slide),hooks=verified)
                                    if 'exe_hooks' in user_hooks and 'backboardd' in user_hooks['exe_hooks']:
                                        exe_proc = 'backboardd'
                                        hdr_hex = user_hooks.get('exe_headers', {}).get(exe_proc, 'cffaedfe0c0000010000000002000000')
                                        hdr = bytes.fromhex(hdr_hex)
                                        known = user_hooks.get('known_return_sites', {}).get(exe_proc, {})
                                        exe_slide = derive_exe_slide(gs, memory, exe_proc, hdr, known)
                                        if exe_slide is not None:
                                            exe_exp = user_hooks.get('exe_expected', {}).get(exe_proc, {})
                                            exe_path = user_hooks.get('exe_paths', {}).get(exe_proc)
                                            exe_v = install_exe_hooks(gs, memory, rsp, exe_proc, exe_slide, user_hooks['exe_hooks'][exe_proc], exe_exp, bps, user_expected, exe_path=exe_path)
                                            verified.extend(exe_v)
                                            exe_slides[exe_proc] = exe_slide
                                            exe_installed[exe_proc] = True
                                            mapping_dict['exe_slide'] = hex(exe_slide)
                                            mapping_dict['exe_slides'] = {exe_proc: hex(exe_slide)}
                                    save_json(out/'user-hook-mapping.json',mapping_dict)
                                    user_installed=True;break
                            if user_hooks and 'exe_hooks' in user_hooks:
                                cur_proc = caller_identity.get('process')
                                if cur_proc in user_hooks['exe_hooks'] and cur_proc not in exe_installed:
                                    hdr_hex = user_hooks.get('exe_headers', {}).get(cur_proc, 'cffaedfe0c0000010000000002000000')
                                    hdr = bytes.fromhex(hdr_hex)
                                    known = user_hooks.get('known_return_sites', {}).get(cur_proc, {})
                                    lr_val = x[30] if len(x) > 30 else None
                                    exe_slide = derive_exe_slide(gs, memory, cur_proc, hdr, known, lr=lr_val, hook_name=name)
                                    if exe_slide is not None:
                                        exe_exp = user_hooks.get('exe_expected', {}).get(cur_proc, {})
                                        exe_path = user_hooks.get('exe_paths', {}).get(cur_proc)
                                        exe_v = install_exe_hooks(gs, memory, rsp, cur_proc, exe_slide, user_hooks['exe_hooks'][cur_proc], exe_exp, bps, user_expected, exe_path=exe_path)
                                        exe_slides[cur_proc] = exe_slide
                                        exe_installed[cur_proc] = True
                                        mapping_path = out / 'user-hook-mapping.json'
                                        if mapping_path.exists():
                                            m_dict = json.loads(mapping_path.read_text())
                                            m_dict['exe_slide'] = hex(exe_slide)
                                            m_dict.setdefault('exe_slides', {})[cur_proc] = hex(exe_slide)
                                            m_dict.setdefault('hooks', []).extend(exe_v)
                                            save_json(mapping_path, m_dict)
                            if name.startswith('bb_') and caller_identity.get('process') != 'backboardd':
                                assert rsp(gs,'s').startswith('T');cont();continue
                            if name.startswith('sb_') and caller_identity.get('process') != 'SpringBoard':
                                assert rsp(gs,'s').startswith('T');cont();continue
                            if name.startswith('keybagd_') and caller_identity.get('process') != 'keybagd':
                                assert rsp(gs,'s').startswith('T');cont();continue
                            if pc in user_expected:
                                actual=memory(gs,pc,16)
                                try:mapped=instruction_mapped(actual,user_expected[pc])
                                except ValueError:
                                    save_json(out/'user-hook-mismatch.json',dict(name=name,pc=hex(pc),actual=actual.hex(),expected=user_expected[pc].hex(),identity=caller_identity))
                                    raise
                                if not mapped:
                                    # TCG may stop at a virtual breakpoint before
                                    # the guest resolves its instruction-page fault.
                                    # Let the fault execute, retaining the hook;
                                    # this is not evidence of function execution.
                                    assert rsp(gs,'s').startswith('T');cont();continue
                            if name == 'sb_dispatch_release_unaligned' and not (x[0] & 7):
                                # This diagnostic targets the T13 failure (x0 ends
                                # in 0x78c). Keep the hook until a suspicious call;
                                # ordinary aligned releases must not consume its cap.
                                assert rsp(gs,'s').startswith('T');cont();continue
                            if name.startswith(('io_','surface_','accel_','ca_')) and caller_identity.get('process') not in ('backboardd','SpringBoard'):
                                assert rsp(gs,'s').startswith('T');cont();continue
                            if name == 'display_irq_register':
                                provider_props=properties(gs,x[0])
                                provider_name=provider_props.get('name',{}).get('data_hex','')
                                if not any(t in provider_name for t in ['6469737030','6d697069']):
                                    assert rsp(gs,'s').startswith('T');cont();continue
                            if name=='display_irq_init':
                                provider_props=properties(gs,x[4])
                                provider_name=provider_props.get('name',{}).get('data_hex','')
                                if '6469737030' not in provider_name:
                                    assert rsp(gs,'s').startswith('T');cont();continue
                            if name == 'lcd_parent_publish' and pc in bps:
                                owner_props=properties(gs,x[1])
                                if owner_props.get('IOClass',{}).get('text')!='ApplePinotLCD':
                                    assert rsp(gs,'s').startswith('T');cont();continue
                            if name == 'lcd_service_wait':
                                frame=x[29]; lcd_caller=False
                                for _ in range(6):
                                    data=memory(gs,frame,16)
                                    if len(data)!=16: break
                                    frame,ret=struct.unpack('<QQ',data)
                                    if ret in (0xffffff8000f3b7c0,0xffffff8000f4c4cc,0xffffff8000f4c4ec,0xffffff8000f4c50c): lcd_caller=True;break
                                if not lcd_caller:
                                    assert rsp(gs,'s').startswith('T');cont();continue
                            if name in ('lcd_function_lookup','lcd_platform_call'):
                                lookup = memory(gs, u64(gs,x[1]+0x18), 80).split(b'\0')[0].decode(errors='replace')
                                if lookup != 'function-lcd_enable':
                                    assert rsp(gs,'s').startswith('T'); cont(); continue
                            if name == 'lcd_matching_wait':
                                matching = dictionary(gs,x[0])
                                if matching.get('IONameMatch',{}).get('text') != 'function-lcd_enable':
                                    assert rsp(gs,'s').startswith('T'); cont(); continue
                            event = dict(name=name, elapsed=clock.elapsed(), wall_elapsed=clock.wall(),
                                         pc=hex(pc), registers=[hex(v) for v in x], reply=payload)
                            if name in hook_dumps:
                                event['memory_dumps'] = capture_hook_dumps(gs, x, hook_dumps[name])
                            event['identity'] = caller_identity
                            if name == 'sb_dispatch_release_unaligned':
                                event['dispatch_release'] = dispatch_release_evidence(gs, x)
                            if caller_identity.get('exec_fname'):
                                event['exec_fname'] = caller_identity['exec_fname']
                            if caller_identity.get('vnode_path'):
                                event['vnode_path'] = caller_identity['vnode_path']
                            event['instruction_bytes'] = memory(gs, pc, 16).hex()
                            if name.startswith('hidf_'):
                                # IOHIDServiceFilter (x0) -> plugin interfaces at +0x10/+0x18.
                                # For CompassPlugIn, vtable - 0x4108 is the image base;
                                # record its lazy pointers, stubs and stub helper.
                                obs = dict(filter=hex(x[0]), filter_bytes=memory(gs, x[0], 0x30).hex())
                                for slot in (0x10, 0x18):
                                    inst = u64(gs, x[0] + slot) if len(memory(gs, x[0] + slot, 8)) == 8 else 0
                                    if not inst:
                                        continue
                                    head = memory(gs, inst, 0x40)
                                    entry = dict(instance=hex(inst), bytes=head.hex())
                                    if len(head) >= 8:
                                        vt = int.from_bytes(head[:8], 'little')
                                        base = vt - 0x4108
                                        entry['vtable'] = hex(vt)
                                        if base & 0xfff == 0 and memory(gs, base, 4) == bytes.fromhex('cffaedfe'):
                                            entry['compass_base'] = hex(base)
                                            entry['la_symbol_ptr'] = memory(gs, base + 0x4030, 0xc0).hex()
                                            entry['stubs'] = memory(gs, base + 0x3c3c, 0x120).hex()
                                            entry['stub_helper'] = memory(gs, base + 0x3d5c, 0x138).hex()
                                    obs[hex(slot)] = entry
                                event['hidf'] = obs
                            if name == 'keybagd_systembag_load_before':
                                event['contract_observation']=dict(verified_user_mapping=keybagd_installed)
                            if name in ('keybagd_read_plist','keybagd_open_result_before','keybagd_errno_before') and not pending:
                                pointer=x[0] if name=='keybagd_read_plist' else x[21]
                                event['contract_observation']=dict(path=memory(gs,pointer,1024).split(b'\0')[0].decode(errors='replace'),verified_user_mapping=keybagd_installed)
                                if name=='keybagd_open_result_before':event['contract_observation']['fd']=hex(x[0])
                                if name=='keybagd_errno_before':event['contract_observation']['errno']=memory_state(gs,x[0],4)
                            if name == 'aks_system_unlock':
                                provider=int(pending['object'],16) if pending else x[0]
                                event['contract_observation']=dict(provider=hex(provider),ready=memory_state(gs,provider+0x119,1))
                            if name == 'aks_ready_before':
                                provider=x[19];ready=memory_state(gs,provider+0x119,1)
                                event['contract_observation']=dict(provider=hex(provider),ready_flag=int(ready['value'],16) if ready['readable'] else None)
                                if ready['readable'] and ready['value']=='0x1':ready_providers.add(provider)
                            if name == 'aks_wakeup_before':
                                provider=u64(gs,x[21]+0xd8);ready=memory_state(gs,provider+0x119,1)
                                event['contract_observation']=dict(provider=hex(provider),ready_flag=int(ready['value'],16) if ready['readable'] else None)
                                if provider in ready_providers and ready['readable'] and ready['value']=='0x1':
                                    wake_providers.add(provider)
                                    for k_pc in [k for k, v in list(bps.items()) if v.startswith('keybagd_')]:
                                        if k_pc not in temporary: rsp(gs, f'z0,{k_pc:x},4')
                                        bps.pop(k_pc, None)
                                        user_expected.pop(k_pc, None)
                            if (pending and wake_providers and
                                    (name in ('ls_lookup','ls_plugin_store','bks_checkin') or
                                     name=='aks_external_method' and int(pending['argument1'],16)==17 and x[0]&0xffffffff==0)):
                                event.setdefault('contract_observation',{})['progress_after_ready']=True
                            if name.startswith('sep_') and name != 'sep_firmware_helper':
                                owner=int(pending['object'],16) if pending else x[19] if name in ('sep_ep_gate_before','sep_ready_wait_before') else x[0]
                                offsets=(0x88,0x90,0x98,0xc0,0xd0,0xf8) if name in ('sep_start','sep_publish','sep_handle','sep_ready_wait_before') else (0x48,0x60,0x70,0x90,0x98) if name in ('sep_ep_init','sep_ep_gate_before') else (0x140,0x148,0x150,0x158,0x160,0x168,0x178,0x188)
                                if name not in ('sep_ep_init','sep_ep_gate_before'):event['sep_properties']=properties(gs,owner)
                                event['sep_fields']={hex(o):memory_state(gs,owner+o,4 if o in (0xc0,0x60,0x140) else 8) for o in offsets}
                                if name=='sep_start' and not pending:sep_managers.add(owner)
                                if name=='sep_ep_gate_before':
                                    lock=u64(gs,x[21]+0x10);event['gate_workloop']=hex(x[21]);event['gate_owner']=memory_state(gs,lock+0x10)
                                if name=='sep_ready_wait_before':
                                    endpoint=int.from_bytes(memory(gs,0xffffff8000d58a90+7*8,8),'little')
                                    client=u64(gs,endpoint+0x50);workloop=u64(gs,client+0x90);lock=u64(gs,workloop+0x10)
                                    event['readiness_wait']=dict(address=hex(x[20]),state=memory_state(gs,x[20],4),command_gate=hex(x[0]),gate_owner=memory_state(gs,lock+0x10),endpoint7=hex(endpoint),endpoint_head=memory(gs,endpoint,0xa0).hex())
                            if name == 'sep_firmware_helper':
                                event['iomd'] = hex(x[0])
                                event['payload_type'] = hex(x[1])
                                event['caller_lr'] = hex(lr)
                            if name.startswith('seputil_'):
                                static_addrs = {
                                    'seputil_loader': 0x100001c8c,
                                    'seputil_preflight_test': 0x100002724,
                                    'seputil_submission_entry': 0x1000027d4,
                                    'seputil_call_struct': 0x1000027ec,
                                    'seputil_open_fail': 0x100002934,
                                }
                                s_slide = pc - static_addrs[name] if name in static_addrs else 0
                                bss_addr = 0x100008990 + s_slide
                                event['seputil_pc'] = hex(pc)
                                event['seputil_slide'] = hex(s_slide)
                                event['seputil_bss'] = memory_state(gs, bss_addr, 8)
                                event['seputil_flag_byte0'] = memory_state(gs, bss_addr, 1)
                                event['seputil_flag_byte1'] = memory_state(gs, bss_addr + 1, 1)
                                event['seputil_flag_byte2'] = memory_state(gs, bss_addr + 2, 1)
                            if name=='keystore_new_client' and pc in bps:
                                event['service_properties']=properties(gs,x[0])
                                if event['service_properties'].get('IOClass',{}).get('text')=='AppleKeyStore':
                                    workloop=u64(gs,x[0]+0x90);lock=u64(gs,workloop+0x10)
                                    event['keystore_gate']=dict(provider=hex(x[0]),workloop=hex(workloop),owner=memory_state(gs,lock+0x10))
                            if name == 'sep_open1':
                                user_path_ptr = u64(gs, x[1])
                                event['open_path'] = memory(gs, user_path_ptr, 64).split(b'\0')[0].decode(errors='replace') if user_path_ptr else ''
                            if caller_identity.get('process') == 'init_data_prote':
                                thread = int(caller_identity.get('thread','0'),16)
                                saved_state = memory(gs, thread + 0x328, 33 * 8)
                                event['saved_user_state'] = saved_state.hex()
                                if len(saved_state) == 33 * 8:
                                    uregs = struct.unpack_from('<33Q', saved_state)
                                    user_fp = uregs[29]
                                    user_lr = uregs[30]
                                    event['user_pc'] = hex(uregs[32])
                                    event['user_lr'] = hex(user_lr)
                                    event['user_sp'] = hex(uregs[31])
                                    frame = user_fp
                                    seputil_slide = None
                                    if (uregs[32] & 0xfff) == 0x448 and 0x100000000 <= uregs[32] < 0x200000000:
                                        seputil_slide = (uregs[32] & ~0x3fff) - 0x100000000
                                    elif (user_lr & 0xfff) in (0x8bc, 0x694, 0xb70, 0x5a8):
                                        seputil_slide = (user_lr & ~0xfff) - 0x100002000
                                    elif (user_lr & 0xfff) in (0xca0, 0xc8c):
                                        seputil_slide = (user_lr & ~0xfff) - 0x100001000
                                    user_frames = []
                                    for _ in range(16):
                                        fdata = memory(gs, frame, 16)
                                        if len(fdata) != 16: break
                                        prev_fp, ret = struct.unpack('<QQ', fdata)
                                        user_frames.append(dict(fp=hex(frame), lr=hex(ret)))
                                        if (ret & 0xfff) in (0x8bc, 0x694, 0xb70, 0x5a8):
                                            seputil_slide = (ret & ~0xfff) - 0x100002000
                                            break
                                        elif (ret & 0xfff) in (0xca0, 0xc8c):
                                            seputil_slide = (ret & ~0xfff) - 0x100001000
                                            break
                                        elif 0x100000000 <= ret < 0x200000000:
                                            cand = (ret & ~0x3fff) - 0x100000000
                                            if memory(gs, 0x100000000 + cand, 4) == b'\xcf\xfa\xed\xfe':
                                                seputil_slide = cand
                                                break
                                        frame = prev_fp
                                    event['user_frames'] = user_frames
                                    if seputil_slide is not None:
                                        magic = memory(gs, 0x100000000 + seputil_slide, 4)
                                        bss_addr = 0x100008990 + seputil_slide
                                        event['seputil_slide'] = dict(
                                            slide=hex(seputil_slide),
                                            magic=magic.hex(),
                                            magic_valid=magic==b'\xcf\xfa\xed\xfe',
                                            bss_address=hex(bss_addr),
                                            bss_state=memory_state(gs, bss_addr, 8),
                                            flag_byte0=memory_state(gs, bss_addr, 1),
                                            flag_byte1=memory_state(gs, bss_addr + 1, 1),
                                            flag_byte2=memory_state(gs, bss_addr + 2, 1)
                                        )
                                        for hname, static_addr in (
                                            ('seputil_loader', 0x100001c8c),
                                            ('seputil_preflight_test', 0x100002724),
                                            ('seputil_submission_entry', 0x1000027d4),
                                            ('seputil_call_struct', 0x1000027ec),
                                            ('seputil_open_fail', 0x100002934),
                                        ):
                                            rt_addr = static_addr + seputil_slide
                                            if rt_addr not in bps and rsp(gs, f'Z0,{rt_addr:x},4') == 'OK':
                                                bps[rt_addr] = hname
                            if name=='keystore_wait' and not pending:event['matching']=matching
                            if name in ('console_get','console_set'):
                                video=x[1] if not pending else int(pending['argument1'],16)
                                event['console_video']=memory(gs,video,96).hex()
                                event['platform_video']=memory(gs,0xffffff8000681110,0x90).hex()
                            if name in ('ca_alignment_fault','console_set'):
                                thread=int(caller_identity.get('thread','0'),16)
                                event['saved_user_state']=memory(gs,thread+0x328,33*8).hex()
                                event['exception_registers']={n:rsp(gs,f'p{sysregs[n]:x}') for n in ('ESR_EL1','FAR_EL1','ELR_EL1') if n in sysregs}
                                saved_pc=u64(gs,thread+0x428)
                                event['saved_user_pc_bytes']=memory(gs,max(0,saved_pc-16),64).hex()
                            if name == 'io_matching' and pc in bps:
                                event['class_name']=memory(gs,x[0],128).split(b'\0')[0].decode(errors='replace')
                            if name.startswith(('surface_','accel_','ca_','io_')):
                                event['user_arguments']={str(n):memory(gs,x[n],128).hex() for n in range(4) if 0x100000000<=x[n]<0x200000000}
                                if name.startswith('ca_') and pc in bps:
                                    event['ca_object']=memory(gs,x[0],512).hex()
                                    if name=='ca_add_context':event['ca_context']=memory(gs,x[1],512).hex()
                                if name.startswith('surface_') and pc in temporary and 0x100000000<=x[0]<0x200000000:
                                    event['surface_head']=memory(gs,x[0],256).hex()
                                if name.startswith('accel_') and pc in bps:
                                    dev = x[0] if x[0] >> 56 == 0xff else 0
                                    dev_status = memory(gs, dev + 0x490, 1) if dev else b''
                                    if not dev_status and dev:
                                        alt_dev = u64(gs, dev + 0x118)
                                        if alt_dev >> 56 == 0xff:
                                            dev = alt_dev
                                            dev_status = memory(gs, dev + 0x490, 1)
                                    if dev_status:
                                        event['device_status_before'] = dev_status.hex()
                                        if 'device_status' in poke and not (dev_status[0] & 2):
                                            new_byte = dev_status[0] | 2
                                            rsp(gs, f'M{dev + 0x490:x},1:{new_byte:02x}')
                                            event['device_status_after'] = hex(new_byte)
                                if name.startswith('accel_wait_stamp') and pc in bps:
                                    channel_obj = x[0]
                                    ring_idx = x[1] & 0x1f
                                    target_stamp = x[2] & 0xffffffff
                                    stamps_table = u64(gs, channel_obj + 0x28) if channel_obj >> 56 == 0xff else 0
                                    stamp_ptr = u64(gs, stamps_table + ring_idx * 8) if stamps_table >> 56 == 0xff else 0
                                    if stamp_ptr >> 56 == 0xff:
                                        cur_stamp = int.from_bytes(memory(gs, stamp_ptr, 4), 'little')
                                        event['stamp_before'] = cur_stamp
                                        event['target_stamp'] = target_stamp
                                        event['stamp_ptr'] = hex(stamp_ptr)
                                        if 'stamps' in poke and cur_stamp < target_stamp:
                                            rsp(gs, f'M{stamp_ptr:x},4:{target_stamp.to_bytes(4, "little").hex()}')
                                            event['stamp_after'] = target_stamp
                                        if 'stamps' in poke and x[3] >> 56 == 0xff:
                                            rsp(gs, f'M{x[3]:x},4:{target_stamp.to_bytes(4, "little").hex()}')
                                if name.startswith('accel_stamp_') and pc in bps:
                                    stamp_ptr = x[23] if x[23] >> 56 == 0xff else x[0]
                                    if stamp_ptr >> 56 == 0xff:
                                        cur_stamp = int.from_bytes(memory(gs, stamp_ptr, 4), 'little')
                                        target_stamp = x[21] & 0xffffffff
                                        event['stamp_before'] = cur_stamp
                                        event['target_stamp'] = target_stamp
                                        event['stamp_ptr'] = hex(stamp_ptr)
                                        if 'stamps' in poke and cur_stamp < target_stamp:
                                            rsp(gs, f'M{stamp_ptr:x},4:{target_stamp.to_bytes(4, "little").hex()}')
                                            event['stamp_after'] = target_stamp
                                        if 'stamps' in poke and x[22] >> 56 == 0xff:
                                            rsp(gs, f'M{x[22]:x},4:{target_stamp.to_bytes(4, "little").hex()}')
                                if name == 'accel_drain_sleep' and pc in bps and 'drain' in poke:
                                    event['drain_iteration'] = x[23]
                                    event['drain_event_obj'] = hex(x[21])
                                    regs_mod = list(x)
                                    regs_mod[24] = 1
                                    rsp(gs, 'G' + struct.pack('<33Q', *regs_mod).hex())
                                    event['drain_x24_set'] = 1
                            if name.startswith('meta_') and pc in bps:
                                meta=x[19] if x[19]>>56==0xff else (x[0] if x[0]>>56==0xff else 0)
                                if meta:
                                    event['meta_object']=hex(meta)
                                    event['meta_state_538']=memory(gs,meta+0x530,16).hex()
                                    owner=u64(gs,meta+0x58)
                                    if owner>>56==0xff:
                                        doorbell=u64(gs,owner+0x518)
                                        event['meta_owner']=hex(owner);event['meta_doorbell_va']=hex(doorbell)
                                        if doorbell>>56==0xff:
                                            event['meta_doorbell_pa']=command(fp,'human-monitor-command',{'command-line':f'gva2gpa {doorbell:#x}'}).strip()
                                if name=='meta_wait' and args.meta_dump and meta and not (out/'meta-dump.json').exists():
                                    def gpa(va):
                                        raw=command(fp,'human-monitor-command',{'command-line':f'gva2gpa {va:#x}'})
                                        found=re.findall(r'gpa: 0x([0-9a-fA-F]+)',raw);return hex(int(found[0],16)) if found else None
                                    owner=u64(gs,meta+0x58);dump=dict(meta=hex(meta),owner=hex(owner),meta_bytes=memory(gs,meta,0x600).hex(),pointers={})
                                    for base,size in ((meta,0x600),(owner,0x3f00)):
                                        for o in range(0,size,8):
                                            v=u64(gs,base+o)
                                            if v>>40==0xffffff and v&7==0:dump['pointers'][f'{base:#x}+{o:#x}']=dict(va=hex(v),pa=gpa(v))
                                    dump['owner_bytes']=memory(gs,owner,0x3f00).hex()
                                    dump['sgx1_sequencer']=command(fp,'human-monitor-command',{'command-line':'xp /20xw 0x201e10000'})
                                    command(fp,'pmemsave',{'val':plan['dram_base'],'size':plan['memory_size'],'filename':str(out/'ram.bin')})
                                    save_json(out/'meta-dump.json',dump)
                                if name=='meta_msg':
                                    event['meta_message']=memory(gs,x[1],64).hex() if x[1]>>56==0xff else None
                                if name=='meta_wait' and 'meta_alive' in poke and meta and memory(gs,meta+0x539,1)==b'\0':
                                    assert rsp(gs,f'M{meta+0x538:x},2:0101')=='OK'
                                    event['diagnostic_poke']='meta_alive'
                            if name=='swap_submit' and pc in bps:
                                event['swap_payload']=memory(gs,x[1],min(x[2],0x138)).hex()
                            if name in ('swap_begin','program_hardware','irq_filter'):
                                event['pipe_fields']={hex(o):memory(gs,x[0]+o,8).hex() for o in [0x330,0x338,0x408,0x40c,0x5a0,0x5b8,0x5d8,0x3780]}
                            if name=='display_irq_register':
                                event['interrupt_provider_properties']=provider_props
                            if name=='display_irq_init':
                                event['interrupt_provider_properties']=provider_props
                            if name == 'lcd_parent_publish' and pc in bps:
                                event['parent_properties']=properties(gs,x[0])
                                event['owner_properties']=owner_props
                            if name in ('lcd_function_lookup','lcd_platform_call'):
                                event['lookup_name'] = lookup
                                event['lookup_provider_properties'] = properties(gs,x[0])
                            if name == 'lcd_matching_wait':
                                event['matching'] = matching
                                event['matching_nested'] = {k:dictionary(gs,int(v['object'],16)) for k,v in matching.items() if isinstance(v,dict) and k.endswith('Match') and k!='IONameMatch'}
                            if name in ('service_wait','lcd_service_wait'):
                                event['matching'] = dictionary(gs,x[0])
                                event['matching_nested'] = {k:dictionary(gs,int(v['object'],16)) for k,v in event['matching'].items() if isinstance(v,dict) and k.endswith('Match')}
                            if name.startswith('physical_') or name == 'startWithIBoot':
                                driver = x[0] if name == 'startWithIBoot' and pc in bps else x[19]
                                event['driver'] = hex(driver)
                                event['driver_properties'] = properties(gs, driver)
                                event['display_fields'] = {hex(o):memory(gs,driver+o,8).hex() for o in [0x3d0,0x5180,0x5db8,0x5dd0,0x5dd8]}
                            if pending:
                                event['return_of'] = pending.copy()
                                if temporary.remove(pc,pending) and pc not in bps:assert rsp(gs, f'z0,{pc:x},4') == 'OK'
                            elif args.returns and not name.endswith('_before'):
                                item=dict(name=name, object=hex(x[0]), argument1=hex(x[1]), entry_pc=hex(pc),thread=caller_identity.get('thread'),stack=hex(x[31]))
                                if temporary.add(lr,item) and lr not in bps:assert rsp(gs, f'Z0,{lr:x},4') == 'OK'
                            event['hmp'] = command(fp, 'human-monitor-command', {'command-line':'info registers'})
                            # Retain a small object view, without claiming an unknown layout.
                            if x[0] >> 56 == 0xff:
                                event['object_head'] = rsp(gs, f'm{x[0]:x},340')
                                provider = x[1] if name == 'userclient_start' and 'return_of' not in event else u64(gs, x[0] + 0xd8)
                                if provider >> 56 == 0xff:
                                    event['provider'] = hex(provider)
                                    event['provider_head'] = rsp(gs, f'm{provider:x},100')
                                    event['provider_properties'] = properties(gs, provider)
                                    vt = u64(gs, provider)
                                    event['provider_methods'] = {hex(o):hex(u64(gs, vt+o)) for o in [0x538,0x588,0x590,0x5c0]}
                            if 'return_of' in event:
                                entry_obj = int(event['return_of']['object'],16)
                                if entry_obj >> 56 == 0xff:
                                    event['returned_object'] = rsp(gs, f'm{entry_obj:x},340')
                                if name == 'lcd_parent_publish':
                                    event['returned_owner_properties']=properties(gs,int(event['return_of']['argument1'],16))
                            # Small ARM64 frame chain, to identify the exact waiting caller.
                            frames = []; frame = x[29]; seen = set()
                            for _ in range(16):
                                if not frame or frame in seen: break
                                seen.add(frame); data = memory(gs,frame,16)
                                if len(data)!=16: break
                                previous, ret = struct.unpack('<QQ',data)
                                frames.append(dict(fp=hex(frame),lr=hex(ret))); frame=previous
                            event['frames'] = frames
                            scanout.observe(event)
                            events.append(event); save_json(out / 'breakpoints.json', events)
                            if name=='keybagd_errno_before' and args.keybagd_loader_hooks:
                                save_json(out/'loader-stop.json',dict(reason='Native open failed; errno pointer captured',event_index=len(events)-1))
                                break
                            if pc in bps:
                                hook_counts[pc]=hook_counts.get(pc,0)+1
                                cap=hit_cap(name,pc)
                                if cap and hook_counts[pc]>=cap:
                                    if pc not in temporary:assert rsp(gs,f'z0,{pc:x},4')=='OK'
                                    del bps[pc]
                            steps = []
                            for _ in range(args.step_count):
                                assert rsp(gs, 's').startswith('T')
                                if args.step_count > 1:
                                    sr = struct.unpack_from('<33Q', bytes.fromhex(rsp(gs,'g')))
                                    steps.append(dict(pc=hex(sr[32]),x0=hex(sr[0]),x1=hex(sr[1]),x8=hex(sr[8]),
                                                      x16=hex(sr[16]),x19=hex(sr[19]),x20=hex(sr[20]),x21=hex(sr[21]),
                                                      lr=hex(sr[30]),bytes=memory(gs,sr[32],4).hex()))
                            if steps:
                                event['steps'] = steps
                                save_json(out / 'breakpoints.json', events)
                            cont()
                        else:
                            if time.monotonic() >= next_sample:
                                command(fp, 'stop');clock.pause()
                                if capture(fp, out, clock.elapsed()) and pixel_stop():break
                                command(fp, 'cont');clock.resume(); next_sample = time.monotonic() + 1
                            time.sleep(.02)
                    freeze()
                    capture(fp, out, clock.elapsed())
                    if user_hooks and gs:
                        from ca_probe_state import snapshot
                        snapshot(gs,memory,u64,out,out/'user-hook-mapping.json',Path(user_hooks['index_path']))
                    if sep_managers and gs:
                        state=[]
                        for manager in sep_managers:
                            workloop=u64(gs,manager+0x90);lock=u64(gs,workloop+0x10);owner=u64(gs,lock+0x10)
                            state.append(dict(manager=hex(manager),fields={hex(o):memory_state(gs,manager+o) for o in (0x88,0x90,0x98,0xc0,0xd0)},workloop=hex(workloop),lock=hex(lock),gate_owner=hex(owner),gate_owner_head=memory(gs,owner,0x328).hex(),ep7=memory_state(gs,0xffffff8000d58a90+7*8)))
                        save_json(out/'sep-runtime.json',state)
                    save_json(out/'observations.json',dict(kernel_panic=panicked,guest_running_seconds=clock.elapsed(),wall_seconds=clock.wall(),stop_condition=args.stop_condition))
                    (out / 'registers.txt').write_text(command(fp, 'human-monitor-command', {'command-line':'info registers'}))
                    views = {}
                    for addr, count in [(0x206200000,4),(0x206300000,4),(0x206201020,24),(0x206204040,16),(0x206400000,32),(0x201000000,16),(0x201e00000,16)]:
                        views[hex(addr)] = command(fp, 'human-monitor-command', {'command-line': f'xp /{count}xw {addr:#x}'})
                    save_json(out / 'mmio_registers.json', views)
                    command(fp, 'screendump', {'filename': str(out / 'qmp-framebuffer.ppm')})
                    command(fp, 'quit')
            except Exception as exc:
                save_json(out / 'failure.json', dict(error=repr(exc)))
                raise
            finally:
                if gs: gs.close()
                if qs: qs.close()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait(timeout=5)
                save_json(out / 'process-exit.json', dict(exit_code=proc.returncode, reaped=True))
    print(out, flush=True)


if __name__ == '__main__':
    main()
