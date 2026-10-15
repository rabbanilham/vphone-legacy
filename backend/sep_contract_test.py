#!/usr/bin/env python3
"""Guest-executed software SEP contracts; no native SEP/crypto claim."""
import json
from pathlib import Path
import re
import socket
import struct
import subprocess
import tempfile
import time
from gpio_contract_test import move64
from smoke_test import BINARY, command
from breakpoint_probe import rsp

BASE = 0x800000000
OUTPUT = BASE + 0x200000
GENERATION = OUTPUT + 0x1000
SEP = 0x20d000000
AIC = 0x20e100000


def pack(ep, tag, op, data=0, param=0):
    return ep | tag << 8 | op << 16 | param << 24 | data << 32


def fixture(finish=6, art=True, pending_reset=False, queued_finish=False, notification_backpressure=False):
    code = move64(4, OUTPUT)
    expected = []

    def write32(address, value):
        code.extend(move64(2, address) + move64(0, value) + [0xb9000040])

    def write64(address, value):
        code.extend(move64(2, address) + move64(0, value) + [0xf9000040])

    def read(address, value, wide=False):
        code.extend(move64(2, address) + [0xf9400043 if wide else 0xb9400043,
                    0xf9000003 | len(expected) << 10 | 4 << 5])
        expected.append(value)

    def empty(inbox_irq=False):
        read(SEP + 0x1020, 0x20001)
        read(AIC + 0x4204, 0x1000 if inbox_irq else 0)

    def send(op, tag=0x81, data=0, split=False, ep=0xff, param=0):
        msg = pack(ep, tag, op, data, param)
        if split:
            write32(SEP + 0x1010, msg & 0xffffffff)
            write32(SEP + 0x1014, msg >> 32)
        else:
            write64(SEP + 0x1010, msg)

    def reply(tag=0x81, status=0, split=False):
        read(SEP + 0x1020, 1)
        read(AIC + 0x4204, 0x8000)
        msg = pack(0xff, tag, 0x66, status)
        if split:
            read(SEP + 0x1038, msg & 0xffffffff)
            read(SEP + 0x103c, msg >> 32)
        else:
            read(SEP + 0x1038, msg, True)

    def delay():
        # Three milliseconds at the machine's 24MHz counter frequency.
        code.extend(move64(7, 72000) + [0xd53be025])
        code.extend([0xd53be026, 0xcb0500c6, 0xeb0700df,
                     0x54000003 | (0x7fffd << 5)])

    # Persistent RAM generation distinguishes completed execution after QMP reset
    # from stale results left by the previous boot.
    code.extend(move64(2, GENERATION) + [0xf9400040, 0x91000400, 0xf9000040])
    if pending_reset:
        # Allow any stale pre-reset notification timer to fire before checking
        # reset state. CNTPCT uses the machine's 24MHz timebase (3ms here).
        delay()
    read(SEP + 0x1020, 0x20000)
    read(AIC + 0x4204, 0)
    write32(SEP + 0x1020, 1)
    write32(SEP + 0x28, 0x10)
    write32(SEP + 0x1004, 0x1000)
    send(0x15, ep=7)  # Unsupported service must not get a success reply.
    empty()
    write32(SEP + 0x1004, 1)
    read(AIC + 0x4204, 0x1000)  # Inbox-empty IRQ44, independent of outbox IRQ47.
    write32(SEP + 0x1000, 1)
    read(AIC + 0x4204, 0)
    read(SEP + 0x1004, 0x1000)
    write32(SEP + 0x884, 1)
    send(7, data=(BASE + 0x300000) >> 12)  # Invalid boot order.
    send(8)
    send(0x55)
    empty()
    send(2, tag=0xe2, split=True)
    reply(tag=0xe2, status=1, split=True)
    send(2)
    reply(status=1)
    # Full queue rejects BootTz0 without advancing its phase.
    for tag in range(64):
        send(2, tag=tag)
    read(SEP + 0x1020, 0x10001)
    read(SEP + 0x1008, 0x10000)
    send(5)
    for tag in range(64):
        read(SEP + 0x1038, pack(0xff, tag, 0x66, 1), True)
    empty()
    send(2)
    reply(status=1)
    if queued_finish:
        # Two identical zero-status replies: consuming BootTz0's response
        # must not schedule OS-active before the actual finish response.
        send(5)
        send(finish)
        reply()
        delay()
        reply()
        empty()
    else:
        send(5, param=1)
        reply()
        send(5)  # Duplicate command must not advance or ACK.
        empty()
        send(2, tag=0x31)
        reply(tag=0x31, status=2)
        send(2, tag=0x32)
        reply(tag=0x32, status=2)
        send(7, data=0xffffffff)  # Outside AP RAM.
        empty()
        if art:
            send(7, data=(BASE + 0x300000) >> 12, split=True)
            reply(split=True)
            send(7, data=(BASE + 0x300000) >> 12)
            empty()
        send(6, data=0)
        empty()
        send(finish, tag=0xfe, data=(BASE + 0x400000) >> 12 if finish == 6 else 0)
        reply(tag=0xfe)
    if pending_reset:
        # The runner's GDB breakpoint stops immediately after the marker,
        # before the 1ms notification. QMP reset must cancel this pending event.
        code.extend(move64(2, GENERATION) + [0xf9400040])
        code.extend(move64(1, 0xface) + [0x8b010000,
                    0xf9000000 | len(expected) << 10 | 4 << 5, 0x14000000])
        return struct.pack('<' + 'I' * len(code), *code), expected
    if notification_backpressure:
        code.extend(move64(2, SEP + 0x1010))
        code.extend(move64(0, pack(0xff, 0, 2)))
        code.extend(move64(1, 64))
        code.extend([0xf9000040, 0x91040000, 0xf1000421, 0x54ffffa1])
        delay()
        read(SEP + 0x1020, 0x10001)
        for tag in range(64):
            read(SEP + 0x1038, pack(0xff, tag, 0x66, 2), True)
    # Poll until asynchronous OS-active is present. The host timeout catches stalls.
    code.extend(move64(2, SEP + 0x1020))
    code.extend([0xb9400043, 0x37000000 | 17 << 19 | 0x3fff << 5 | 3])
    read(SEP + 0x1038, pack(0, 0, 0x0d), True)
    empty()
    send(finish, tag=0xfe, data=(BASE + 0x400000) >> 12 if finish == 6 else 0)
    send(5)
    send(0x15, ep=7, data=0x12345678)
    empty()
    send(2)
    reply(status=2)
    empty()
    # Completion marker includes the reset generation; publish only after reads.
    code.extend(move64(2, GENERATION) + [0xf9400040])
    code.extend(move64(1, 0xface) + [0x8b010000,
                0xf9000000 | len(expected) << 10 | 4 << 5, 0x14000000])
    return struct.pack('<' + 'I' * len(code), *code), expected


