#!/usr/bin/env python3
"""Guest checks for observational display tracing; not native iOS UI evidence."""
from pathlib import Path
import re
import socket
import struct
import subprocess
import tempfile
import time

from gpio_contract_test import move64
from smoke_test import BINARY, command

BASE = 0x800000000
OUT = BASE + 0x20000


def fixture():
    code = move64(4, OUT)
    expected = []; accesses = []

    def read(addr, value):
        code.extend(move64(2, addr))
        pc = BASE + 4 * len(code)
        code.extend([0xb9400043, 0xf9000003 | len(expected) << 10 | 4 << 5])
        expected.append(value); accesses.append(('read', addr, value, pc))

    def write(addr, value):
        code.extend(move64(2, addr) + move64(0, value))
        pc = BASE + 4 * len(code)
        code.append(0xb9000040); accesses.append(('write', addr, value, pc))

    read(0x206200004, 0)
    write(0x206200004, 1)
    read(0x206200004, 1)
    read(0x206300004, 0)
    write(0x206300004, 0x0000c000)
    read(0x206300004, 0x0000c000)
    write(0x206300004, 0x8000c000)
    read(0x206300004, 0x8000c001)
    write(0x206300004, 0x0000c001)
    read(0x206300004, 0x0000c000)
    read(0x206201038, 0x100)
    read(0x20620104c, 1)
    write(0x206204048, 99)
    read(0x206204048, 2560)  # Trace must record the special returned value.
    read(0x206400004, 0)
    write(0x206400004, 0x1234)
    read(0x206400004, 0x1234)
    # A committed flush must complete without synthesizing native error bit 19.
    write(0x206201028, 0x100)
    write(0x206201050, 1)
    code.extend(move64(12, 960000) + [0xd53be02a, 0x8b0c014c,
                0xd53be02a, 0xeb0c015f, 0x54ffffc3])
    read(0x20620102c, 0x100)
    read(0x206201048, 1)
    write(0x20620102c, 0x100)
    read(0x20620102c, 0)
    write(0x206201028, 1)
    code.extend(move64(12, 960000) + [0xd53be02a, 0x8b0c014c,
                0xd53be02a, 0xeb0c015f, 0x54ffffc3])
    read(0x20620102c, 1)  # VBI only: no synthesized underrun bit 1.
    write(0x206201028, 0)
    write(0x20620102c, 1)
    read(0x20620102c, 0)
    write(0x2062010c0, 0xa0020009)
    write(0x2062010c0, 0x14044)
    write(0x2062010c0, 0x018e0000)
    code.extend(move64(12, 960000) + [0xd53be02a, 0x8b0c014c,
                0xd53be02a, 0xeb0c015f, 0x54ffffc3])
    read(0x206204044, 0x018e0000)
    read(0x206201048, 9)
    write(0x2062010c0, 0xa002000a)
    write(0x2062010c0, 0x24044)  # Unsupported opcode cannot apply or acknowledge.
    write(0x2062010c0, 0x12345678)
    read(0x206204044, 0x018e0000)
    read(0x206201048, 9)
    code.extend(move64(0, 0xface) + [0xf9000000 | len(expected) << 10 | 4 << 5, 0x14000000])
    expected.append(0xface)
    return struct.pack('<' + 'I' * len(code), *code), expected, accesses


def run(enabled):
    with tempfile.TemporaryDirectory(prefix='display-contract-', dir='/tmp') as tmp:
        tmp = Path(tmp); code, expected, accesses = fixture()
        (tmp / 'fixture.bin').write_bytes(code)
        cmd = [str(BINARY), '-machine', f's5l8960x,boot-entry={BASE:#x},boot-args={BASE+0x1000:#x},display-trace={int(enabled)}',
               '-kernel', str(tmp / 'fixture.bin'), '-accel', 'tcg,thread=single',
               '-display', 'none', '-serial', 'none', '-S',
               '-qmp', f'unix:{tmp}/qmp,server=on,wait=off',
               '-d', 'unimp', '-D', str(tmp / 'trace.log')]
        with (tmp / 'stderr').open('wb') as err:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=err)
            try:
                deadline = time.monotonic() + 5
                while not (tmp / 'qmp').exists():
                    assert proc.poll() is None, (tmp / 'stderr').read_text()
                    assert time.monotonic() < deadline
                    time.sleep(.01)
                sock = socket.socket(socket.AF_UNIX); sock.settimeout(5); sock.connect(str(tmp / 'qmp'))
                with sock, sock.makefile('rwb') as fp:
                    fp.readline(); command(fp, 'qmp_capabilities'); command(fp, 'cont')
                    raw = tmp / 'result.bin'
                    while True:
                        command(fp,'pmemsave',dict(val=OUT,size=len(expected)*8,filename=str(raw)))
                        actual=list(struct.unpack('<'+'Q'*len(expected),raw.read_bytes()))
                        if actual[-1]==0xface: break
                        assert time.monotonic()<deadline, actual
                        time.sleep(.01)
                    command(fp, 'stop')
                    assert actual == expected, (actual,expected)
                    command(fp,'human-monitor-command',{'command-line':'xp /1xw 0x206400004'})
                    command(fp,'quit')
                assert proc.wait(timeout=5)==0
            finally:
                if proc.poll() is None: proc.kill();proc.wait(timeout=5)
        lines = [l for l in (tmp/'trace.log').read_text().splitlines() if l.startswith('DISPLAY_MMIO')]
        if not enabled:
            assert not lines, 'Tracing must be opt-in'
            return
        guest=[];host=[]
        for line in lines:
            d=dict(re.findall(r'(\w+)=([^ ]+)',line))
            assert int(d['ns'])>=0 and int(d['size'])==4
            if d['origin']=='guest':guest.append((d['op'],int(d['pa'],16),int(d['value'],16),int(d['pc'],16)))
            else:host.append(d)
        assert guest==accesses, (guest,accesses)
        assert len(host)==1 and host[0]['pc']=='0x0' and host[0]['value']=='0x1234'


if __name__=='__main__':
    run(True);run(False)
    print('PASS: display timing status; VBI bit 0; no false error bits; swap token/W1C; exact trace count/values/PCs and host distinction')
    print('Synthetic device contract only; no native framebuffer success inferred.')
