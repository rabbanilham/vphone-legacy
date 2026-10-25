#!/usr/bin/env python3
"""Synthetic production Swift RFB client -> QEMU VNC -> A7 touch regression."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from smoke_test import BINARY, command, fixture
ROOT=Path(__file__).resolve().parents[1]


def main():
    source=(ROOT/'sources/vphone-player/main.swift').read_text().split('guard CommandLine.arguments.count == 3')[0]
    harness='''
let client = RFB()
let qmp = QMPInput(CommandLine.arguments[2])
let gestures = TouchGestures()
var inputError = false
qmp.failed = { message in print(message);inputError = true }
gestures.send = {type,points in qmp.touch(type,points)}
var sent = false
client.frame = { _ in
 if !sent {
  sent = true
  client.pointer(123,456,true)
  DispatchQueue.global().asyncAfter(deadline:.now()+0.06) {client.pointer(321,654,true)}
  DispatchQueue.global().asyncAfter(deadline:.now()+0.12) {client.pointer(321,654,false)}
  DispatchQueue.global().asyncAfter(deadline:.now()+0.8) {
    gestures.pinch(NSPoint(x:160,y:568),begin:true)
    DispatchQueue.global().asyncAfter(deadline:.now()+0.08) {gestures.pinch(NSPoint(x:120,y:568),begin:false)}
    DispatchQueue.global().asyncAfter(deadline:.now()+0.16) {gestures.end()}
  }
 }
}
client.failed = {_ in}
client.run(CommandLine.arguments[1])
assert(sent && !inputError)
'''
    with tempfile.TemporaryDirectory(prefix='vp-vnc-',dir='/tmp') as directory:
        root=Path(directory);(root/'main.swift').write_text(source+harness)
        subprocess.run(['swiftc','-module-cache-path',str(ROOT/'build/player/module-cache'),str(root/'main.swift'),'-o',str(root/'client')],check=True)
        (root/'fixture.bin').write_bytes(fixture())
        proc=subprocess.Popen([str(BINARY),'-L',str(ROOT/'backend/upstream/pc-bios'),'-machine','s5l8960x,boot-entry=0x800000000,boot-args=0x800001000',
            '-kernel',str(root/'fixture.bin'),'-accel','tcg','-display','none','-vnc',f'unix:{root}/vnc',
            '-qmp',f'unix:{root}/qmp,server=on,wait=off','-d','unimp','-D',str(root/'trace')],
            env={**os.environ,'VPHONE_TOUCH_FORCE':'1'},stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        client=None
        try:
            deadline=time.monotonic()+10
            while not (root/'qmp').exists() or not (root/'vnc').exists():
                assert proc.poll() is None,proc.stderr.read().decode()
                assert time.monotonic()<deadline;time.sleep(.01)
            client=subprocess.Popen([str(root/'client'),str(root/'vnc'),str(root/'qmp')])
            while True:
                trace=(root/'trace').read_text()
                if 'A7 MTOUCH n=2 state=7 x=119 y=583' in trace:break
                assert time.monotonic()<deadline,trace[-3000:]
                time.sleep(.01)
            assert 'x=122 y=471' in trace and 'A7 MOUSE state=7 x=320 y=669' in trace,trace
            assert 'A7 MTOUCH n=2 state=1 x=159 y=583' in trace,trace
            print('\n'.join(line for line in trace.splitlines() if any(f'A7 MOUSE state={state} ' in line for state in (1,3,7))))
            proc.terminate()
            assert client.wait(timeout=5)==0
            assert proc.wait(timeout=5) == 0
        finally:
            if client and client.poll() is None:client.kill();client.wait()
            if proc.poll() is None:proc.kill();proc.wait()
            proc.stderr.close()
    print('PASS: production Swift VNC pointer and QMP two-finger gestures generate A7 start/move/release contacts')
if __name__=='__main__':main()