def run_fixture(finish, art, pending_reset=False, queued_finish=False, notification_backpressure=False):
    payload, expected = fixture(finish, art, pending_reset, queued_finish, notification_backpressure)
    with tempfile.TemporaryDirectory(prefix='a7sep-', dir='/tmp') as directory:
        root = Path(directory)
        image = root / 'fixture.bin'
        image.write_bytes(payload)
        args = [
            str(BINARY), '-machine',
            f's5l8960x,boot-entry={BASE:#x},boot-args={BASE + 0x1000:#x}',
            '-kernel', str(image), '-accel', 'tcg', '-display', 'none', '-S',
            '-d', 'unimp,guest_errors', '-D', str(root / 'trace.log'),
            '-qmp', f'unix:{root}/qmp.sock,server=on,wait=off'
        ]
        if pending_reset:
            args.extend(['-gdb', f'unix:{root}/gdb.sock,server=on,wait=off'])
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        gs = None
        try:
            deadline = time.monotonic() + 10
            while not (root / 'qmp.sock').exists():
                assert proc.poll() is None, proc.stderr.read().decode()
                assert time.monotonic() < deadline
                time.sleep(.01)
            sock = socket.socket(socket.AF_UNIX)
            sock.settimeout(5)
            sock.connect(str(root / 'qmp.sock'))
            with sock, sock.makefile('rwb') as fp:
                json.loads(fp.readline())
                command(fp, 'qmp_capabilities')
                if pending_reset:
                    gs = socket.socket(socket.AF_UNIX)
                    gs.settimeout(5)
                    gs.connect(str(root / 'gdb.sock'))
                    assert rsp(gs, f'Z1,{BASE + len(payload) - 4:x},4') == 'OK'
                for generation in (1, 2):
                    if generation == 2:
                        command(fp, 'stop')
                        command(fp, 'system_reset')
                    command(fp, 'cont')
                    deadline = time.monotonic() + 15
                    while True:
                        dump = command(fp, 'human-monitor-command', {
                            'command-line': f'xp /{len(expected) + 1}gx {OUTPUT:#x}'
                        })
                        actual = [int(x, 16) for x in re.findall(r'0x([0-9a-fA-F]+)', dump)]
                        if actual and actual[-1] == 0xface + generation:
                            break
                        assert time.monotonic() < deadline, (dump, (root/'trace.log').read_text()[-6000:])
                        time.sleep(.01)
                    assert actual == expected + [0xface + generation], (actual, expected)
                    if not pending_reset:
                        # Let virtual timers run, then prove no second notification.
                        time.sleep(.02)
                        status = command(fp, 'human-monitor-command', {
                            'command-line': f'xp /1wx {SEP + 0x1020:#x}'})
                        assert int(re.findall(r'0x([0-9a-fA-F]+)', status)[0], 16) == 0x20001, status
                command(fp, 'quit')
            assert proc.wait(timeout=5) == 0
            trace = (root / 'trace.log').read_text()
            assert trace.count('SEP software model OS-active') == (0 if pending_reset else 2), trace[-3000:]
            assert 'SEP unsupported request: ep=0x7' in trace
            assert trace.count('request not executed') == 2
        finally:
            if gs is not None:
                gs.close()
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
    print(f'PASS: SEP finish={finish}, ART={art}, pending-reset={pending_reset}, queued-finish={queued_finish}, notification-backpressure={notification_backpressure}, boot/reboot, wire layout, FIFO, IRQs, backpressure, invalid requests, OS-active lifecycle')


