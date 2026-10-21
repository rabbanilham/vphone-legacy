"""Walk the VDM control stream of an AGXF frame dump (C6 token table)."""
import struct
import sys

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
from agx_frame import Frame

f = Frame(sys.argv[1])
ta = f.ta_command
w = lambda b, o: struct.unpack_from('<I', b, o)[0]
q = lambda b, o: struct.unpack_from('<Q', b, o)[0]
print('TA +0x68 vdm', hex(q(ta, 0x68)), '+0x98', hex(q(ta, 0x98)),
      '+0xd0', hex(q(ta, 0xd0)), '+0xd8', hex(q(ta, 0xd8)),
      'w/h', w(ta, 0x54), w(ta, 0x58))
for off in range(0, 0x120, 8):
    v = q(ta, off)
    if v:
        print(f'  ta+0x{off:03x} = 0x{v:x}')

pc = q(ta, 0x68)
limit = int(sys.argv[2]) if len(sys.argv) > 2 else 200
n = 0
while n < limit:
    try:
        w0 = f.u32(pc)
    except KeyError as e:
        print('stop', e)
        break
    top = w0 >> 29
    if w0 == 0xc0000000:
        print(f'{pc:#x}: TERMINATE')
        break
    if (w0 >> 30) == 2:
        tgt = ((w0 & 0xff) << 32) | (f.u32(pc + 4) & ~3)
        print(f'{pc:#x}: LINK -> {tgt:#x}')
        pc = tgt
        n += 1
        continue
    if top == 2:
        extra = (1 if w0 & (1 << 28) else 0) + (2 if w0 & (1 << 27) else 0) + (2 if w0 & (1 << 26) else 0)
        words = [f.u32(pc + 4 * i) for i in range(1 + extra)]
        print(f'{pc:#x}: VDM_STATE ' + ' '.join(f'{x:#x}' for x in words))
        pc += 4 * (1 + extra)
        n += 1
        continue
    if top == 1:
        d, c = f.u32(pc + 4), f.u32(pc + 8)
        print(f'{pc:#x}: PDS_STATE w0={w0:#x} data={d:#x} code={c:#x}')
        pc += 12
    elif (w0 >> 24) == 0x6a:
        words = [f.u32(pc + 4 * i) for i in range(5)]
        print(f'{pc:#x}: DRAW ' + ' '.join(f'{x:#x}' for x in words))
        pc += 12 if not (w0 & (1 << 26)) and not (w0 & 0x1f0000) else (16 if w0 & (1 << 26) and not (w0 & 0x1f0000) else 20)
    elif (w0 >> 28) == 0:
        cnt = (w0 >> 8) & 0xfffff
        gva = ((w0 & 0xff) << 32) | (f.u32(pc + 4) & ~3)
        try:
            words = [f.u32(gva + 4 * i) for i in range(min(cnt, 40))]
        except KeyError:
            words = ['unmapped']
        print(f'{pc:#x}: PPP n={cnt} gva={gva:#x} ' + ' '.join(
            f'{x:#x}' if isinstance(x, int) else x for x in words))
        pc += 8
    else:
        print(f'{pc:#x}: ?? {w0:#x} ' + ' '.join(f'{f.u32(pc + 4 * i):#x}' for i in range(1, 6)))
        pc += 4
    n += 1
