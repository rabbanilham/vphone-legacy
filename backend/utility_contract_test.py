#!/usr/bin/env python3
"""Guest-executed logical utility model checks; no physical NAND/identity claim."""
import hashlib
import json
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from gpio_contract_test import move64
from smoke_test import BINARY, command
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/qemu'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from utility import utility_header, REGIONS, IMAGE_SIZE

BASE = 0x800000000
CMD = BASE + 0x100000
PAGE = BASE + 0x200000
OUTPUT = BASE + 0x400000
ANS = 0x208040000
EP = 4 << 56
EPOCH = 777


def fixture(reopen=False, broken=False):
    code = move64(4, OUTPUT)
    expected = []
    def write(address, value, wide=False):
        code.extend(move64(2, address) + move64(0, value) +
                    [0xf9000040 if wide else 0xb9000040])
    def read(address, value, wide=True):
        code.extend(move64(2, address) + [0xf9400043 if wide else 0xb9400043,
                    0xf9000003 | len(expected) << 10 | 4 << 5])
        expected.append(value)
    def execute(opcode, status=0, offset=0, count=1, tag=0, ppn=PAGE >> 12, sub=None):
        address = CMD + (tag * 0x180 if tag < 20 else
                        0x1e00 + (tag-20)*0x1440 if tag < 31 else 0xfcc0)
        write(address, opcode | (0x80000 if count > 256 else 0))
        write(address + 4, offset); write(address + 8, count)
        # Fill a real PPN array using guest instructions, not host injection.
        code.extend(move64(2, address + 0x30) + move64(0, ppn) + move64(10, count) +
                    [0xb8004440, 0x7100054a, 0x54ffffc1])
        if sub is not None: write(address + 0x38, sub)
        write(ANS + 0x1010, EP | tag << 4 | 3, True)
        read(ANS + 0x1038, EP | status << 12 | tag << 4 | 2)
    write(ANS + 0x1010, EP | CMD << 16 | 0x4750, True)
    execute(0)
    read(CMD + 0x40, 0 if broken else 1, False)
    execute(0x80, status=5 if broken else 0, sub=5)
    if not broken:
        read(CMD + 0x44, EPOCH, False)
        read(CMD + 0x48, (EPOCH, EPOCH+5), False)
    if broken:
        execute(0x35, status=5); execute(0x45, status=5)
    else:
        for index, (read_op, write_op, pages) in enumerate(REGIONS):
            marker = (index+1) * 0x1111111111111111
            execute(read_op, offset=pages-1)
            read(PAGE, marker if reopen else 0xffffffffffffffff)
            if not reopen:
                write(PAGE, marker, True)
                execute(write_op, offset=pages-1)
                execute(read_op, offset=pages-1)
                read(PAGE, marker)
                execute(read_op, status=1, offset=pages)
                execute(write_op, status=1, offset=pages)
                execute(read_op, status=1, ppn=0x20a0a0000 >> 12)
        if not reopen:
            # Largest native firmware request and native fixed SysCfg read fit
            # their real descriptor pools and do not escape logical regions.
            execute(0x31, count=2048, tag=31)
            read(PAGE, 0x2222222222222222)
            execute(0x37, count=32, tag=20)
            read(PAGE, 0x6666666666666666)
            execute(0x35, status=1, count=2)
            execute(0x13)
            for unsupported in [0x15, 0x16, 0x32, 0x42, 0x79]: execute(unsupported, status=5)
    write(OUTPUT + len(expected)*8, 0xface, True)
    expected.append(0xface); code.append(0x14000000)
    return struct.pack('<' + 'I'*len(code), *code), expected