def service_fixture():
    code = move64(4, OUTPUT)
    expected = []

    def write32(address, value):
        code.extend(move64(2, address) + move64(0, value) + [0xb9000040])

    def write64(address, value):
        code.extend(move64(2, address) + move64(0, value) + [0xf9000040])

    def read(address, value, wide=False):
        code.extend(move64(2, address) + [0xf9400043 if wide else 0xb9400043,
                    0xf9000003 | len(expected) << 10 | 4 << 5])
        expected.append(value)

    def empty():
        read(SEP + 0x1020, 0x20001)
        read(AIC + 0x4204, 0x1000)

    def send(op, tag=0x81, data=0, ep=0xff, param=0):
        msg = pack(ep, tag, op, data, param)
        write64(SEP + 0x1010, msg)

    def reply(tag=0x81, status=0):
        read(SEP + 0x1020, 1)
        read(AIC + 0x4204, 0x9000)
        read(SEP + 0x1038, pack(0xff, tag, 0x66, status), True)

    code.extend(move64(2, GENERATION) + [0xf9400040, 0x91000400, 0xf9000040])
    read(SEP + 0x1020, 0x20000)
    write32(SEP + 0x1020, 1)
    write32(SEP + 0x28, 0x10)
    write32(SEP + 0x1004, 1)
    # Fast bootstrap through finish
    send(2)
    reply(status=1)
    send(5, param=1)
    reply()
    send(2)
    reply(status=2)
    send(7, data=(BASE + 0x300000) >> 12)
    reply()
    send(6, tag=0xfe, data=(BASE + 0x400000) >> 12)
    reply(tag=0xfe)
    # Wait for OS-active
    code.extend(move64(2, SEP + 0x1020))
    code.extend([0xb9400043, 0x37000000 | 17 << 19 | 0x3fff << 5 | 3])
    read(SEP + 0x1038, pack(0, 0, 0x0d), True)
    # 1. EP0 setReceiveOOLBuffer (op 3, param 3=EP3, page=0x809184)
    send(3, ep=0, tag=8, param=3, data=0x809184)
    read(SEP + 0x1038, pack(0, 8, 0x65, data=0), True)
    # 2. EP0 setSendOOLBuffer (op 2, param 2=EP2, page=0x809223)
    send(2, ep=0, tag=7, param=2, data=0x809223)
    read(SEP + 0x1038, pack(0, 7, 0x65, data=0x809223), True)
    # 3. EP0 getParam (op 2, param 2, data=0) -> returns stored page 0x809223
    send(2, ep=0, tag=9, param=2, data=0)
    read(SEP + 0x1038, pack(0, 9, 0x67, data=0x809223), True)
    # 4. EP0 keepalive (op 0x13, tag 0) -> consumed without ACK
    send(0x13, ep=0, tag=0)
    empty()
    # 5. EP2 ART_LOAD (op 2, tag 0xa) -> ACK with op 3, status 0
    send(2, ep=2, tag=0xa)
    read(SEP + 0x1038, pack(2, 0xa, 3, data=0, param=0), True)
    # 6. EP7 get_lock_state (cmd 0x11, tag 0x22, len 8)
    send(3, ep=0, tag=0xb, param=7, data=(BASE + 0x500000) >> 12)
    read(SEP + 0x1038, pack(0, 0xb, 0x65, data=0), True)
    write64(BASE + 0x500000, 0xdeadbeefcafebabe)
    write64(BASE + 0x500008, 0x0123456789abcdef)
    write64(SEP + 0x1010, 7 | (0x11 << 8) | (0x22 << 16) | (8 << 48))
    read(SEP + 0x1038, 7 | (0x11 << 8) | (1 << 15) | (0x22 << 16) | (0x10 << 48), True)
    read(BASE + 0x500000, 0, True)
    read(BASE + 0x500008, 0, True)
    # 7. EP7 register_client / session init (cmd 0x12, tag 0x33, len 28)
    write64(SEP + 0x1010, 7 | (0x12 << 8) | (0x33 << 16) | (0x1c << 48))
    read(SEP + 0x1038, 7 | (0x12 << 8) | (1 << 15) | (0x33 << 16), True)
    # 8. EP7 key locker / keybag op (cmd 0x1a, tag 0x44, len 48)
    write64(SEP + 0x1010, 7 | (0x1a << 8) | (0x44 << 16) | (0x30 << 48))
    read(SEP + 0x1038, 7 | (0x1a << 8) | (1 << 15) | (0x44 << 16), True)
    # 9. Native selector14 query: context, system bag=0, operation=-1,
    # empty blob. Error responses do not DMA into receive OOL memory.
    send(2, ep=0, tag=0xc, param=7, data=(BASE + 0x600000) >> 12)
    read(SEP + 0x1038, pack(0, 0xc, 0x65, data=(BASE + 0x600000) >> 12), True)
    write64(BASE + 0x600000, 0x12345678)
    write64(BASE + 0x600008, 0xffffffff)
    write64(BASE + 0x500000, 0x1122334455667788)
    write64(SEP + 0x1010, 7 | (0x04 << 8) | (0x55 << 16) | (0x10 << 48))
    read(SEP + 0x1038, 7 | (0x04 << 8) | (1 << 15) | (0x55 << 16) | (0xfd << 24), True)
    read(BASE + 0x500000, 0x1122334455667788, True)
    # Non-system bag, other operation, malformed size/offset stay unsupported.
    for request_head, operation, length, offset in (
            (0x100000000, 0xffffffff, 16, 0),
            (0x12345678, 1, 16, 0),
            (0x12345678, 0xffffffff, 12, 0),
            (0x12345678, 0xffffffff, 16, 4090)):
        write64(BASE + 0x600000, request_head)
        write64(BASE + 0x600008, operation)
        write64(SEP + 0x1010, 7 | (4 << 8) | (0x56 << 16) | (offset << 32) | (length << 48))
        empty()
    write64(BASE + 0x600000, 0x12345678)
    write64(BASE + 0x600008, 0xffffffff)
    # 10. EP7 keybag operation / state (cmd 0x16, tag 0x66, len 4) -> reply len 0
    write64(SEP + 0x1010, 7 | (0x16 << 8) | (0x66 << 16) | (4 << 48))
    read(SEP + 0x1038, 7 | (0x16 << 8) | (1 << 15) | (0x66 << 16), True)
    # 11. Cmd25 replies with 8 bytes, preserving the following sentinel.
    write64(BASE + 0x500000, 0xfacefeedbaadf00d)
    write64(BASE + 0x500008, 0x55aa55aa33cc33cc)
    write64(SEP + 0x1010, 7 | (0x19 << 8) | (0x70 << 16) | (8 << 48))
    read(SEP + 0x1038, 7 | (0x19 << 8) | (1 << 15) | (0x70 << 16) | (8 << 48), True)
    read(BASE + 0x500000, 0, True)
    read(BASE + 0x500008, 0x55aa55aa33cc33cc, True)
    # 12. EP7 keybag create (cmd 0x02, tag 0x71, len 16) -> reply len 0
    write64(SEP + 0x1010, 7 | (0x02 << 8) | (0x71 << 16) | (16 << 48))
    read(SEP + 0x1038, 7 | (0x02 << 8) | (1 << 15) | (0x71 << 16), True)
    # 13. EP7 keybag session / system set (cmd 0x0d, tag 0x72, len 32) -> reply len 0
    write64(SEP + 0x1010, 7 | (0x0d << 8) | (0x72 << 16) | (32 << 48))
    read(SEP + 0x1038, 7 | (0x0d << 8) | (1 << 15) | (0x72 << 16), True)
    # 14. EP7 system keybag unlock (cmd 0x1e, tag 0x73, len 48) -> reply len 0
    write64(SEP + 0x1010, 7 | (0x1e << 8) | (0x73 << 16) | (48 << 48))
    read(SEP + 0x1038, 7 | (0x1e << 8) | (1 << 15) | (0x73 << 16), True)
    # A generic create/set/unlock ACK is not proof of an imported system bag.
    # Until an import contract is implemented, query stays absent even after them.
    write64(SEP + 0x1010, 7 | (4 << 8) | (0x74 << 16) | (16 << 48))
    read(SEP + 0x1038, 7 | (4 << 8) | (1 << 15) | (0x74 << 16) | (0xfd << 24), True)
    # 15. EP7 keybag create with data (cmd 0x03, tag 0x75, len 24) -> handle 1, reply len 4
    write32(BASE + 0x600000, 0x11223344)
    write32(BASE + 0x600004, 16)
    write64(BASE + 0x600008, 0x0123456789abcdef)
    write64(BASE + 0x600010, 0xfedcba9876543210)
    write64(BASE + 0x500000, 0xdeadbeefcafebabe)
    write64(SEP + 0x1010, 7 | (0x03 << 8) | (0x75 << 16) | (24 << 48))
    read(SEP + 0x1038, 7 | (0x03 << 8) | (1 << 15) | (0x75 << 16) | (4 << 48), True)
    read(BASE + 0x500000, 1, False)
    read(BASE + 0x500004, 0xdeadbeef, False)
    # 16. EP7 keybag copy data (cmd 0x06, tag 0x78, len 8) -> returns 16-byte blob, reply len 20
    write32(BASE + 0x600000, 0x11223344)
    write32(BASE + 0x600004, 1)
    write64(SEP + 0x1010, 7 | (0x06 << 8) | (0x78 << 16) | (8 << 48))
    read(SEP + 0x1038, 7 | (0x06 << 8) | (1 << 15) | (0x78 << 16) | (20 << 48), True)
    read(BASE + 0x500000, 16, False)
    read(BASE + 0x500004, 0x89abcdef, False)
    read(BASE + 0x500008, 0x01234567, False)
    read(BASE + 0x50000c, 0x76543210, False)
    read(BASE + 0x500010, 0xfedcba98, False)
    # Wrong handle (handle 2) -> unsupported, no reply
    write32(BASE + 0x600004, 2)
    write64(SEP + 0x1010, 7 | (0x06 << 8) | (0x79 << 16) | (8 << 48))
    empty()
    # 17. EP7 keybag free (cmd 0x05, tag 0x76, len 8) -> reply len 0
    write64(SEP + 0x1010, 7 | (0x05 << 8) | (0x76 << 16) | (8 << 48))
    read(SEP + 0x1038, 7 | (0x05 << 8) | (1 << 15) | (0x76 << 16), True)
    # 18. Malformed Cmd 3 (length < 8) -> no reply
    write64(SEP + 0x1010, 7 | (0x03 << 8) | (0x77 << 16) | (4 << 48))
    empty()
    # 19. EP7 Cmd 10 (cmd 0x0a, tag 0x7a, len 4) -> reply len 0
    write64(SEP + 0x1010, 7 | (0x0a << 8) | (0x7a << 16) | (4 << 48))
    read(SEP + 0x1038, 7 | (0x0a << 8) | (1 << 15) | (0x7a << 16), True)
    # 20. EP7 Cmd 7 (cmd 0x07, tag 0x7b, len 8) -> reply len 4
    write64(SEP + 0x1010, 7 | (0x07 << 8) | (0x7b << 16) | (8 << 48))
    read(SEP + 0x1038, 7 | (0x07 << 8) | (1 << 15) | (0x7b << 16) | (4 << 48), True)
    read(BASE + 0x500000, 0, False)
    # 21. EP7 Cmd 8 unwrap (cmd 0x08, tag 0x7c, len 28) -> reply len 16
    write32(BASE + 0x600000, 0x11223344)
    write32(BASE + 0x600004, 1)
    write32(BASE + 0x600008, 0)
    write32(BASE + 0x60000c, 8)
    write64(BASE + 0x600010, 0x1122334455667788)
    write32(BASE + 0x600018, 0)
    write64(SEP + 0x1010, 7 | (0x08 << 8) | (0x7c << 16) | (28 << 48))
    read(SEP + 0x1038, 7 | (0x08 << 8) | (1 << 15) | (0x7c << 16) | (16 << 48), True)
    read(BASE + 0x500000, 8, False)
    read(BASE + 0x500004, 0x55667788, False)
    read(BASE + 0x500008, 0x11223344, False)
    read(BASE + 0x50000c, 0, False)
    # 22. Unsupported command on EP7 (cmd 0x15) -> no reply
    send(0x15, ep=7)
    empty()

    code.extend(move64(2, GENERATION) + [0xf9400040])
    code.extend(move64(1, 0xface) + [0x8b010000,
                0xf9000000 | len(expected) << 10 | 4 << 5, 0x14000000])
    return struct.pack('<' + 'I' * len(code), *code), expected


