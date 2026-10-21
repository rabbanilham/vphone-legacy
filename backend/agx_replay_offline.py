"""Offline high-level replay of a backboardd AGX frame dump into a PNG.

Walks the VDM stream from the TA command, decodes each draw's vertices
(vertex PDS data: stride, buffer) and texture state words (PPP -> fragment
PDS data -> TSW), then rasterises axis-aligned quads with src-over blending.
"""
import struct
import sys
import zlib

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
from agx_frame import Frame

PDS_HEAP = 0x600000000
W, H = 640, 1136


def png(path, w, h, rgba):
    raw = b''.join(b'\x00' + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))

    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    open(path, 'wb').write(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0)) +
                           chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


def walk(f):
    """Yield (vertex_pds_data, ppp_words, draw_words) per draw."""
    pc = struct.unpack_from('<Q', f.ta_command, 0x68)[0]
    vs_data, ppp = None, []
    for _ in range(4096):
        w0 = f.u32(pc)
        kind = w0 >> 29
        if kind == 6:
            return
        if kind == 4:
            pc = ((w0 & 0xff) << 32) | (f.u32(pc + 4) & ~3)
            continue
        if kind == 2:
            extra = (1 if w0 & (1 << 28) else 0) + (2 if w0 & (1 << 27) else 0) + (2 if w0 & (1 << 26) else 0)
            if w0 & (1 << 27):
                vs_data = PDS_HEAP | f.u32(pc + 4 + (4 if w0 & (1 << 28) else 0))
            pc += 4 * (1 + extra)
        elif kind == 1:
            pc += 12
        elif kind == 0:
            count = (w0 >> 8) & 0xfffff
            gva = ((w0 & 0xff) << 32) | (f.u32(pc + 4) & ~3)
            ppp = [f.u32(gva + 4 * i) for i in range(count)]
            pc += 8
        elif kind == 3:
            draw = [f.u32(pc + 4 * i) for i in range(3)]
            yield vs_data, ppp, draw
            pc += 12
        else:
            raise ValueError(f'unknown VDM block {w0:#x} at {pc:#x}')


def vertices(f, vs_data, count):
    stride, buf = f.u32(vs_data), f.u64(vs_data + 8)
    out = []
    for i in range(count):
        v = f.read(buf + i * stride, stride)
        x, y = struct.unpack_from('<2f', v, 0)
        extra = v[16:]
        out.append((x, y, extra))
    return stride, out


def texture(f, ppp):
    """Find the TSW through a PDS-heap fragment data pointer in the PPP words."""
    for word in ppp:
        if word & 0xc0000000 != 0x80000000:
            continue
        try:
            ptr = f.u64(PDS_HEAP | word)
            tsw = [f.u32(ptr + 4 * i) for i in range(4)]
        except (KeyError, ValueError):
            continue
        w = ((tsw[1] >> 2) & 0x3fff) + 1
        h = (tsw[1] >> 16) + 1
        q = tsw[2] | (tsw[3] << 32)
        addr = ((q >> 16) & 0x3ffffffff) << 2
        if addr in f.pages or (addr & ~0xfff) in f.pages:
            return dict(tsw=tsw, w=w, h=h, addr=addr, stride=((w + 15) & ~15) * 4)
    return None


def main():
    f = Frame(sys.argv[1])
    fb = bytearray(W * H * 4)
    for vs_data, ppp, draw in walk(f):
        count = draw[1]
        stride, vs = vertices(f, vs_data, count)
        xs = [v[0] for v in vs]
        ys = [v[1] for v in vs]
        x0, x1 = max(0, int(min(xs))), min(W, int(max(xs)))
        y0, y1 = max(0, int(min(ys))), min(H, int(max(ys)))
        tex = texture(f, ppp)
        print(f'draw n={count} stride={stride} rect=({x0},{y0})-({x1},{y1}) tex={tex and {k: (hex(v) if isinstance(v, int) else v) for k, v in tex.items()}}')
        if tex:
            data = f.read(tex['addr'], tex['stride'] * tex['h'])
            for y in range(y0, y1):
                ty = min(tex['h'] - 1, (y - y0) * tex['h'] // max(1, y1 - y0))
                for x in range(x0, x1):
                    tx = min(tex['w'] - 1, (x - x0) * tex['w'] // max(1, x1 - x0))
                    o = ty * tex['stride'] + tx * 4
                    b, g, r, a = data[o:o + 4]
                    d = (y * W + x) * 4
                    for c, s in enumerate((r, g, b)):
                        fb[d + c] = s + fb[d + c] * (255 - a) // 255  # premultiplied src-over
                    fb[d + 3] = 255
        else:
            colour = struct.unpack_from('<I', vs[0][2], 0)[0] if len(vs[0][2]) >= 4 else 0xff000000
            r, g, b, a = (colour >> 16) & 0xff, (colour >> 8) & 0xff, colour & 0xff, colour >> 24
            for y in range(y0, y1):
                fb[(y * W + x0) * 4:(y * W + x1) * 4] = bytes((r, g, b, a)) * (x1 - x0)
    png(sys.argv[2], W, H, bytes(fb))


main()