def invoke(root, utility, reopen=False, broken=None):
    image = root / 'guest.bin'
    payload, expected = fixture(reopen, bool(broken)); image.write_bytes(payload)
    sockpath = root / 'qmp.sock'; sockpath.unlink(missing_ok=True)
    machine = (f's5l8960x,boot-entry={BASE:#x},boot-args={BASE+0x1000:#x},'
               f'system-image={root}/system.raw,data-image={root}/data.raw,utility-image={utility}')
    common = [str(BINARY), '-machine', machine, '-kernel', str(image), '-accel', 'tcg', '-display', 'none']
    with (root / 'stderr.log').open('wb') as stderr:
        proc = subprocess.Popen(common + ['-S', '-qmp', f'unix:{sockpath},server=on,wait=off'],
                                stdout=subprocess.DEVNULL, stderr=stderr)
        try:
            deadline = time.monotonic()+5
            while not sockpath.exists():
                assert proc.poll() is None, (root / 'stderr.log').read_text()
                assert time.monotonic() < deadline
                time.sleep(.01)
            if not reopen and not broken:
                # Other disk images bypass the System/Data locks, proving the
                # utility file itself has exclusive writer ownership.
                for name in ['other-system.raw', 'other-data.raw']:
                    (root / name).write_bytes(bytes(4*4096))
                competing = common.copy()
                competing[2] = machine.replace('/system.raw', '/other-system.raw').replace('/data.raw', '/other-data.raw')
                result = subprocess.run(competing, capture_output=True, timeout=5)
                assert result.returncode != 0 and b'already in use' in result.stderr, result.stderr
            sock = socket.socket(socket.AF_UNIX); sock.settimeout(5); sock.connect(str(sockpath))
            with sock, sock.makefile('rwb') as fp:
                json.loads(fp.readline()); command(fp, 'qmp_capabilities')
                if broken == 'truncate':
                    with utility.open('r+b') as ufp: ufp.truncate(IMAGE_SIZE-4096)
                elif broken == 'header':
                    with utility.open('r+b') as ufp: ufp.write(b'BROKEN')
                command(fp, 'cont')
                while True:
                    dump = command(fp, 'human-monitor-command', {'command-line':f'xp /{len(expected)}gx {OUTPUT:#x}'})
                    actual = [int(x,16) for x in re.findall(r'0x([0-9a-fA-F]+)', dump)]
                    if actual and actual[-1] == 0xface: break
                    assert time.monotonic() < deadline, dump
                    time.sleep(.01)
                assert len(actual) == len(expected), (actual, expected)
                assert all((want[0] <= got <= want[1]) if isinstance(want,tuple)
                           else want == got for got,want in zip(actual,expected)), (actual,expected)
                command(fp, 'quit')
            assert proc.wait(timeout=5) == 0
        finally:
            if proc.poll() is None: proc.kill(); proc.wait(timeout=5)
    return common


def make_case(root):
    root.mkdir()
    for name in ['system.raw','data.raw']: (root/name).write_bytes(bytes(4*4096))
    utility = root/'utility.raw'
    utility.write_bytes(utility_header(4,4,EPOCH) + b'\xff'*(IMAGE_SIZE-4096))
    return utility


def main():
    with tempfile.TemporaryDirectory(prefix='a7utility-',dir='/tmp') as directory:
        root = Path(directory)
        case = root/'valid'; utility = make_case(case)
        original = utility.read_bytes()
        common = invoke(case,utility)
        updated = utility.read_bytes()
        assert updated[:4096] == original[:4096], 'Utility commands touched private metadata'
        page = 1
        for index,(_,_,count) in enumerate(REGIONS):
            marker = struct.pack('<Q',(index+1)*0x1111111111111111)
            assert updated[(page+count-1)*4096:(page+count-1)*4096+8] == marker
            page += count
        assert (case/'system.raw').read_bytes() == bytes(4*4096), 'Physical FORMAT altered System'
        assert (case/'data.raw').read_bytes() == bytes(4*4096), 'Physical FORMAT altered Data'
        invoke(case,utility,reopen=True)
        for fault in ['truncate','header']:
            case = root/fault; utility = make_case(case)
            invoke(case,utility,broken=fault)
        # Invalid/incomplete model containers fail attachment; none become formatted.
        mutations = [('crc', lambda h:h.__setitem__(0,h[0]^1),False),
                     ('source',lambda h:h.__setitem__(56,h[56]^1),True),
                     ('geometry',lambda h:struct.pack_into('<Q',h,40,5),True),
                     ('region',lambda h:struct.pack_into('<Q',h,136,0),True),
                     ('reserved',lambda h:h.__setitem__(320,1),True),
                     ('epoch',lambda h:struct.pack_into('<Q',h,32,1<<32),True),
                     ('incomplete',lambda h:h.__setitem__(slice(0,4096),bytes(4096)),False)]
        for name,mutate,recrc in mutations:
            case=root/name; utility=make_case(case)
            header=bytearray(utility.read_bytes()[:4096]); mutate(header)
            if recrc: struct.pack_into('<I',header,4092,zlib.crc32(header[:4092]))
            with utility.open('r+b') as fp: fp.write(header)
            image=case/'guest.bin'; image.write_bytes(fixture()[0])
            machine=f's5l8960x,boot-entry={BASE:#x},boot-args={BASE+0x1000:#x},system-image={case}/system.raw,data-image={case}/data.raw,utility-image={utility}'
            before=hashlib.sha256(utility.read_bytes()).hexdigest()
            result=subprocess.run([str(BINARY),'-machine',machine,'-kernel',str(image),'-display','none'],capture_output=True,timeout=5)
            assert result.returncode !=0 and b'utility container' in result.stderr,result.stderr
            assert hashlib.sha256(utility.read_bytes()).hexdigest()==before,'Invalid container was reset'
    print('PASS: six real utility regions, erased bytes, native typed writes/readback, fsync and fresh-QEMU guest persistence')
    print('PASS: native DMA/count/region bounds, metadata preservation, ownership, incomplete/corrupt/source/geometry rejection')
    print('PASS: utility ready and format epoch reflect committed backing; post-open faults unformat/error; FORMAT15/16 unsupported')
    print('Logical model only; no native NAND format, original utility content, keys or factory identities claimed.')

if __name__=='__main__':main()
