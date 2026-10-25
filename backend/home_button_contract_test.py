#!/usr/bin/env python3
"""Actual QMP keyboard -> GPIO Home press/release contract, without iOS."""
import json
from pathlib import Path
import re
import socket
import struct
import subprocess
import tempfile
import time
from smoke_test import BINARY, command
from gpio_contract_test import move64


def main():
    with tempfile.TemporaryDirectory(prefix='vp-home-', dir='/tmp') as directory:
        root = Path(directory)
        # Enable both-edge GPIO2, then spin. IRQ group 0 stays latched.
        code = move64(1, 0x20e300008) + move64(0, 0x20c) + [0xb9000020,0x14000000]
        image=root/'code.bin'; image.write_bytes(struct.pack('<'+'I'*len(code),*code))
        proc=subprocess.Popen([str(BINARY),'-machine','s5l8960x,boot-entry=0x800000000,boot-args=0x800001000',
            '-kernel',str(image),'-accel','tcg','-display','none',
            '-qmp',f'unix:{root}/qmp,server=on,wait=off'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        try:
            deadline=time.monotonic()+10
            while not (root/'qmp').exists():
                assert proc.poll() is None,proc.stderr.read().decode()
                assert time.monotonic()<deadline
                time.sleep(.01)
            with socket.socket(socket.AF_UNIX) as sock:
                sock.settimeout(5);sock.connect(str(root/'qmp'))
                with sock.makefile('rwb') as fp:
                    json.loads(fp.readline());command(fp,'qmp_capabilities')
                    def read(address):
                        output=command(fp,'human-monitor-command',{'command-line':f'xp /1wx {address:#x}'})
                        return int(re.search(r': 0x([0-9a-f]+)',output)[1],16)
                    while read(0x20e300008)!=0x20d:
                        assert time.monotonic()<deadline;time.sleep(.01)
                    for _ in range(3):
                        command(fp,'input-send-event',{'device':'a7-touch-display','events':[{'type':'key','data':{'down':True,'key':{'type':'qcode','data':'home'}}}]})
                        # The edge proves dispatch even if host scheduling misses the short low sample.
                        assert read(0x20e300800)&4
                        time.sleep(.15)
                        assert read(0x20e300008)&1, 'Home remained pressed after safety timer'
                        command(fp,'input-send-event',{'device':'a7-touch-display','events':[{'type':'key','data':{'down':False,'key':{'type':'qcode','data':'home'}}}]})
                    command(fp,'quit')
            assert proc.wait(timeout=5)==0
        finally:
            if proc.poll() is None:proc.kill();proc.wait()
            proc.stderr.close()
    print('PASS: actual QMP Home keyboard dispatch, GPIO edge latch and automatic release')

if __name__=='__main__':main()
