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
            extra = (1 if w0 & (1 << 28) else 0) + (1 if w0 & (1 << 27) else 0) + (3 if w0 & (1 << 26) else 0)
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
        out.append((x, y, v[16:]))
    return stride, out


LUMA = (0x3d93a000, 0x3e59a000, 0x3f372000)  # Rec.709 weights: vibrancy/desaturate shaders


def fragment_words(f, ppp):
    """Words reachable from the PPP's PDS-heap fragment data pointers."""
    found = []
    for word in ppp:
        if word & 0xc0000000 != 0x80000000:
            continue
        base = PDS_HEAP | word
        try:
            data = [f.u32(base + 4 * i) for i in range(8)]
        except (KeyError, ValueError):
            continue
        for k in range(0, 8, 2):
            ptr = data[k] | (data[k + 1] << 32)
            if 0x1000 <= ptr < (1 << 40):
                try:
                    found.append((ptr, [f.u32(ptr + 4 * i) for i in range(12)]))
                except (KeyError, ValueError):
                    pass
    return found


def decode_tsw(f, tsw):
    if tsw[0] >> 28 != 6:
        return None
    w = ((tsw[1] >> 2) & 0x3fff) + 1
    h = (tsw[1] >> 16) + 1
    q = tsw[2] | (tsw[3] << 32)
    addr = ((q >> 16) & 0x3ffffffff) << 2
    if not addr or (addr & ~0xfff) not in f.pages:
        return None
    strip = (tsw[0] & 7) == 1
    # Linear textures carry (pitch in pixels - 1) in TSW word 2 bits [15:0];
    # GPU-rendered surfaces use 64-pixel aligned rows in 16-row strips.
    pitch = ((tsw[2] & 0xffff) + 1) * 4
    stride = ((w + 63) & ~63) * 4 if strip else (pitch if pitch >= w * 4 else ((w + 15) & ~15) * 4)
    return dict(w=w, h=h, addr=addr, strip=strip, stride=stride, tsw=tsw)


def texture(f, ppp):
    """First texture state word block among the fragment data; None for vibrancy."""
    words = fragment_words(f, ppp)
    if any(tuple(ws[:3]) == LUMA for _, ws in words):
        return 'skip'
    for _, ws in words:
        for k in range(0, 9):
            tex = decode_tsw(f, ws[k:k + 4])
            if tex:
                return tex
    return None


def texel(f, tex, x, y):
    if tex['strip']:
        gva = tex['addr'] + (y >> 4) * 0x10000 + (y & 15) * tex['stride'] + x * 4
    else:
        gva = tex['addr'] + y * tex['stride'] + x * 4
    try:
        return f.read(gva, 4)
    except KeyError:
        return b'\0\0\0\0'


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
        print(f'draw n={count} stride={stride} rect=({x0},{y0})-({x1},{y1}) tex=' +
              (tex if isinstance(tex, str) else str(tex and {k: (hex(v) if isinstance(v, int) else v)
                                                         for k, v in tex.items() if k != 'tsw'})))
        if tex == 'skip' or x0 >= x1 or y0 >= y1:
            continue
        if tex:
            # stride 24: float u,v (normalised) at +16; stride 28: BGRA tint at +16,
            # then u,v in texels at +20.
            uv_off = 4 if stride == 28 else 0
            uvs = ([struct.unpack_from('<2f', v[2], uv_off) for v in vs] if stride >= 24
                   else [(0, 0), (1, 1)])
            u0, u1 = min(u for u, _ in uvs), max(u for u, _ in uvs)
            v0, v1 = min(v for _, v in uvs), max(v for _, v in uvs)
            su, sv = (1, 1) if max(u1, v1) > 1.0001 else (tex['w'], tex['h'])
            tint = struct.unpack_from('<I', vs[0][2], 0)[0] if stride == 28 else 0xffffffff
            for y in range(y0, y1):
                ty = min(tex['h'] - 1, int((v0 + (v1 - v0) * (y - y0 + 0.5) / (y1 - y0)) * sv))
                for x in range(x0, x1):
                    tx = min(tex['w'] - 1, int((u0 + (u1 - u0) * (x - x0 + 0.5) / (x1 - x0)) * su))
                    b, g, r, a = texel(f, tex, tx, ty)
                    if tint != 0xffffffff:
                        tb, tg, tr, ta = tint & 0xff, (tint >> 8) & 0xff, (tint >> 16) & 0xff, tint >> 24
                        b, g, r, a = b * tb // 255, g * tg // 255, r * tr // 255, a * ta // 255
                    d = (y * W + x) * 4
                    for c, src in enumerate((r, g, b)):
                        fb[d + c] = min(255, src + fb[d + c] * (255 - a) // 255)
                    fb[d + 3] = 255
        else:
            colour = struct.unpack_from('<I', vs[0][2], 0)[0] if stride == 20 else 0xff000000
            r, g, b, a = (colour >> 16) & 0xff, (colour >> 8) & 0xff, colour & 0xff, colour >> 24
            for y in range(y0, y1):
                fb[(y * W + x0) * 4:(y * W + x1) * 4] = bytes((r, g, b, a)) * (x1 - x0)
    png(sys.argv[2], W, H, bytes(fb))


main()
