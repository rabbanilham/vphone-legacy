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
W, H = 640, 1136  # output canvas; offscreen passes use the top-left corner


def png(path, w, h, rgba):
    raw = b''.join(b'\x00' + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))

    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    open(path, 'wb').write(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0)) +
                           chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


def find_viewport(ppp):
    """PPP viewport block: x scale, x offset, y scale, y offset, then 0.5, 0.5."""
    for i in range(len(ppp) - 5):
        if (ppp[i] == ppp[i + 1] and ppp[i + 2] == ppp[i + 3] and
                ppp[i + 4] == ppp[i + 5] == 0x3f000000 and 0x3f800000 <= ppp[i] < 0x46000000):
            f32 = lambda w: struct.unpack('<f', struct.pack('<I', w))[0]
            return f32(ppp[i]), f32(ppp[i + 1]), f32(ppp[i + 2]), f32(ppp[i + 3])
    return None


def find_matrix(f, vs_data):
    """Orthographic vertex matrix (column-major 4x4) referenced by the vertex PDS data."""
    try:
        words = [f.u32(vs_data + 4 * k) for k in range(20)]
    except KeyError:
        return None
    for k in range(4, 20, 2):
        ptr = words[k] | (words[k + 1] << 32)
        if not 0x1000 <= ptr < (1 << 40):
            continue
        try:
            m = struct.unpack('<16f', f.read(ptr, 64))
        except KeyError:
            continue
        zeros = (1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 14)
        if abs(m[15] - 1) < 1e-6 and m[0] and m[5] and all(m[z] == 0 for z in zeros):
            return m[0], m[5], m[12], m[13]
    return None


def walk(f):
    """Yield (vertex_pds_data, ppp_words, draw_words, viewport) per draw."""
    pc = struct.unpack_from('<Q', f.ta_command, 0x68)[0]
    vs_data, ppp, viewport = None, [], None
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
            viewport = find_viewport(ppp) or viewport
            pc += 8
        elif kind == 3:
            draw = [f.u32(pc + 4 * i) for i in range(3)]
            yield vs_data, ppp, draw, viewport
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
            if not 0x1000 <= ptr < (1 << 40):
                break  # contiguous pointer list; the rest may be the next draw's
            try:
                found.append((ptr, [f.u32(ptr + 4 * i) for i in range(12)]))
            except (KeyError, ValueError):
                break  # unmapped or invalid pointer terminates the contiguous list
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
    # GPU-rendered surfaces (layout 1): display-style 16-row strips when the
    # mapping has a hole at +0xa000, otherwise dense 16-pixel aligned rows.
    # Linear textures carry (pitch in pixels - 1) in TSW word 2 bits [15:0].
    strip = False
    if (tsw[0] & 7) == 1:
        strip = (addr + 0x9000) // 4096 * 4096 in f.pages and (addr + 0xa000) // 4096 * 4096 not in f.pages
        stride = 2560 if strip else ((w + 15) & ~15) * 4
    else:
        pitch = ((tsw[2] & 0xffff) + 1) * 4
        stride = pitch if pitch >= w * 4 else ((w + 15) & ~15) * 4
    return dict(w=w, h=h, addr=addr, strip=strip, stride=stride, tsw=tsw)


def texture(f, ppp):
    """First texture state word block among the fragment data; None for vibrancy."""
    words = fragment_words(f, ppp)
    vibrancy = any(tuple(ws[i:i + 3]) == LUMA for _, ws in words for i in range(6))
    for _, ws in words:
        # TSW at 16-byte aligned 0 or 4; word 7 only behind the luma weights
        # (vibrancy block: luma, four sampler words, TSW), as in the C replay.
        for k in (0, 4, 7):
            if k == 7 and tuple(ws[:3]) != LUMA:
                continue
            if len(ws) >= k + 4:
                tex = decode_tsw(f, ws[k:k + 4])
                if tex:
                    tex['vibrancy'] = vibrancy
                    return tex
    return None