def run_service_fixture():
    payload, expected = service_fixture()
    with tempfile.TemporaryDirectory(prefix='a7sep-svc-', dir='/tmp') as directory:
        root = Path(directory)
        image = root / 'fixture.bin'
        image.write_bytes(payload)
        args = [
            str(BINARY), '-machine',
            f's5l8960x,boot-entry={BASE:#x},boot-args={BASE + 0x1000:#x}',
            '-kernel', str(image), '-accel', 'tcg', '-display', 'none', '-S',
            '-d', 'unimp,guest_errors', '-D', str(root / 'trace.log'),
            '-qmp', f'unix:{root}/qmp.sock,server=on,wait=off'
        ]
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not (root / 'qmp.sock').exists():
                assert proc.poll() is None, proc.stderr.read().decode()
                assert time.monotonic() < deadline
                time.sleep(.01)
            sock = socket.socket(socket.AF_UNIX)
            sock.settimeout(5)
            sock.connect(str(root / 'qmp.sock'))
            with sock, sock.makefile('rwb') as fp:
                json.loads(fp.readline())
                command(fp, 'qmp_capabilities')
                for generation in (1, 2):
                    if generation == 2:
                        command(fp, 'stop')
                        command(fp, 'system_reset')
                    command(fp, 'cont')
                    deadline = time.monotonic() + 15
                    while True:
                        dump = command(fp, 'human-monitor-command', {
                            'command-line': f'xp /{len(expected) + 1}gx {OUTPUT:#x}'
                        })
                        actual = [int(x, 16) for x in re.findall(r'0x([0-9a-fA-F]+)', dump)]
                        if actual and actual[-1] == 0xface + generation:
                            break
                        assert time.monotonic() < deadline, (dump, (root/'trace.log').read_text()[-6000:])
                        time.sleep(.01)
                    assert actual == expected + [0xface + generation], (actual, expected)
                command(fp, 'quit')
            assert proc.wait(timeout=5) == 0
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
    print('PASS: SEP post-boot service endpoints: EP0 (OOL/params/keepalive), EP2 (ART_LOAD), EP7 (Cmd2/4/13/17/18/22/25/26/30, reject unsupported)')


def main():
    run_fixture(6, True)
    time.sleep(0.2)
    run_fixture(8, False)
    time.sleep(0.2)
    run_fixture(6, True, pending_reset=True)
    time.sleep(0.2)
    run_fixture(8, False, queued_finish=True)
    time.sleep(0.2)
    run_fixture(6, True, notification_backpressure=True)
    time.sleep(0.2)
    run_service_fixture()


if __name__ == '__main__':
    main()