def blend_vibrancy(d, s):
    """darkenSourceOver/lightenSourceOver (agy C8): scale S by Y^4 or (1-Y)^4 of D, src-over."""
    dr, dg, db, da = d
    sr, sg, sb, sa = s
    y = (54 * dr + 183 * dg + 19 * db) >> 8
    y1 = 255 - y if sr + sg + sb > 384 else y
    f = (y1 * y1 // 255) ** 2 // 255
    sr, sg, sb, sa = (c * f // 255 for c in (sr, sg, sb, sa))
    return tuple(min(255, sc + dc * (255 - sa) // 255) for sc, dc in ((sr, dr), (sg, dg), (sb, db), (sa, da)))


DEST_IN = 0x4ee01acc47551344  # USC fingerprint of CA's mask shader: D *= texel alpha

# CoreAnimation narrow Gaussian blur shaders (sigma = 3.5, 6 bilinear tap pairs).
# Recovered from CA::OGL::BlurState::narrow_blur / narrowBlurParameters in QuartzCore.
BLUR_PROGRAMS = {
    0x115ac3fe908dec8a,
    0x90d330877114e752,
    0xb443c1f6a714b429,
    0x21377cafd3277883,
    0x95631b61e7c1be31,
    0x145dbc3101d94f8c,
    0xf0c17d1e30ec2393,
    0x69f1605719a006b2,
    0xa4c025e0be54470a,
}

BLUR_TAPS = [
    (0.6575, 0.1666),
    (2.4492, 0.1759),
    (4.4092, 0.1005),
    (6.3704, 0.0417),
    (8.3332, 0.0126),
    (10.2979, 0.0027),
]


def sample_bilinear(buf, w, h, u, v):
    fu = max(0.0, min(float(w - 1), u - 0.5))
    fv = max(0.0, min(float(h - 1), v - 0.5))
    x0 = int(fu)
    y0 = int(fv)
    x1 = min(x0 + 1, w - 1)
    y1 = min(y0 + 1, h - 1)
    fx = fu - x0
    fy = fv - y0

    c00 = buf[y0 * w + x0]
    c10 = buf[y0 * w + x1]
    c01 = buf[y1 * w + x0]
    c11 = buf[y1 * w + x1]

    out = []
    for c in range(4):
        val = ((c00[c] * (1.0 - fx) + c10[c] * fx) * (1.0 - fy) +
               (c01[c] * (1.0 - fx) + c11[c] * fx) * fy)
        out.append(max(0, min(255, int(round(val)))))
    return tuple(out)


def gaussian_blur_texture(buf, w, h, taps=BLUR_TAPS):
    tmp_buf = [None] * (w * h)
    for y in range(h):
        for x in range(w):
            acc = [0.0, 0.0, 0.0, 0.0]
            for off, weight in taps:
                sp = sample_bilinear(buf, w, h, x + 0.5 + off, y + 0.5)
                sn = sample_bilinear(buf, w, h, x + 0.5 - off, y + 0.5)
                for c in range(4):
                    acc[c] += (sp[c] + sn[c]) * weight
            tmp_buf[y * w + x] = tuple(max(0, min(255, int(round(acc[c])))) for c in range(4))

    out_buf = [None] * (w * h)
    for y in range(h):
        for x in range(w):
            acc = [0.0, 0.0, 0.0, 0.0]
            for off, weight in taps:
                sp = sample_bilinear(tmp_buf, w, h, x + 0.5, y + 0.5 + off)
                sn = sample_bilinear(tmp_buf, w, h, x + 0.5, y + 0.5 - off)
                for c in range(4):
                    acc[c] += (sp[c] + sn[c]) * weight
            out_buf[y * w + x] = tuple(max(0, min(255, int(round(acc[c])))) for c in range(4))
    return out_buf


def program(f, ppp):
    """FNV-1a of the first 64 bytes of the draw's USC fragment code."""
    try:
        off = f.u32(0x600000000 | ppp[7])
        code = f.read(0x700000000 | off, 64)
    except (KeyError, ValueError, IndexError):
        return 0
    h = 0xcbf29ce484222325
    for b in code:
        h = ((h ^ b) * 0x100000001b3) & 0xffffffffffffffff
    return h


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
    pw = (struct.unpack_from('<I', f.type1_command, 0x74)[0] & 0xffff) + 1
    ph = (struct.unpack_from('<I', f.type1_command, 0x70)[0] & 0xffff) + 1
    target_w, target_h = (pw, ph) if (pw != 640 or ph != 1136) else (W, H)
    fb = bytearray(target_w * target_h * 4)
    target = f.header.get('pbe_target')
    if target and target in f.pages:
        strip = ((target + 0x9000) // 4096 * 4096 in f.pages and
                 (target + 0xa000) // 4096 * 4096 not in f.pages)
        stride = 2560 if strip else ((target_w + 15) & ~15) * 4
        for y in range(target_h):
            for x in range(target_w):
                gva = (target + (y >> 4) * 0x10000 + (y & 15) * stride + x * 4 if strip
                       else target + y * stride + x * 4)
                try:
                    b, g, r, a = f.read(gva, 4)
                    off = (y * target_w + x) * 4
                    fb[off:off + 4] = bytes((r, g, b, a))
                except KeyError:
                    pass
    for vs_data, ppp, draw, viewport in walk(f):
        count = draw[1]
        stride, vs = vertices(f, vs_data, count)
        m = find_matrix(f, vs_data) if stride != 12 else None
        vp = viewport or (pw / 2, pw / 2, ph / 2, ph / 2)
        if m:   # screen-space position -> NDC -> pass pixels
            xs = [(m[0] * v[0] + m[2]) * vp[0] + vp[1] for v in vs]
            ys = [(m[1] * v[1] + m[3]) * vp[2] + vp[3] for v in vs]
        else:
            xs = [v[0] for v in vs]
            ys = [v[1] for v in vs]
        x0, x1 = max(0, int(round(min(xs)))), min(target_w, pw, int(round(max(xs))))
        y0, y1 = max(0, int(round(min(ys)))), min(target_h, ph, int(round(max(ys))))
        tex = texture(f, ppp)
        print(f'draw n={count} stride={stride} rect=({x0},{y0})-({x1},{y1}) tex=' +
              (tex if isinstance(tex, str) else str(tex and {k: (hex(v) if isinstance(v, int) else v)
                                                         for k, v in tex.items() if k != 'tsw'})))
        if x0 >= x1 or y0 >= y1:
            continue
        prog = program(f, ppp)
        if tex:
            is_blur = prog in BLUR_PROGRAMS
            blurred_tex = None
            if is_blur and tex['w'] > 0 and tex['h'] > 0:
                raw_pixels = []
                for ty in range(tex['h']):
                    for tx in range(tex['w']):
                        raw_pixels.append(texel(f, tex, tx, ty))
                blurred_tex = gaussian_blur_texture(raw_pixels, tex['w'], tex['h'])

            # stride 24: float u,v (normalised) at +16; stride 28: BGRA tint at +16,
            # then u,v in texels at +20.
            uv_off = 4 if stride == 28 else 0
            uvs = ([struct.unpack_from('<2f', v[2], uv_off) for v in vs] if stride >= 24
                   else [(0, 0), (1, 1)])
            u0, u1 = min(u for u, _ in uvs), max(u for u, _ in uvs)
            v0, v1 = min(v for _, v in uvs), max(v for _, v in uvs)
            su, sv = (1, 1) if max(u1, v1) > 1.0001 else (tex['w'], tex['h'])
            tint = struct.unpack_from('<I', vs[0][2], 0)[0] if stride == 28 else 0xffffffff
            mask = prog == DEST_IN

            sub_w = (u1 - u0) * su
            sub_h = (v1 - v0) * sv
            is_scaled = abs((x1 - x0) - sub_w) > 0.1 or abs((y1 - y0) - sub_h) > 0.1
            tex_cached_rows = {}
            def get_tex_row(ry):
                if ry not in tex_cached_rows:
                    tex_cached_rows[ry] = [texel(f, tex, rx, ry) for rx in range(tex['w'])]
                return tex_cached_rows[ry]

            for y in range(y0, y1):
                fv = (v0 + (v1 - v0) * (y - y0 + 0.5) / (y1 - y0)) * sv
                if is_scaled and not blurred_tex:
                    v_sample = max(0.0, min(float(tex['h'] - 1), fv - 0.5))
                    ty0 = int(v_sample)
                    ty1 = min(tex['h'] - 1, ty0 + 1)
                    fy = v_sample - ty0
                    row0 = get_tex_row(ty0)
                    row1 = get_tex_row(ty1) if ty1 != ty0 else row0
                else:
                    ty = min(tex['h'] - 1, int(fv))
                    row0 = get_tex_row(ty)

                for x in range(x0, x1):
                    fu = (u0 + (u1 - u0) * (x - x0 + 0.5) / (x1 - x0)) * su
                    if blurred_tex:
                        b, g, r, a = sample_bilinear(blurred_tex, tex['w'], tex['h'], fu, fv)
                    elif is_scaled:
                        u_sample = max(0.0, min(float(tex['w'] - 1), fu - 0.5))
                        tx0 = int(u_sample)
                        tx1 = min(tex['w'] - 1, tx0 + 1)
                        fx = u_sample - tx0
                        c00 = row0[tx0]
                        c10 = row0[tx1]
                        c01 = row1[tx0]
                        c11 = row1[tx1]
                        b, g, r, a = (
                            max(0, min(255, int(round(
                                (c00[c] * (1.0 - fx) + c10[c] * fx) * (1.0 - fy) +
                                (c01[c] * (1.0 - fx) + c11[c] * fx) * fy
                            )))) for c in range(4)
                        )
                    else:
                        tx = min(tex['w'] - 1, int(fu))
                        b, g, r, a = row0[tx]
                    if tint != 0xffffffff:
                        tb, tg, tr, ta = tint & 0xff, (tint >> 8) & 0xff, (tint >> 16) & 0xff, tint >> 24
                        b, g, r, a = b * tb // 255, g * tg // 255, r * tr // 255, a * ta // 255
                    d = (y * target_w + x) * 4
                    if mask:
                        fb[d:d + 4] = bytes(c * a // 255 for c in fb[d:d + 4])
                        continue
                    if tex.get('vibrancy'):
                        fb[d:d + 4] = bytes(blend_vibrancy(tuple(fb[d:d + 4]), (r, g, b, a)))
                    else:
                        for c, src in enumerate((r, g, b, a)):
                            fb[d + c] = min(255, src + fb[d + c] * (255 - a) // 255)
        elif stride == 20:
            colour = struct.unpack_from('<I', vs[0][2], 0)[0]
            r, g, b, a = (colour >> 16) & 0xff, (colour >> 8) & 0xff, colour & 0xff, colour >> 24
            for y in range(y0, y1):
                for x in range(x0, x1):
                    d = (y * target_w + x) * 4
                    for c, src in enumerate((r, g, b, a)):
                        fb[d + c] = min(255, src + fb[d + c] * (255 - a) // 255)
    png(sys.argv[2], target_w, target_h, bytes(fb))


if __name__ == '__main__':
    main()
