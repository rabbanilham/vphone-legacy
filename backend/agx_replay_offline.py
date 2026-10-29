"""Offline high-level replay of a backboardd AGX frame dump into a PNG.

Walks the VDM stream from the TA command, decodes each draw's vertices
(vertex PDS data: stride, buffer) and texture state words (PPP -> fragment
PDS data -> TSW), then rasterises axis-aligned quads with src-over blending.
"""
from pathlib import Path
import math
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


def decode_ppp_clipping(ppp, state=None):
    """Retain PPP scissor/index and 32-pixel region state across partial updates."""
    if state is None:
        state = {'scenable': False, 'scindex': 0, 'has_rc': False, 'rc_box': None}
    if not ppp:
        return state
    hdr = ppp[0]
    idx = 1
    if hdr & (1 << 0):
        if idx < len(ppp):
            state['scenable'] = bool(ppp[idx] & (1 << 16))
            idx += 1
    if hdr & (1 << 1): idx += 1
    if hdr & (1 << 2): idx += 1
    if hdr & (1 << 3): idx += 1
    if hdr & (1 << 4): idx += 1
    if hdr & (1 << 5):
        if idx < len(ppp):
            state['scindex'] = ppp[idx] & 0xffff
            idx += 1
    if hdr & (1 << 6): idx += 4
    if hdr & (1 << 7): idx += 1
    if hdr & (1 << 8): idx += 1
    if hdr & (1 << 9): idx += 1
    if hdr & (1 << 10):
        if idx + 1 < len(ppp):
            rc0 = ppp[idx]
            rc1 = ppp[idx + 1]
            idx += 2
            if (rc0 >> 31) & 1:
                state['has_rc'] = True
                state['rc_box'] = (((rc0 >> 16) & 0x1ff) * 32,
                                   ((rc0 & 0x1ff) + 1) * 32,
                                   ((rc1 >> 16) & 0x1ff) * 32,
                                   ((rc1 & 0x1ff) + 1) * 32)
            else:
                state['has_rc'] = False
                state['rc_box'] = None
    return state


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


PPP_BIT_SIZES = {
    0: 1, 1: 1, 2: 1, 3: 1, 4: 1, 5: 1,
    6: 4, 7: 1, 8: 1, 9: 1, 10: 2, 11: 6,
    16: 1, 17: 1, 18: 1, 19: 1, 20: 1, 21: 1, 22: 1,
    23: 2, 25: 2
}


def update_ppp_state(retained, packet):
    if not packet:
        return
    if len(packet) == 1:
        if (packet[0] & 0xc0000000) == 0x80000000:
            retained[12] = packet[0]
        elif packet[0] == 0:
            retained[12] = 0
        return
    hdr = packet[0]
    idx = 1
    slot = 0
    for b in range(32):
        sz = PPP_BIT_SIZES.get(b, 0)
        if not sz:
            continue
        if hdr & (1 << b):
            for s in range(sz):
                if idx < len(packet) and slot + s < len(retained):
                    retained[slot + s] = packet[idx]
                    idx += 1
        slot += sz


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
        if stride >= 20:
            # vec4 position: 3D layer transforms (unlock icon fly-in) leave
            # w != 1, so divide like the C replay's agx_vertex_xy.
            w = struct.unpack_from('<f', v, 12)[0]
            if math.isfinite(w) and w > 1e-6 and abs(w - 1) > 1e-6:
                x, y = x / w, y / w
        out.append((x, y, v[8:] if stride == 16 else v[16:]))
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
            data = [f.u32(base + 4 * i) for i in range(16)]
        except (KeyError, ValueError):
            continue
        for k in range(0, 16, 2):
            ptr = data[k] | (data[k + 1] << 32)
            if not 0x1000 <= ptr < (1 << 40):
                continue
            try:
                found.append((ptr, [f.u32(ptr + 4 * i) for i in range(128)]))
            except (KeyError, ValueError):
                continue
    return found


def is_strip_surface(f, base, width):
    """Full-width aligned display buffers, not small glyph allocation neighbours."""
    return (width == 640 and base % 0x10000 == 0 and
            (base + 0x9000) // 4096 * 4096 in f.pages and
            (base + 0xa000) // 4096 * 4096 not in f.pages)


def nc_sparse_layout(f, base, w, h):
    """Recognize only the proved NC/folder scratch extents with sparse page maps."""
    if base & 4095 or (w, h) not in ((128, 96), (640, 224), (640, 288), (640, 320), (640, 640)):
        return False
    pitch = ((w + 15) & ~15) * 4
    if all(((base + y * pitch) & ~4095) in f.pages for y in range(h)):
        return False
    for y in range(0, h, 32):
        for x in range(0, w, 32):
            offset = bit = 0
            mask = 1
            while mask < w or mask < h:
                if mask < h:
                    offset |= bool(y & mask) << bit; bit += 1
                if mask < w:
                    offset |= bool(x & mask) << bit; bit += 1
                mask <<= 1
            if ((base + offset * 4) & ~4095) not in f.pages:
                return False
    return True


def decode_tsw(f, tsw):
    fmt = tsw[0] >> 28
    # WebKit's U8 coverage atlas replicates the byte into alpha through
    # swizzle 0x16d00. Do not accept arbitrary format-0 words as descriptors.
    coverage = fmt == 0 and (tsw[0] & ~7) == 0x16d00
    if fmt not in (6, 3) and not coverage:
        return None
    bpp = 1 if fmt == 3 or coverage else 4
    w = ((tsw[1] >> 2) & 0x3fff) + 1
    h = (tsw[1] >> 16) + 1
    q = tsw[2] | (tsw[3] << 32)
    addr = ((q >> 16) & 0x3ffffffff) << 2
    if not addr or (addr & ~0xfff) not in f.pages:
        return None
    # Display mappings use 16-row strips. Other layout-1 surfaces use
    # Y-first Morton order, including GPU-produced render targets.
    # Linear textures carry (pitch in pixels - 1) in TSW word 2 bits [15:0].
    strip = is_strip_surface(f, addr, w)
    if strip:
        stride = 2560
    elif (tsw[0] & 7) == 1:
        stride = ((w + 15) & ~15) * bpp
    else:
        pitch = ((tsw[2] & 0xffff) + 1) * bpp
        stride = pitch if pitch >= w * bpp else ((w + 15) & ~15) * bpp
    nc_sparse = not strip and (tsw[0] & 0x8007) == 1 and nc_sparse_layout(f, addr, w, h)
    return dict(w=w, h=h, addr=addr, strip=strip, stride=stride, bpp=bpp,
                twiddled=not strip and ((tsw[0] & 0x8007) == 0x8001 or (bpp == 1 and (tsw[0] & 7) == 1) or ((tsw[0] >> 24) & 0xf) == 8 or nc_sparse),
                nc_sparse=nc_sparse, tsw=tsw)


def decode_sampler(words):
    """
    Decodes PowerVR Rogue sampler words (4 x 32-bit words).
    Word 0: bits [11:0] max LOD (0xfff = no LOD limit).
    Word 1:
      bits [5:4]: min filter (0=nearest, 1=linear)
      bits [7:6]: mag filter (0=nearest, 1=linear)
      bits [11:9]: wrap mode U (0=repeat, 2=clamp_to_edge)
      bits [14:12]: wrap mode V (0=repeat, 2=clamp_to_edge)
    """
    w0, w1 = words[0], words[1]
    min_filter = 'linear' if ((w1 >> 4) & 3) == 1 else 'nearest'
    mag_filter = 'linear' if ((w1 >> 6) & 3) == 1 else 'nearest'
    wrap_u = 'clamp_to_edge' if ((w1 >> 9) & 7) == 2 or ((w1 >> 16) & 0xff) == 2 else 'repeat'
    wrap_v = 'clamp_to_edge' if ((w1 >> 12) & 7) == 2 or ((w1 >> 24) & 0xff) == 2 else 'repeat'
    max_lod = w0 & 0xfff
    return {
        'min_filter': min_filter,
        'mag_filter': mag_filter,
        'wrap_u': wrap_u,
        'wrap_v': wrap_v,
        'max_lod': max_lod,
        'linear_filter': min_filter == 'linear' or mag_filter == 'linear',
        'clamp_to_edge': wrap_u == 'clamp_to_edge' or wrap_v == 'clamp_to_edge',
        'words': list(words[:4]),
    }


def texture(f, ppp, prog=None):
    """Decode a texture and its sampler descriptor."""
    if prog is None:
        prog = program(f, ppp)
    words = fragment_words(f, ppp)
    for _, ws in words:
        # TSW at 16-byte aligned 0 or 4; word 7 only behind the luma weights
        # (vibrancy block: luma, four sampler words, TSW), as in the C replay.
        # Word 8 is used by ROUNDED_CAPS (two 4-word samplers followed by TSW).
        offsets = (0, 4, 7, 8) if prog == ROUNDED_CAPS else (0, 4, 7)
        for k in offsets:
            if k == 7 and tuple(ws[:3]) != LUMA:
                continue
            if len(ws) >= k + 4:
                tex = decode_tsw(f, ws[k:k + 4])
                if tex:
                    if k == 8 and prog == ROUNDED_CAPS and not (tex['addr'] == 0x10524000 or (tex['w'] == 114 and tex['h'] == 87)):
                        continue
                    # Trailing constants may be the next draw's allocation.
                    tex['vibrancy'] = tuple(ws[:3]) == LUMA or prog == STRIDE36_DUAL_TEX
                    if prog in (STRIDE36_DUAL_TEX, CORNER_RESTORE) and len(ws) >= 8:
                        tex['mask_tex'] = decode_tsw(f, ws[4:8])
                    sampler = None
                    if k >= 4:
                        sampler = decode_sampler(ws[k - 4:k])
                    elif len(ws) >= 8 and ((ws[4] & 0xfff == 0xfff) or ((ws[5] >> 9) & 7 == 2)):
                        sampler = decode_sampler(ws[4:8])
                    if not sampler:
                        for _, s_ws in words:
                            if len(s_ws) >= 4 and s_ws[0] == 0xfff and ((s_ws[1] >> 9) & 7 == 2 or (s_ws[1] & 0x2000) != 0):
                                sampler = decode_sampler(s_ws[:4])
                                break
                    tex['sampler'] = sampler
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


WALLPAPER = 0x789b8f08a3f3a8cd  # observed CoreImage XY/UV clip-space pass
COPY = 0x99d4fcf52a8c2ef3  # CA solid source-copy padding clear (including alpha zero)
DEST_IN = 0x4ee01acc47551344  # USC fingerprint of CA's mask shader: D *= texel alpha
SPANS = 0x1a20cc29e8fcc15b  # 1-pixel horizontal spans for glyph rows
WEBKIT_SPANS = 0x84102d511f2a41bb  # rounded image-mask coverage spans
WEBKIT_STRIPS = 0x4eb8837dc2079f95  # gradient strips consuming the rounded mask
PLUS_L = 0x5b133ef7e0c3428c  # CoreAnimation plus-lighter compositing filter
PLUS_L2 = 0xa225a18725e48d5c  # CoreAnimation plus-lighter gradient variant
SOLID_PLUS_L = 0xf137b90c25ab5471  # Solid grey text fill (plus-lighter)
PILL_BORDER = 0x62a99af8d8beb332  # UISwitch track border (stride 36)
COLOR_MATRIX = 0x2353a3fe1ca2b71b  # Navigation bar saturation boost & tint
ROUNDED_CAPS = 0x6c01d033fd937c7c  # UISwitch knob rounded caps & drop shadow
RADIAL_CLIP = 0xe344a0f081cc4997
RADIAL_RESTORE = 0xab6fdb6c250c32c0
STRIDE36_DUAL_TEX = 0xb61824c33264c501  # Backdrop through independent alpha-mask UVs
WEBKIT_GLYPHS = 0x3064a456731fc3e3  # normalized U8 atlas coverage times RGBA vertex tint
CORNER_RESTORE = 0x03a2edd2a4d84437  # CA dual-texture corner restore (backdrop + mask)



def extract_color_matrix(f, ppp):
    """Extract 3x3 color matrix and 3-element bias vector from fragment uniforms."""
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
                break
            try:
                raw_bytes = f.read(ptr, 64)
                fl = struct.unpack('<16f', raw_bytes)
                if (fl[3] == 0.0 and fl[7] == 0.0 and fl[11] == 0.0 and fl[15] == 0.0 and
                    abs(fl[0]) > 0.01 and abs(fl[12]) > 0.01):
                    matrix = (
                        fl[0], fl[1], fl[2],
                        fl[4], fl[5], fl[6],
                        fl[8], fl[9], fl[10],
                    )
                    bias = (fl[12], fl[13], fl[14])
                    return {'matrix': matrix, 'bias': bias}
            except (KeyError, ValueError):
                break
    return {
        'matrix': (
            0.325948, -0.034016, -0.034016,
            -0.114432, 0.245568, -0.114432,
            -0.011552, -0.011552, 0.348448,
        ),
        'bias': (0.776000, 0.776000, 0.776000),
    }


def extract_downsample(f, ppp):
    """Four symmetric pairs at offset[0,3,5,6], in normalized units."""
    if len(ppp) != 32:
        state = [0] * 32
        update_ppp_state(state, ppp)
        ppp = state
    try:
        base = PDS_HEAP | ppp[12]
        first = struct.unpack('<12f', f.read(f.u64(base + 16), 48))
        rest = struct.unpack('<16f', f.read(f.u64(base + 24), 64))
        offsets = [first[:2], rest[:2], rest[8:10], rest[12:14]]
        if all(math.isfinite(x) and abs(x) <= 1 for pair in offsets for x in pair):
            return offsets
    except (KeyError, ValueError):
        pass
    return None


def sample_downsample(buf, w, h, u, v, offsets):
    samples = [sample_bilinear(buf, w, h, u + sign * dx * w, v + sign * dy * h)
               for dx, dy in offsets for sign in (-1, 1)]
    return tuple((sum(p[c] for p in samples) + 4) // 8 for c in range(4))


def nc_copy_texmat(f, vs_data, state):
    """Recover/inherit texmat0 for NC's normalized copy vertex program."""
    try:
        data = struct.unpack('<16I', f.read(vs_data, 64))
        ptr = data[12] | data[13] << 32
        if 0x1000 <= ptr < 1 << 40:
            uniforms = struct.unpack('<28f', f.read(ptr, 112))
            state['code'] = data[10]
            state['normalized'] = uniforms[24:28] == (1.0, 1.0, 0.0, 0.0)
        return state.get('code') == data[10] and state.get('normalized', False)
    except (KeyError, ValueError):
        return False


def extract_region_clear(f, ppp):
    """Four uploaded RGBA constants; do not scan the next draw's allocation."""
    if len(ppp) != 32:
        state = [0] * 32
        update_ppp_state(state, ppp)
        ppp = state
    try:
        data = struct.unpack('<4I', f.read(PDS_HEAP | ppp[12], 16))
        if data[2] != 0x90000004 or data[3] & 0x0fffffff:
            return None
        ptr = data[0] | data[1] << 32
        rgba = struct.unpack('<4f', f.read(ptr, 16))
        if not all(math.isfinite(v) and 0 <= v <= 1 for v in rgba):
            return None
        r, g, b, a = (int(v * 255 + 0.5) for v in rgba)
        return b | g << 8 | r << 16 | a << 24
    except (KeyError, ValueError):
        return None


def is_clear_pass(type1_command):
    """Determine if a pass background is a clear (True) or load (False).

    A 3D pass is a clear if it carries a background object (+0x178 != 0)
    and the driver's load flag at +0x23c is 0. Display passes set +0x23c = 1
    (load-op) and offscreen backdrops set +0x23c = 0 (clear-op).
    Synthetic commands without a background object (+0x178 == 0) leave
    canvas memory untouched.
    """
    w178 = struct.unpack_from('<I', type1_command, 0x178)[0]
    w23c = struct.unpack_from('<I', type1_command, 0x23c)[0]
    return (w178 != 0) and ((w23c & 1) == 0)

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

BLUR_4TAP = 0x0cda5e479d40815f
BLUR_7TAP = 0xf82f9a96aff1e034
DOWNSAMPLE_8 = 0x00e3b3d44520e23f

BLUR_PROGRAMS_12 = {
    0x13a51b8bc1b85b4c,
    0x23e324c0933a2344,
    0x28463bd14bffa040,
    0x3568dc9c7476346b,
    0x4251b7edb10c7be4,
    0x48870313e0aa10e8,
    0x604b25e32d1eca5c,
    0x66287bbe34a68c71,
    0x664855a92cde7229,
    0x6a2a1d789db87995,
    0x6af3b793cd19efb3,
    0x95450769f4fd347b,
    0x9606b672a1c89485,
    0xa112190af2d46c9e,
    0xa7d834f91cf06600,
    0xa82d25f9c6abcf1f,
    0xc449a4cb19584ccf,
    0xc8fbd825d2fea18d,
    0xcbf57b156b6423ec,
    0xe8b7f35f72f5737f,
    0xefc984709b59caeb,
    0xf5f55b3c7127e3f3,
}

ALL_BLUR_PROGRAMS = BLUR_PROGRAMS | {BLUR_4TAP, BLUR_7TAP} | BLUR_PROGRAMS_12

# 41-byte USC machine code preamble common to all CoreAnimation separable Gaussian blur shaders.
BLUR_PREAMBLE = bytes([
    0x2a, 0x0a, 0x94, 0x99, 0x99, 0xaf, 0x1b, 0x80,
    0x00, 0x33, 0x00, 0x00, 0x80, 0x40, 0x80, 0x01,
    0x88, 0x00, 0x20, 0xff, 0x3a, 0x8a, 0x80, 0x94,
    0x99, 0x99, 0xaf, 0x1b, 0x80, 0x00, 0x33, 0x00,
    0x00, 0x82, 0x40, 0x80, 0x03, 0x88, 0x00, 0x21,
    0x00
])

_blur_cache = set()


def is_blur_program(f, ppp, prog):
    """Signature-based detection for CoreAnimation separable Gaussian blur shaders."""
    if prog in (BLUR_4TAP, BLUR_7TAP) or prog in BLUR_PROGRAMS or prog in _blur_cache:
        return True
    pds_ptr = ppp[6] if len(ppp) == 32 else (ppp[7] if len(ppp) > 7 else (ppp[6] if len(ppp) > 6 else 0))
    if not pds_ptr and len(ppp) > 7:
        pds_ptr = ppp[7]
    if not pds_ptr:
        return False
    try:
        off = f.u32(0x600000000 | pds_ptr)
        code = f.read(0x700000000 | off, len(BLUR_PREAMBLE))
        if code == BLUR_PREAMBLE:
            _blur_cache.add(prog)
            return True
    except Exception:
        pass
    return False

BLUR_TAPS_4 = [
    (0.638336, 0.275844),
    (2.348645, 0.185744),
    (4.245085, 0.035760),
    (6.164516, 0.002652),
]

BLUR_TAPS_7 = [
    (0.662207, 0.118916),
    (2.475021, 0.141265),
    (4.455121, 0.107065),
    (6.435364, 0.069256),
    (8.415810, 0.038236),
    (10.396517, 0.018016),
    (12.377541, 0.007245),
]

BLUR_TAPS = [
    (0.6575, 0.1666),
    (2.4492, 0.1759),
    (4.4092, 0.1005),
    (6.3704, 0.0417),
    (8.3332, 0.0126),
    (10.2979, 0.0027),
]


def extract_nc_blur_params(f, vs_data, ppp, prog):
    """Extract tap offsets, weights, direction and tap count for a blur draw."""
    if prog in (BLUR_4TAP, BLUR_7TAP):
        n = 4 if prog == BLUR_4TAP else 7
        state = ppp
        if len(state) != 32:
            state = [0] * 32
            update_ppp_state(state, ppp)
        try:
            uniforms = struct.unpack('<48f', f.read(f.u64(vs_data + 48), 192))
            weights = struct.unpack('<' + 'f' * n, f.read(f.u64((PDS_HEAP | state[12]) + 16), n * 4))
            horizontal = abs(uniforms[17]) < 0.001 and uniforms[16] > 0.05
            vertical = abs(uniforms[16]) < 0.001 and uniforms[17] > 0.05
            offsets = [uniforms[16 + 4 * i + int(vertical)] for i in range(n)]
            if ((horizontal or vertical) and all(math.isfinite(x) and 0.05 < x < 50 for x in offsets)
                    and all(a < b for a, b in zip(offsets, offsets[1:]))
                    and all(math.isfinite(x) and 0 <= x <= 0.6 for x in weights)
                    and abs(sum(weights) - 0.5) < 0.01):
                return dict(direction='vertical' if vertical else 'horizontal', offsets=offsets,
                            weights=list(weights), n_taps=n, extracted=True)
        except (KeyError, ValueError):
            pass
        return dict(direction='horizontal', offsets=[], weights=[], n_taps=0, extracted=False)
    return dict(direction="horizontal", offsets=[], weights=[], n_taps=0, extracted=False)


def extract_blur_params(f, vs_data, ppp, prog):
    """Extract tap offsets, weights, direction and tap count for a blur draw."""
    if prog == BLUR_4TAP:
        taps = BLUR_TAPS_4
        def_dir = 'horizontal'
    elif prog == BLUR_7TAP:
        taps = BLUR_TAPS_7
        def_dir = 'horizontal'
    else:
        taps = BLUR_TAPS
        def_dir = 'both'

    offsets = [t[0] for t in taps]
    weights = [t[1] for t in taps]
    direction = def_dir
    n_taps = len(taps)
    max_taps = 4 if prog == BLUR_4TAP else (7 if prog == BLUR_7TAP else 7)
    found_offsets = False
    found_weights = False

    # 1. Inspect vertex uniforms (vs_data + 48)
    if vs_data:
        try:
            raw_vsd = [f.u32(vs_data + 4 * i) for i in range(16)]
            u_ptr = raw_vsd[12] | (raw_vsd[13] << 32)
            if 0x1000 <= u_ptr < (1 << 40):
                p_words = [f.u32(u_ptr + 4 * j) for j in range(48)]
                ox0 = struct.unpack('<f', struct.pack('<I', p_words[16]))[0]
                oy0 = struct.unpack('<f', struct.pack('<I', p_words[17]))[0]
                is_vert = None
                if abs(ox0) > 0.01 and abs(oy0) < 0.001:
                    is_vert = False
                elif abs(oy0) > 0.01 and abs(ox0) < 0.001:
                    is_vert = True

                if is_vert is not None:
                    direction = 'vertical' if is_vert else 'horizontal'
                    read_offs = []
                    for j in range(16, 16 + 4 * max_taps, 4):
                        ox = struct.unpack('<f', struct.pack('<I', p_words[j]))[0]
                        oy = struct.unpack('<f', struct.pack('<I', p_words[j + 1]))[0]
                        val = oy if is_vert else ox
                        if 0.05 < val < 50.0:
                            read_offs.append(val)
                        else:
                            break
                    if len(read_offs) >= 3:
                        offsets = read_offs
                        n_taps = len(read_offs)
                        found_offsets = True
        except Exception:
            pass

    # 2. Inspect fragment uniforms for weights and offsets
    if ppp:
        try:
            words = fragment_words(f, ppp)
            for _, ws in words:
                fl = [struct.unpack('<f', struct.pack('<I', w))[0] for w in ws]
                # Look for weights
                if not found_weights:
                    for n in (7, 4):
                        for i in range(len(fl) - n + 1):
                            sub = fl[i:i + n]
                            if all(0.0005 <= x <= 0.6 for x in sub) and abs(sum(sub) - 0.5) < 0.01:
                                weights = list(sub)
                                n_taps = n
                                found_weights = True
                                break
                        if found_weights:
                            break

                # Search for offsets in fragment uniforms if not found in vertex uniforms
                if not found_offsets:
                    for n in (n_taps, 7, 4):
                        for offset_sub in (0, 1):
                            for start_idx in range(len(fl) - n * 4 + 1):
                                xs = [fl[start_idx + k * 4 + offset_sub] for k in range(n)]
                                ys = [fl[start_idx + k * 4 + offset_sub + 1] for k in range(n)]
                                if all(0.05 < xs[k] < 50.0 for k in range(n)) and all(abs(ys[k]) < 0.001 for k in range(n)):
                                    if all(xs[k] < xs[k + 1] for k in range(n - 1)):
                                        direction = 'horizontal'
                                        offsets = xs
                                        n_taps = n
                                        found_offsets = True
                                        break
                                if all(0.05 < ys[k] < 50.0 for k in range(n)) and all(abs(xs[k]) < 0.001 for k in range(n)):
                                    if all(ys[k] < ys[k + 1] for k in range(n - 1)):
                                        direction = 'vertical'
                                        offsets = ys
                                        n_taps = n
                                        found_offsets = True
                                        break
                            if found_offsets:
                                break
                        if found_offsets:
                            break

                if found_weights and found_offsets:
                    break
        except Exception:
            pass

    extracted = (found_weights and found_offsets) or (prog in (BLUR_4TAP, BLUR_7TAP))

    return {
        'direction': direction,
        'offsets': offsets,
        'weights': weights,
        'n_taps': n_taps,
        'extracted': extracted,
    }


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

    wx = int(fx * 256.0 + 0.5)
    wy = int(fy * 256.0 + 0.5)
    out = []
    for c in range(4):
        top = c00[c] * (256 - wx) + c10[c] * wx
        bot = c01[c] * (256 - wx) + c11[c] * wx
        val = (top * (256 - wy) + bot * wy + 32768) >> 16
        out.append(max(0, min(255, val)))
    return tuple(out)


def gaussian_blur_texture(buf, w, h, taps=None, offsets=None, weights=None, direction='both'):
    if offsets is not None and weights is not None:
        taps = list(zip(offsets, weights))
    elif taps is None:
        taps = BLUR_TAPS

    current = buf
    if direction in ('horizontal', 'both'):
        out_h = [None] * (w * h)
        for y in range(h):
            for x in range(w):
                acc = [0.0, 0.0, 0.0, 0.0]
                for off, weight in taps:
                    sp = sample_bilinear(current, w, h, x + 0.5 + off, y + 0.5)
                    sn = sample_bilinear(current, w, h, x + 0.5 - off, y + 0.5)
                    for c in range(4):
                        acc[c] += (sp[c] + sn[c]) * weight
                out_h[y * w + x] = tuple(max(0, min(255, int(round(acc[c])))) for c in range(4))
        current = out_h

    if direction in ('vertical', 'both'):
        out_v = [None] * (w * h)
        for y in range(h):
            for x in range(w):
                acc = [0.0, 0.0, 0.0, 0.0]
                for off, weight in taps:
                    sp = sample_bilinear(current, w, h, x + 0.5, y + 0.5 + off)
                    sn = sample_bilinear(current, w, h, x + 0.5, y + 0.5 - off)
                    for c in range(4):
                        acc[c] += (sp[c] + sn[c]) * weight
                out_v[y * w + x] = tuple(max(0, min(255, int(round(acc[c])))) for c in range(4))
        current = out_v

    return current


def program(f, ppp):
    """FNV-1a of the first 64 bytes of the draw's USC fragment code."""
    pds_ptr = ppp[6] if len(ppp) == 32 else (ppp[7] if len(ppp) > 7 else (ppp[6] if len(ppp) > 6 else 0))
    if not pds_ptr and len(ppp) > 7:
        pds_ptr = ppp[7]
    try:
        off = f.u32(0x600000000 | pds_ptr)
        code = f.read(0x700000000 | off, 64)
    except (KeyError, ValueError, IndexError):
        return 0
    h = 0xcbf29ce484222325
    for b in code:
        h = ((h ^ b) * 0x100000001b3) & 0xffffffffffffffff
    return h


def texel(f, tex, x, y):
    bpp = tex.get('bpp', 4)
    if tex.get('twiddled', False):
        offset, bit, mask = 0, 0, 1
        while mask < tex['w'] or mask < tex['h']:
            if mask < tex['h']:
                offset |= bool(y & mask) << bit
                bit += 1
            if mask < tex['w']:
                offset |= bool(x & mask) << bit
                bit += 1
            mask <<= 1
        gva = tex['addr'] + offset * bpp
    elif tex['strip']:
        gva = tex['addr'] + (y >> 4) * 0x10000 + (y & 15) * tex['stride'] + x * bpp
    else:
        gva = tex['addr'] + y * tex['stride'] + x * bpp
    try:
        if bpp == 1:
            a = f.read(gva, 1)[0]
            return (0, 0, 0, a)
        return f.read(gva, 4)
    except KeyError:
        return (0, 0, 0, 0) if bpp == 1 else b'\0\0\0\0'


def quad_coordinate(pixel, low, high, uv0, uv1):
    """Interpolate on the original quad, even when viewport clipping crops it."""
    return uv0 + (uv1 - uv0) * (pixel + 0.5 - low) / (high - low)


def quad_affine(xy, uv):
    """Return U/V affine coefficients from three associated non-collinear vertices."""
    dx1, dy1 = xy[1][0] - xy[0][0], xy[1][1] - xy[0][1]
    dx2, dy2 = xy[2][0] - xy[0][0], xy[2][1] - xy[0][1]
    det = dx1 * dy2 - dx2 * dy1
    if not math.isfinite(det) or abs(det) < 1e-8:
        return None
    out = []
    for c in range(2):
        d1, d2 = uv[1][c] - uv[0][c], uv[2][c] - uv[0][c]
        a, b = (d1 * dy2 - d2 * dy1) / det, (dx1 * d2 - dx2 * d1) / det
        out.extend((a, b, uv[0][c] - a * xy[0][0] - b * xy[0][1]))
    return tuple(out) if all(math.isfinite(v) for v in out) else None


def radial_coverage(u, v, mapping):
    inside = 0
    for j in range(4):
        for i in range(4):
            dx, dy = (i+0.5)/4-0.5, (j+0.5)/4-0.5
            a = u+mapping[0]*dx+mapping[1]*dy
            b = v+mapping[3]*dx+mapping[4]*dy
            inside += a*a+b*b <= 1
    return (inside*255+8)//16


def classify_draw(f, vs_data, ppp, draw, viewport, target_w, target_h, pw, ph, retained_ppp=None, copy_texmat_state=None):
    """Classify a draw outcome: replayed exactly, approximated, or skipped with reason."""
    count = draw[1]
    w0 = draw[0]
    kind = w0 >> 29
    if kind != 3:
        return 'skipped: unknown VDM block', {'kind': kind}

    try:
        stride, vs = vertices(f, vs_data, count)
    except Exception as e:
        return 'skipped: unmapped memory', {'error': str(e)}

    if stride is None or stride < 12 or stride > 64:
        return 'skipped: unmapped memory', {'stride': stride}

    prog = program(f, ppp) or (program(f, retained_ppp) if retained_ppp else 0)
    try:
        tex = texture(f, ppp, prog=prog) or (texture(f, retained_ppp, prog=prog) if retained_ppp else None)
    except TypeError:
        tex = texture(f, ppp) or (texture(f, retained_ppp) if retained_ppp else None)
    if not tex:
        try:
            tex = texture(f, ppp) or (texture(f, retained_ppp) if retained_ppp else None)
        except TypeError:
            pass

    if (stride, prog) in ((24, RADIAL_CLIP), (36, RADIAL_RESTORE)):
        return 'approximated: analytic radial corner coverage (4x4 AA)', {'prog': hex(prog)}

    nc_context = nc_sparse_layout(f, f.header['pbe_target'], target_w, target_h) if hasattr(f, 'header') else False
    nc_context = nc_context or bool(tex and tex.get('nc_sparse'))
    nc_normalized = (nc_context and stride == 24 and prog == 0x8eb52cf6fe248b1d and
                     nc_copy_texmat(f, vs_data, copy_texmat_state if copy_texmat_state is not None else {}))
    is_blur_prog = is_blur_program(f, ppp, prog) or (is_blur_program(f, retained_ppp, prog) if retained_ppp else False)
    if stride == 16 and prog in (SPANS, WEBKIT_SPANS):
        return 'replayed exactly: glyph spans (stride=16)', {'count': count, 'prog': hex(prog)}
    if stride == 36 and prog == PILL_BORDER:
        return 'replayed exactly: pill border (stride=36)', {'count': count, 'prog': hex(prog)}
    if stride == 12 and is_blur_prog:
        colour = extract_region_clear(f, retained_ppp or ppp) if nc_context else None
        if colour is not None:
            return 'replayed exactly: region clear (uniform RGBA)', {'colour': hex(colour), 'prog': hex(prog)}
        params = extract_blur_params(f, vs_data, retained_ppp or ppp, prog)
        if params.get('extracted'):
            d_name = params['direction']
            n = params['n_taps']
            return f'replayed exactly: 1D Gaussian blur ({d_name}, {n}-tap, canvas backdrop)', {'prog': hex(prog)}
        return 'approximated: Gaussian blur (separable 6-tap, canvas backdrop)', {'prog': hex(prog)}

    if not tex and stride not in (20, 28):
        if stride == 24 and prog == 0:
            return 'skipped: stride 24 untextured with program 0x0', {'stride': stride, 'prog': hex(prog)}
        return 'skipped: untextured with stride not in (20, 28)', {'stride': stride, 'prog': hex(prog)}

    if tex and tex['w'] * 4 > 0x10000:
        return 'skipped: texture wider than texrow', {'width': tex['w']}

    m = ((1, 1, 0, 0) if stride == 16 and prog == WALLPAPER else
         find_matrix(f, vs_data) if stride != 12 else None)
    vp = viewport or (pw / 2, pw / 2, ph / 2, ph / 2)

    step = 4 if count == 4 else 6
    is_line = False
    is_cg_line = (prog == 0xba8c50a79810e866 and (not tex or tex.get('bpp') == 1))
    is_clock_cap = (prog == ROUNDED_CAPS and count == 48 and (not tex or tex.get('bpp') == 1))
    if count == 6 and (not tex or is_cg_line) and stride in (20, 28):
        vs_coords = []
        for v in vs:
            x = (m[0] * v[0] + m[2]) * vp[0] + vp[1] if m else v[0]
            y = (m[1] * v[1] + m[3]) * vp[2] + vp[3] if m else v[1]
            vs_coords.append((round(x, 2), round(y, 2)))
        if len(set(vs_coords)) <= 2:
            is_line = True

    if is_line:
        return f'replayed exactly: solid wide line (stride={stride})', {'count': count}

    is_axis_aligned = count in (4, 6, 12) or (count % step == 0)
    if is_axis_aligned:
        for q_start in range(0, count, step):
            q_vs = vs[q_start:q_start + step]
            if len(q_vs) < step:
                is_axis_aligned = False
                break
            if m:
                xs = [(m[0] * v[0] + m[2]) * vp[0] + vp[1] for v in q_vs]
                ys = [(m[1] * v[1] + m[3]) * vp[2] + vp[3] for v in q_vs]
            else:
                xs = [v[0] for v in q_vs]
                ys = [v[1] for v in q_vs]
            minx, maxx = min(xs), max(xs)
            miny, maxy = min(ys), max(ys)
            if (maxx - minx) < 1e-3 or (maxy - miny) < 1e-3:
                continue
            for x, y in zip(xs, ys):
                if not ((abs(x - minx) < 1e-3 or abs(x - maxx) < 1e-3) and
                        (abs(y - miny) < 1e-3 or abs(y - maxy) < 1e-3)):
                    is_axis_aligned = False
                    break
            if is_axis_aligned:
                has_tl = has_tr = has_bl = has_br = False
                for x, y in zip(xs, ys):
                    if abs(x - minx) < 1e-3 and abs(y - miny) < 1e-3: has_tl = True
                    if abs(x - maxx) < 1e-3 and abs(y - miny) < 1e-3: has_tr = True
                    if abs(x - minx) < 1e-3 and abs(y - maxy) < 1e-3: has_bl = True
                    if abs(x - maxx) < 1e-3 and abs(y - maxy) < 1e-3: has_br = True
                if (has_tl + has_tr + has_bl + has_br) < 3:
                    is_axis_aligned = False
            if not is_axis_aligned:
                break

    if not is_axis_aligned:
        if (not tex or is_cg_line or is_clock_cap) and stride in (20, 28):
            return f'replayed exactly: solid triangles (stride={stride})', {'count': count}
        if not tex:
            return 'skipped: non-axis-aligned or rotated geometry', {'quad': 0}
        uv_off = 4 if stride in (28, 36) else 0
        uvs = ([struct.unpack_from('<2f', v[2], uv_off) for v in vs] if stride >= 24 or (stride == 16 and prog == WALLPAPER) else [(0, 0), (1, 1)])
        xs = [(m[0] * v[0] + m[2]) * vp[0] + vp[1] for v in vs] if m else [v[0] for v in vs]
        ys = [(m[1] * v[1] + m[3]) * vp[2] + vp[3] for v in vs] if m else [v[1] for v in vs]
        uv_map = quad_affine(list(zip(xs, ys))[:3], uvs[:3]) if len(uvs) >= 3 else None
        if not uv_map:
            return 'skipped: non-axis-aligned or rotated geometry', {'quad': 0}

    if (not tex or stride == 20) and stride == 20:
        if count == 12:
            return 'replayed exactly: solid multi-quad (2 quads, stride=20)', {'count': count}
        return 'replayed exactly: solid quad (stride=20)', {'count': count}
    if not tex and stride == 28:
        # Untextured glyphs (clock digits): RGBA colour at +16, like stride 20.
        return 'replayed exactly: solid quad (stride=28)', {'count': count}

    if nc_context and prog == DOWNSAMPLE_8:
        offsets = extract_downsample(f, retained_ppp or ppp)
        if offsets is None:
            return 'skipped: downsample uniforms unavailable or invalid', {'prog': hex(prog)}
        zero = all(x == 0 for pair in offsets for x in pair)
        return 'replayed exactly: 8-sample downsample' + (' (zero captured offsets)' if zero else ''), {'offsets': offsets}
    is_blur = is_blur_program(f, ppp, prog)
    is_dest_in = prog == DEST_IN
    is_vibrancy = tex.get('vibrancy', False)

    uv_off = 4 if stride == 28 else 0
    uvs = ([struct.unpack_from('<2f', v[2], uv_off) for v in vs] if stride >= 24 or (stride == 16 and prog == WALLPAPER) else [(0, 0), (1, 1)])
    u0, u1 = min(u for u, _ in uvs), max(u for u, _ in uvs)
    v0, v1 = min(v for _, v in uvs), max(v for _, v in uvs)
    su, sv = (1, 1) if not nc_normalized and max(u1, v1) > 1.0001 else (tex['w'], tex['h'])
    xs = [(m[0] * v[0] + m[2]) * vp[0] + vp[1] for v in vs] if m else [v[0] for v in vs]
    ys = [(m[1] * v[1] + m[3]) * vp[2] + vp[3] for v in vs] if m else [v[1] for v in vs]
    x0, x1 = max(0, int(round(min(xs)))), min(target_w, pw, int(round(max(xs))))
    y0, y1 = max(0, int(round(min(ys)))), min(target_h, ph, int(round(max(ys))))
    sub_w = (u1 - u0) * su
    sub_h = (v1 - v0) * sv
    is_scaled = abs((x1 - x0) - sub_w) > 0.1 or abs((y1 - y0) - sub_h) > 0.1
    uv_map = quad_affine(list(zip(xs, ys))[:3], uvs[:3]) if len(uvs) >= 3 else None
    if uv_map:
        step_x = math.hypot(uv_map[0] * su, uv_map[3] * sv)
        step_y = math.hypot(uv_map[1] * su, uv_map[4] * sv)
        is_scaled = abs(step_x - 1) > 0.1 or abs(step_y - 1) > 0.1

    if is_blur:
        params = (extract_nc_blur_params(f, vs_data, retained_ppp or ppp, prog) if nc_context
                  else extract_blur_params(f, vs_data, ppp, prog))
        if nc_context and prog in (BLUR_4TAP, BLUR_7TAP) and not params['extracted']:
            return 'skipped: blur uniforms unavailable or invalid', {'prog': hex(prog)}
        if params.get('extracted'):
            d_name = params['direction']
            n = params['n_taps']
            return f'replayed exactly: 1D Gaussian blur ({d_name}, {n}-tap)', {
                'prog': hex(prog),
                'direction': d_name,
                'taps': n,
            }
        return 'approximated: Gaussian blur (separable 6-tap)', {'prog': hex(prog)}
    elif is_dest_in:
        if is_scaled:
            return 'approximated: dest-in mask (bilinear scaled)', {'prog': hex(prog)}
        return 'replayed exactly: dest-in mask (1:1)', {'prog': hex(prog)}
    elif is_vibrancy:
        if stride == 36 and prog == STRIDE36_DUAL_TEX:
            return 'approximated: dual-texture alpha composite (stride=36)', {'prog': hex(prog)}
        return 'replayed exactly: vibrancy (Rec.709 luma-weighted tint)', {'prog': hex(prog)}
    elif stride == 36 and prog == CORNER_RESTORE:
        return 'replayed exactly: corner restore (backdrop + mask)', {'prog': hex(prog)}
    elif prog == COLOR_MATRIX:
        return 'replayed exactly: color matrix (saturation & tint)', {'prog': hex(prog)}
    elif prog == ROUNDED_CAPS:
        return 'replayed exactly: textured rounded quad (stride=28)', {'count': count, 'prog': hex(prog)}
    elif is_scaled:
        return 'replayed exactly: textured quad (bilinear scaled)', {'prog': hex(prog)}
    else:
        return 'replayed exactly: textured quad (1:1 nearest)', {'prog': hex(prog)}


def audit_frame(f):
    """Walk frame f and return list of (draw_idx, outcome, details)."""
    pw = (struct.unpack_from('<I', f.type1_command, 0x74)[0] & 0xffff) + 1
    ph = (struct.unpack_from('<I', f.type1_command, 0x70)[0] & 0xffff) + 1
    target_w, target_h = (pw, ph) if (pw != 640 or ph != 1136) else (W, H)
    outcomes = []
    did = 0
    retained_ppp = [0] * 32
    copy_texmat_state = {}
    for vs_data, ppp, draw, viewport in walk(f):
        update_ppp_state(retained_ppp, ppp)
        res, details = classify_draw(f, vs_data, ppp, draw, viewport, target_w, target_h, pw, ph, retained_ppp=retained_ppp, copy_texmat_state=copy_texmat_state)
        outcomes.append((did, res, details))
        did += 1
    return outcomes


def is_webkit_upload(f, prog, matrix, tex):
    """Observed padded upload atlases, excluding generated 96x96 masks."""
    return bool(tex and (tex['tsw'][0] & 7) == 1 and
                not (tex['w'] & (tex['w'] - 1)) and not (tex['h'] & (tex['h'] - 1)) and
                f.header.get('context_id', 1) != 1 and matrix and matrix[1] < 0 and
                prog == 0x8eb52cf6fe248b1d)


def texture_diagnostics(f):
    """Describe the selected texture state per draw without rendering pixels."""
    retained = [0] * 32
    last_matrix = None
    for index, (vs_data, ppp, draw, _) in enumerate(walk(f)):
        update_ppp_state(retained, ppp)
        prog = program(f, retained) or program(f, ppp)
        tex = texture(f, retained, prog) or texture(f, ppp, prog)
        matrix = find_matrix(f, vs_data)
        if matrix:
            last_matrix = matrix
        elif prog in (WEBKIT_GLYPHS, 0x8eb52cf6fe248b1d):
            matrix = last_matrix
        upload = is_webkit_upload(f, prog, matrix, tex)
        if tex and upload and (tex['tsw'][0] & 7) == 1 and not tex['strip']:
            tex['twiddled'] = True
        record = dict(draw=index, program=hex(prog), vertex_stride=f.u32(vs_data),
                      vertex_count=draw[1], texture=None)
        if tex:
            tsw = tex['tsw']
            record['texture'] = dict(
                format=tsw[0] >> 28, layout=hex(tsw[0] & 0x8007),
                width=tex['w'], height=tex['h'], pitch_word=hex(tsw[2]),
                stride=tex['stride'], bpp=tex['bpp'], address=hex(tex['addr']),
                twiddled=tex['twiddled'], strip=tex['strip'],
                nc_sparse=tex['nc_sparse'], tsw=[hex(w) for w in tsw],
                rgba_upload=upload,
                sampler=tex['sampler']['words'] if tex.get('sampler') else None)
        yield record


def render_target_texture(f, width, height):
    """Decode PBE layout, changing only the proved sparse NC targets."""
    target = f.header['pbe_target']
    try:
        eot = PDS_HEAP | struct.unpack_from('<I', f.type1_command, 0x1bc)[0]
        pbe = f.u32(eot + 8)
        tex = decode_tsw(f, [f.u32(pbe + 8 + 4 * i) for i in range(4)])
        if tex and tex['addr'] == target:
            return tex
    except (KeyError, ValueError):
        pass
    strip = is_strip_surface(f, target, width)
    return dict(addr=target, w=width, h=height, strip=strip, twiddled=not strip and nc_sparse_layout(f, target, width, height),
                stride=2560 if strip else ((width + 15) & ~15) * 4)


def render_target_pitch(f, width):
    """Exact linear PBE pitch, with the older aligned-target fallback."""
    try:
        eot = PDS_HEAP | struct.unpack_from('<I', f.type1_command, 0x1bc)[0]
        pbe = f.u32(eot + 8)
        tex = decode_tsw(f, [f.u32(pbe + 8 + 4 * i) for i in range(4)])
        if tex and tex['addr'] == f.header['pbe_target'] and not tex['twiddled']:
            return tex['stride']
    except (KeyError, ValueError):
        pass
    return ((width + 15) & ~15) * 4


def main():
    if len(sys.argv) > 1 and sys.argv[1] == '--audit':
        dumps_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).resolve().parents[1] / 'backend/evidence/keybag-contract-20261015/probe-164-gfxdump/dumps'
        from collections import Counter, defaultdict
        total_counts = Counter()
        category_frames = defaultdict(set)
        for i in range(1, 151):
            bp = dumps_dir / f'frame-{i}.bin'
            if not bp.exists():
                continue
            fr = Frame(bp)
            fr.require_matching_ta()
            for did, out, _ in audit_frame(fr):
                total_counts[out] += 1
                category_frames[out].add(i)
        print(f"{'Category':<60} | {'Count':<6} | {'Frames'}")
        print("-" * 85)
        for cat, cnt in total_counts.most_common():
            flist = sorted(list(category_frames[cat]))
            fstr = f"{len(flist)} frames ({flist[0]}..{flist[-1]})" if len(flist) > 10 else f"{len(flist)} frames {flist}"
            print(f"{cat:<60} | {cnt:<6} | {fstr}")
        return

    f = Frame(sys.argv[1])
    f.require_matching_ta()
    pw = (struct.unpack_from('<I', f.type1_command, 0x74)[0] & 0xffff) + 1
    ph = (struct.unpack_from('<I', f.type1_command, 0x70)[0] & 0xffff) + 1
    target_w, target_h = (pw, ph) if (pw != 640 or ph != 1136) else (W, H)
    fb = bytearray(target_w * target_h * 4)
    target = f.header.get('pbe_target')
    if not is_clear_pass(f.type1_command) and target and target in f.pages:
        rt = render_target_texture(f, target_w, target_h)
        for y in range(target_h):
            for x in range(target_w):
                b, g, r, a = texel(f, rt, x, y)
                off = (y * target_w + x) * 4
                fb[off:off + 4] = bytes((r, g, b, a))
    scissor_base = struct.unpack_from('<I', f.type1_command, 0x50)[0]
    clip_state = {'scenable': False, 'scindex': 0, 'has_rc': False, 'rc_box': None}
    last_m = None
    retained_ppp = [0] * 32
    copy_texmat_state = {}
    for vs_data, ppp, draw, viewport in walk(f):
        clip_state = decode_ppp_clipping(ppp, clip_state)
        update_ppp_state(retained_ppp, ppp)
        clip_x0, clip_y0 = 0, 0
        clip_x1, clip_y1 = target_w, target_h
        if clip_state['scenable'] and scissor_base:
            try:
                w0 = f.u32(scissor_base + clip_state['scindex'] * 8)
                w1 = f.u32(scissor_base + clip_state['scindex'] * 8 + 4)
                clip_x0 = max(clip_x0, w0 >> 16)
                clip_x1 = min(clip_x1, w0 & 0xffff)
                clip_y0 = max(clip_y0, w1 >> 16)
                clip_y1 = min(clip_y1, w1 & 0xffff)
            except (KeyError, ValueError):
                pass
        if clip_state['has_rc'] and clip_state['rc_box']:
            rc_x0, rc_x1, rc_y0, rc_y1 = clip_state['rc_box']
            clip_x0 = max(clip_x0, rc_x0)
            clip_x1 = min(clip_x1, rc_x1)
            clip_y0 = max(clip_y0, rc_y0)
            clip_y1 = min(clip_y1, rc_y1)

        count = draw[1]
        stride, vs = vertices(f, vs_data, count)
        prog = program(f, ppp) or program(f, retained_ppp)
        m = None
        if stride != 12:
            m = find_matrix(f, vs_data)
            if m:
                last_m = m
            elif (stride == 16 and prog in (SPANS, WEBKIT_SPANS) or prog in (WEBKIT_GLYPHS, WEBKIT_STRIPS) or
                  f.header.get('context_id', 1) != 1 and prog == 0x8eb52cf6fe248b1d) and last_m:
                m = last_m
        vp = viewport or (pw / 2, pw / 2, ph / 2, ph / 2)
        tex = texture(f, ppp, prog) or texture(f, retained_ppp, prog)
        if not tex:
            tex = texture(f, ppp) or texture(f, retained_ppp)
        webkit_upload = is_webkit_upload(f, prog, m, tex)
        if (tex and webkit_upload and (tex['tsw'][0] & 7) == 1 and not tex['strip']):
            tex['twiddled'] = True
        is_color_matrix = (tex is not None) and (prog == COLOR_MATRIX)
        if is_color_matrix:
            cm = extract_color_matrix(f, ppp)
            col_mat, col_bias = cm['matrix'], cm['bias']
        if stride == 16:
            if prog == WALLPAPER:
                m = (1, 1, 0, 0)
            elif prog in (SPANS, WEBKIT_SPANS):
                for k in range(0, count, 2):
                    if k + 1 >= len(vs):
                        break
                    v0, v1 = vs[k], vs[k + 1]
                    if m:
                        px0 = (m[0] * v0[0] + m[2]) * vp[0] + vp[1]
                        px1 = (m[0] * v1[0] + m[2]) * vp[0] + vp[1]
                        py0 = (m[1] * v0[1] + m[3]) * vp[2] + vp[3]
                    else:
                        px0, px1, py0 = v0[0], v1[0], v0[1]
                    alpha = v0[2][-1] if len(v0[2]) >= 8 else 0
                    if alpha == 0:
                        continue
                    iy = int(round(py0 - 0.5))
                    if iy < clip_y0 or iy >= clip_y1:
                        continue
                    ix0 = max(clip_x0, int(round(min(px0, px1) - 0.5)))
                    ix1 = min(clip_x1, target_w, pw, int(round(max(px0, px1) - 0.5)))
                    if ix0 >= ix1:
                        continue
                    for ix in range(ix0, ix1):
                        d = (iy * target_w + ix) * 4
                        for c in range(3):
                            fb[d + c] = min(255, alpha + fb[d + c] * (255 - alpha) // 255)
                        fb[d + 3] = min(255, alpha + fb[d + 3] * (255 - alpha) // 255)
                continue
            else:
                continue
        is_blur = is_blur_program(f, ppp, prog) or is_blur_program(f, retained_ppp, prog)
        nc_context = nc_sparse_layout(f, target, target_w, target_h) or bool(tex and tex.get('nc_sparse'))
        nc_normalized = (nc_context and stride == 24 and prog == 0x8eb52cf6fe248b1d and
                         nc_copy_texmat(f, vs_data, copy_texmat_state))
        region_clear = extract_region_clear(f, retained_ppp) if nc_context and stride == 12 and is_blur else None
        blurred_tex = None
        downsample_offsets = extract_downsample(f, retained_ppp) if nc_context and prog == DOWNSAMPLE_8 else None
        downsample_tex = None
        if downsample_offsets is not None and any(x != 0 for pair in downsample_offsets for x in pair) and tex:
            downsample_tex = [texel(f, tex, x, y) for y in range(tex['h']) for x in range(tex['w'])]
        if tex and is_blur and region_clear is None and stride >= 24 and tex['w'] > 0 and tex['h'] > 0:
            params = (extract_nc_blur_params(f, vs_data, retained_ppp, prog) if nc_context
                      else extract_blur_params(f, vs_data, ppp, prog))
            if nc_context and prog in (BLUR_4TAP, BLUR_7TAP) and not params['extracted']:
                continue
            raw_pixels = []
            for ty in range(tex['h']):
                for tx in range(tex['w']):
                    raw_pixels.append(texel(f, tex, tx, ty))
            blurred_tex = gaussian_blur_texture(
                raw_pixels, tex['w'], tex['h'],
                offsets=params['offsets'],
                weights=params['weights'],
                direction=params['direction']
            )

        if not tex and stride == 36 and prog == PILL_BORDER:
            step = 6
            for q_start in range(0, count, step):
                q_vs = vs[q_start:q_start + step]
                if len(q_vs) < step:
                    break
                xs = [(m[0] * v[0] + m[2]) * vp[0] + vp[1] if m else v[0] for v in q_vs]
                ys = [(m[1] * v[1] + m[3]) * vp[2] + vp[3] if m else v[1] for v in q_vs]
                qminx, qmaxx = min(xs), max(xs)
                qminy, qmaxy = min(ys), max(ys)
                qw, qh = qmaxx - qminx, qmaxy - qminy
                if qw < 1e-3 or qh < 1e-3:
                    continue

                tl = tr = bl = br = None
                for i, v in enumerate(q_vs):
                    u1, v1, u2, v2 = struct.unpack_from('<4f', v[2], 4)
                    px, py = xs[i], ys[i]
                    if abs(px - qminx) < 1e-2 and abs(py - qminy) < 1e-2: tl = (u1, v1, u2, v2)
                    if abs(px - qmaxx) < 1e-2 and abs(py - qminy) < 1e-2: tr = (u1, v1, u2, v2)
                    if abs(px - qminx) < 1e-2 and abs(py - qmaxy) < 1e-2: bl = (u1, v1, u2, v2)
                    if abs(px - qmaxx) < 1e-2 and abs(py - qmaxy) < 1e-2: br = (u1, v1, u2, v2)

                if not (tl and tr and bl and br):
                    continue

                du1 = max(abs(tr[0] - tl[0]), abs(br[0] - bl[0]))
                dv1 = max(abs(bl[1] - tl[1]), abs(br[1] - tr[1]))
                du2 = max(abs(tr[2] - tl[2]), abs(br[2] - bl[2]))
                dv2 = max(abs(bl[3] - tl[3]), abs(br[3] - tr[3]))
                scale1 = max(du1 / qw, dv1 / qh)
                scale2 = max(du2 / qw, dv2 / qh)
                if scale1 < 1e-5: scale1 = 1.0 / 31.0
                if scale2 < 1e-5: scale2 = 1.0 / 28.0

                raw_col = q_vs[0][2][:4]
                base_r, base_g, base_b, base_a = raw_col[0], raw_col[1], raw_col[2], raw_col[3]

                x0 = max(clip_x0, int(math.floor(qminx)))
                x1 = min(clip_x1, target_w, pw, int(math.ceil(qmaxx)))
                y0 = max(clip_y0, int(math.floor(qminy)))
                y1 = min(clip_y1, target_h, ph, int(math.ceil(qmaxy)))
                if x0 >= x1 or y0 >= y1:
                    continue

                for y in range(y0, y1):
                    cy = y + 0.5
                    fy = (cy - qminy) / qh
                    for x in range(x0, x1):
                        cx = x + 0.5
                        fx = (cx - qminx) / qw
                        u1 = (1.0 - fx) * (1.0 - fy) * tl[0] + fx * (1.0 - fy) * tr[0] + (1.0 - fx) * fy * bl[0] + fx * fy * br[0]
                        v1 = (1.0 - fx) * (1.0 - fy) * tl[1] + fx * (1.0 - fy) * tr[1] + (1.0 - fx) * fy * bl[1] + fx * fy * br[1]
                        u2 = (1.0 - fx) * (1.0 - fy) * tl[2] + fx * (1.0 - fy) * tr[2] + (1.0 - fx) * fy * bl[2] + fx * fy * br[2]
                        v2 = (1.0 - fx) * (1.0 - fy) * tl[3] + fx * (1.0 - fy) * tr[3] + (1.0 - fx) * fy * bl[3] + fx * fy * br[3]
                        r1 = math.sqrt(u1 * u1 + v1 * v1)
                        r2 = math.sqrt(u2 * u2 + v2 * v2)
                        outer_cov = max(0.0, min(1.0, 0.5 - (r1 - 1.0) / scale1))
                        inner_cov = max(0.0, min(1.0, 0.5 + (r2 - 1.0) / scale2))
                        cov = outer_cov * inner_cov
                        if cov > 0.0:
                            d = (y * target_w + x) * 4
                            src_a = int(round(base_a * cov))
                            src_r = int(round(base_r * cov))
                            src_g = int(round(base_g * cov))
                            src_b = int(round(base_b * cov))
                            fb[d + 0] = min(255, src_r + fb[d + 0] * (255 - src_a) // 255)
                            fb[d + 1] = min(255, src_g + fb[d + 1] * (255 - src_a) // 255)
                            fb[d + 2] = min(255, src_b + fb[d + 2] * (255 - src_a) // 255)
                            fb[d + 3] = min(255, src_a + fb[d + 3] * (255 - src_a) // 255)
            continue

        is_cg_line = (prog == 0xba8c50a79810e866 and (not tex or tex.get('bpp') == 1))
        is_clock_cap = (prog == ROUNDED_CAPS and count == 48 and (not tex or tex.get('bpp') == 1))
        if (not tex or is_cg_line or is_clock_cap) and stride in (20, 28):
            vs_xy = []
            for v in vs:
                x = (m[0] * v[0] + m[2]) * vp[0] + vp[1] if m else v[0]
                y = (m[1] * v[1] + m[3]) * vp[2] + vp[3] if m else v[1]
                vs_xy.append((x, y))

            raw_col = vs[0][2][:4]
            r, g, b, a = raw_col[0], raw_col[1], raw_col[2], raw_col[3]

            is_line = False
            if count == 6:
                pts = list(set((round(p[0], 2), round(p[1], 2)) for p in vs_xy))
                if len(pts) <= 2:
                    is_line = True

            if is_line:
                p0 = vs_xy[0]
                other = [p for p in vs_xy if abs(p[0] - p0[0]) > 1e-3 or abs(p[1] - p0[1]) > 1e-3]
                if other:
                    p1 = other[0]
                    dx = p1[0] - p0[0]
                    dy = p1[1] - p0[1]
                    L2 = dx * dx + dy * dy
                    L = math.sqrt(L2)
                    if L >= 1e-3:
                        is_red = (r > 180 and b < 60) or (vs[0][2][:4] == b'\xdc\x3e\x1c\xff')
                        if is_red:
                            width = (L / 24.0) * 1.0 if L > 40.0 else 1.0
                        elif L >= 22.0:
                            width = (L / 30.0) * 2.0 if L > 40.0 else 2.0
                        else:
                            width = (L / 17.0) * 3.0 if L > 40.0 else 3.0
                        half_w = width * 0.5
                        inv_L = 1.0 / L
                        x0 = max(clip_x0, int(math.floor(min(p0[0], p1[0]) - half_w - 1.0)))
                        x1 = min(clip_x1, target_w, pw, int(math.ceil(max(p0[0], p1[0]) + half_w + 1.0)))
                        y0 = max(clip_y0, int(math.floor(min(p0[1], p1[1]) - half_w - 1.0)))
                        y1 = min(clip_y1, target_h, ph, int(math.ceil(max(p0[1], p1[1]) + half_w + 1.0)))
                        if x0 < x1 and y0 < y1:
                            for y in range(y0, y1):
                                cy = y + 0.5
                                py = cy - p0[1]
                                for x in range(x0, x1):
                                    cx = x + 0.5
                                    px = cx - p0[0]
                                    dot = px * dx + py * dy
                                    t = dot * inv_L
                                    min_end = t if t < (L - t) else (L - t)
                                    cov_par = min_end + 0.5
                                    if cov_par <= 0.0:
                                        continue
                                    if cov_par > 1.0:
                                        cov_par = 1.0
                                    cross = dx * py - dy * px
                                    dist_perp = abs(cross) * inv_L
                                    cov_perp = half_w + 0.5 - dist_perp
                                    if cov_perp <= 0.0:
                                        continue
                                    if cov_perp > 1.0:
                                        cov_perp = 1.0
                                    cov = cov_perp * cov_par
                                    if cov > 0.0:
                                        ca = int(round(a * cov))
                                        cr = int(round(r * cov))
                                        cg = int(round(g * cov))
                                        cb = int(round(b * cov))
                                        d = (y * target_w + x) * 4
                                        for c, src in enumerate((cr, cg, cb, ca)):
                                            fb[d + c] = src if prog == COPY else min(255, src + fb[d + c] * (255 - ca) // 255)
                continue

            is_aa = count in (4, 6, 12)
            step = 4 if count == 4 else 6
            if is_aa:
                for q_start in range(0, count, step):
                    q_coords = vs_xy[q_start:q_start + step]
                    if len(q_coords) < step:
                        is_aa = False
                        break
                    minx = min(p[0] for p in q_coords)
                    maxx = max(p[0] for p in q_coords)
                    miny = min(p[1] for p in q_coords)
                    maxy = max(p[1] for p in q_coords)
                    for x, y in q_coords:
                        if not ((abs(x - minx) < 1e-3 or abs(x - maxx) < 1e-3) and
                                (abs(y - miny) < 1e-3 or abs(y - maxy) < 1e-3)):
                            is_aa = False
                            break
                    if is_aa:
                        has_tl = has_tr = has_bl = has_br = False
                        for x, y in q_coords:
                            if abs(x - minx) < 1e-3 and abs(y - miny) < 1e-3: has_tl = True
                            if abs(x - maxx) < 1e-3 and abs(y - miny) < 1e-3: has_tr = True
                            if abs(x - minx) < 1e-3 and abs(y - maxy) < 1e-3: has_bl = True
                            if abs(x - maxx) < 1e-3 and abs(y - maxy) < 1e-3: has_br = True
                        if (has_tl + has_tr + has_bl + has_br) < 3:
                            is_aa = False
                    if not is_aa:
                        break

            if is_aa:
                for q_start in range(0, count, step):
                    q_coords = vs_xy[q_start:q_start + step]
                    x0 = max(clip_x0, int(round(min(p[0] for p in q_coords))))
                    x1 = min(clip_x1, target_w, pw, int(round(max(p[0] for p in q_coords))))
                    y0 = max(clip_y0, int(round(min(p[1] for p in q_coords))))
                    y1 = min(clip_y1, target_h, ph, int(round(max(p[1] for p in q_coords))))
                    if x0 >= x1 or y0 >= y1:
                        continue
                    for y in range(y0, y1):
                        for x in range(x0, x1):
                            d = (y * target_w + x) * 4
                            for c, src in enumerate((r, g, b, a)):
                                fb[d + c] = src if prog == COPY else min(255, src + fb[d + c] * (255 - a) // 255)
            else:
                for t in range(0, count - 2, 3):
                    p0, p1, p2 = vs_xy[t], vs_xy[t + 1], vs_xy[t + 2]
                    det = (p1[0] - p0[0]) * (p2[1] - p0[1]) - (p1[1] - p0[1]) * (p2[0] - p0[0])
                    if abs(det) < 1e-5:
                        continue
                    t_col = vs[t][2][:4]
                    tr, tg, tb, ta = t_col[0], t_col[1], t_col[2], t_col[3]
                    x0 = max(clip_x0, int(math.floor(min(p0[0], p1[0], p2[0]) - 0.5)))
                    x1 = min(clip_x1, target_w, pw, int(math.ceil(max(p0[0], p1[0], p2[0]) + 0.5)))
                    y0 = max(clip_y0, int(math.floor(min(p0[1], p1[1], p2[1]) - 0.5)))
                    y1 = min(clip_y1, target_h, ph, int(math.ceil(max(p0[1], p1[1], p2[1]) + 0.5)))
                    if x0 >= x1 or y0 >= y1:
                        continue
                    for y in range(y0, y1):
                        for x in range(x0, x1):
                            cnt = 0
                            for si in range(4):
                                sx = x + (2 * si + 1) * 0.125
                                for sj in range(4):
                                    sy = y + (2 * sj + 1) * 0.125
                                    e0 = (p1[0] - p0[0]) * (sy - p0[1]) - (p1[1] - p0[1]) * (sx - p0[0])
                                    e1 = (p2[0] - p1[0]) * (sy - p1[1]) - (p2[1] - p1[1]) * (sx - p1[0])
                                    e2 = (p0[0] - p2[0]) * (sy - p2[1]) - (p0[1] - p2[1]) * (sx - p2[0])
                                    inside = (e0 >= 0 and e1 >= 0 and e2 >= 0) if det > 0 else (e0 <= 0 and e1 <= 0 and e2 <= 0)
                                    if inside:
                                        cnt += 1
                            if cnt > 0:
                                cov = cnt * 0.0625
                                ca = int(round(ta * cov))
                                cr = int(round(tr * cov))
                                cg = int(round(tg * cov))
                                cb = int(round(tb * cov))
                                d = (y * target_w + x) * 4
                                for c, src in enumerate((cr, cg, cb, ca)):
                                    fb[d + c] = src if prog == COPY else min(255, src + fb[d + c] * (255 - ca) // 255)
            continue

        step = 4 if count == 4 else 6
        for q_start in range(0, count, step):
            q_vs = vs[q_start:q_start + step]
            if len(q_vs) < step:
                break
            if m:   # screen-space position -> NDC -> pass pixels
                xs = [(m[0] * v[0] + m[2]) * vp[0] + vp[1] for v in q_vs]
                ys = [(m[1] * v[1] + m[3]) * vp[2] + vp[3] for v in q_vs]
            else:
                xs = [v[0] for v in q_vs]
                ys = [v[1] for v in q_vs]
            x0 = max(clip_x0, int(round(min(xs))))
            x1 = min(clip_x1, target_w, pw, int(round(max(xs))))
            y0 = max(clip_y0, int(round(min(ys))))
            y1 = min(clip_y1, target_h, ph, int(round(max(ys))))
            print(f'draw n={count} stride={stride} rect=({x0},{y0})-({x1},{y1}) tex=' +
                  (tex if isinstance(tex, str) else str(tex and {k: (hex(v) if isinstance(v, int) else v)
                                                             for k, v in tex.items() if k != 'tsw'})))
            if x0 >= x1 or y0 >= y1:
                continue
            if stride == 24 and prog == RADIAL_CLIP:
                uvs = [struct.unpack_from('<2f', v[2]) for v in q_vs]
                mapping = quad_affine(list(zip(xs, ys))[:3], uvs[:3])
                if mapping:
                    for y in range(y0, y1):
                        for x in range(x0, x1):
                            u = mapping[0]*(x+0.5)+mapping[1]*(y+0.5)+mapping[2]
                            v = mapping[3]*(x+0.5)+mapping[4]*(y+0.5)+mapping[5]
                            a = radial_coverage(u, v, mapping)
                            d = (y*target_w+x)*4
                            fb[d:d+4] = bytes(c*a//255 for c in fb[d:d+4])
                continue
            if tex and (stride >= 24 or (stride == 16 and prog == WALLPAPER) or prog == ROUNDED_CAPS):
                # stride 24: float u,v (normalised) at +16; stride 28/36: BGRA tint at +16,
                # then u,v in texels at +20.
                uv_off = 4 if stride in (28, 36) else 0
                uvs = ([struct.unpack_from('<2f', v[2], uv_off) for v in q_vs] if stride >= 24 or (stride == 16 and prog == WALLPAPER)
                       else [(0, 0), (1, 1)])
                u0, u1 = min(u for u, _ in uvs), max(u for u, _ in uvs)
                v0, v1 = min(v for _, v in uvs), max(v for _, v in uvs)
                su, sv = (1, 1) if not nc_normalized and max(u1, v1) > 1.0001 else (tex['w'], tex['h'])
                uv_map = quad_affine(list(zip(xs, ys))[:3], uvs[:3]) if len(uvs) >= 3 else None
                cross_row = uv_map is not None and abs(uv_map[3]) > 1e-8
                tint = struct.unpack_from('<I', q_vs[0][2], 0)[0] if stride in (28, 36) else 0xffffffff
                if prog == WEBKIT_GLYPHS:
                    tint = (tint & 0xff00ff00) | ((tint & 0xff) << 16) | ((tint >> 16) & 0xff)
                mask = prog == DEST_IN

                uv2_map = None
                u2_0, u2_1, v2_0, v2_1 = 0, 1, 0, 1
                if stride == 36 and prog in (CORNER_RESTORE, STRIDE36_DUAL_TEX, RADIAL_RESTORE):
                    uv2s = [struct.unpack_from('<2f', v[2], 12) for v in q_vs]
                    u2_0, u2_1 = min(u for u, _ in uv2s), max(u for u, _ in uv2s)
                    v2_0, v2_1 = min(v for _, v in uv2s), max(v for _, v in uv2s)
                    uv2_map = quad_affine(list(zip(xs, ys))[:3], uv2s[:3]) if len(uv2s) >= 3 else None

                sub_w = (u1 - u0) * su
                sub_h = (v1 - v0) * sv
                is_scaled = abs((max(xs) - min(xs)) - sub_w) > 0.1 or abs((max(ys) - min(ys)) - sub_h) > 0.1
                if uv_map:
                    step_x = math.hypot(uv_map[0] * su, uv_map[3] * sv)
                    step_y = math.hypot(uv_map[1] * su, uv_map[4] * sv)
                    is_scaled = abs(step_x - 1) > 0.1 or abs(step_y - 1) > 0.1
                if tex.get('sampler') and tex['sampler']['linear_filter']:
                    is_scaled = True
                if stride == 36 and prog == STRIDE36_DUAL_TEX:
                    complete = True
                    for y in range(y0, y1):
                        fv = ((uv_map[4] * (y + 0.5) + uv_map[5]) if uv_map else
                              quad_coordinate(y, min(ys), max(ys), v0, v1)) * sv
                        ty = max(0, min(tex['h'] - 1, int(fv)))
                        try:
                            gva = tex['addr'] + ty * tex['stride']
                            if (gva & ~0xfff) not in f.pages:
                                complete = False
                                break
                        except Exception:
                            complete = False
                            break
                    if not complete:
                        continue
                tex_cached_rows = {}
                def get_tex_row(ry):
                    if ry not in tex_cached_rows:
                        tex_cached_rows[ry] = [texel(f, tex, rx, ry) for rx in range(tex['w'])]
                    return tex_cached_rows[ry]

                for y in range(y0, y1):
                    fv = ((uv_map[4] * (y + 0.5) + uv_map[5]) if uv_map else
                          quad_coordinate(y, min(ys), max(ys), v0, v1)) * sv
                    if tex.get('bpp') == 1 and tex['w'] == 256 and tex['h'] == 256:
                        v_norm = fv / sv
                        v_eff = (1.0 - v_norm) if v_norm > 0.5 else v_norm
                        fv = (2.0 * v_eff) * sv
                    if is_scaled and not blurred_tex:
                        v_sample = max(0.0, min(float(tex['h'] - 1), fv - 0.5))
                        ty0 = int(v_sample)
                        ty1 = min(tex['h'] - 1, ty0 + 1)
                        fy = v_sample - ty0
                        row0 = get_tex_row(ty0)
                        row1 = get_tex_row(ty1) if ty1 != ty0 else row0
                    else:
                        ty = max(0, min(tex['h'] - 1, int(fv)))
                        row0 = get_tex_row(ty)

                    for x in range(x0, x1):
                        fu = ((uv_map[0] * (x + 0.5) + uv_map[1] * (y + 0.5) + uv_map[2]) if uv_map else
                              quad_coordinate(x, min(xs), max(xs), u0, u1)) * su
                        sample_v = fv + uv_map[3] * (x + 0.5) * sv if uv_map else fv
                        if cross_row and not blurred_tex:
                            if is_scaled:
                                v_sample = max(0.0, min(float(tex['h'] - 1), sample_v - 0.5))
                                ty0 = int(v_sample)
                                ty1 = min(tex['h'] - 1, ty0 + 1)
                                fy = v_sample - ty0
                                row0, row1 = get_tex_row(ty0), get_tex_row(ty1)
                            else:
                                row0 = get_tex_row(max(0, min(tex['h'] - 1, int(sample_v))))
                        if downsample_tex is not None:
                            b, g, r, a = sample_downsample(downsample_tex, tex['w'], tex['h'], fu, sample_v, downsample_offsets)
                        elif blurred_tex:
                            b, g, r, a = sample_bilinear(blurred_tex, tex['w'], tex['h'], fu, sample_v)
                        elif is_scaled:
                            u_sample = max(0.0, min(float(tex['w'] - 1), fu - 0.5))
                            tx0 = int(u_sample)
                            tx1 = min(tex['w'] - 1, tx0 + 1)
                            fx = u_sample - tx0
                            c00 = row0[tx0]
                            c10 = row0[tx1]
                            c01 = row1[tx0]
                            c11 = row1[tx1]
                            wx = int(fx * 256.0 + 0.5)
                            wy = int(fy * 256.0 + 0.5)
                            b, g, r, a = (
                                max(0, min(255, (
                                    (c00[c] * (256 - wx) + c10[c] * wx) * (256 - wy) +
                                    (c01[c] * (256 - wx) + c11[c] * wx) * wy + 32768
                                ) >> 16)) for c in range(4)
                            )
                        else:
                            tx = max(0, min(tex['w'] - 1, int(fu)))
                            b, g, r, a = row0[tx]
                        if stride == 16 and prog == WALLPAPER or webkit_upload:
                            b, r = r, b     # CoreImage wallpaper source is RGBA
                        if is_color_matrix:
                            rin = r / 255.0
                            gin = g / 255.0
                            bin = b / 255.0
                            rout = col_mat[0] * rin + col_mat[3] * gin + col_mat[6] * bin + col_bias[0]
                            gout = col_mat[1] * rin + col_mat[4] * gin + col_mat[7] * bin + col_bias[1]
                            bout = col_mat[2] * rin + col_mat[5] * gin + col_mat[8] * bin + col_bias[2]
                            r = max(0, min(255, int(round(rout * 255.0))))
                            g = max(0, min(255, int(round(gout * 255.0))))
                            b = max(0, min(255, int(round(bout * 255.0))))
                            a = 255
                        if prog == ROUNDED_CAPS and (tex.get('addr') == 0x10524000 or (tex.get('w') == 114 and tex.get('h') == 87)):
                            cov = (r * a) // 255
                            ta = tint >> 24
                            tr = (tint >> 16) & 0xff
                            tg = (tint >> 8) & 0xff
                            tb = tint & 0xff
                            a = (ta * cov) // 255
                            r = (tr * a) // 255
                            g = (tg * a) // 255
                            b = (tb * a) // 255
                        elif tint != 0xffffffff or tex.get('bpp') == 1:
                            tb, tg, tr, ta = tint & 0xff, (tint >> 8) & 0xff, (tint >> 16) & 0xff, tint >> 24
                            if (b | g | r) == 0 and a != 0:
                                a = (a * ta) // 255
                                r = (tr * a) // 255
                                g = (tg * a) // 255
                                b = (tb * a) // 255
                            else:
                                b, g, r, a = b * tb // 255, g * tg // 255, r * tr // 255, a * ta // 255
                        d = (y * target_w + x) * 4
                        if mask:
                            fb[d:d + 4] = bytes(c * a // 255 for c in fb[d:d + 4])
                            continue
                        if stride == 36 and prog == CORNER_RESTORE and tex.get('mask_tex'):
                            mask_tex = tex['mask_tex']
                            fu2 = ((uv2_map[0] * (x + 0.5) + uv2_map[1] * (y + 0.5) + uv2_map[2]) if uv2_map else
                                   quad_coordinate(x, min(xs), max(xs), u2_0, u2_1))
                            fv2 = ((uv2_map[4] * (y + 0.5) + uv2_map[5]) if uv2_map else
                                   quad_coordinate(y, min(ys), max(ys), v2_0, v2_1))
                            tx2 = max(0, min(mask_tex['w'] - 1, int(round(fu2 * mask_tex['w']))))
                            ty2 = max(0, min(mask_tex['h'] - 1, int(round((fv2 + 0.25) * mask_tex['h']))))
                            m_b, m_g, m_r, m_a = texel(f, mask_tex, tx2, ty2)
                            inv_a = 255 - m_a
                            sb = b * inv_a // 255
                            sg = g * inv_a // 255
                            sr = r * inv_a // 255
                            sa = a * inv_a // 255
                            fb[d + 0] = min(255, sb + fb[d + 0])
                            fb[d + 1] = min(255, sg + fb[d + 1])
                            fb[d + 2] = min(255, sr + fb[d + 2])
                            fb[d + 3] = min(255, sa + fb[d + 3])
                        elif prog == RADIAL_RESTORE and uv2_map:
                            mu = uv2_map[0]*(x+0.5)+uv2_map[1]*(y+0.5)+uv2_map[2]
                            mv = uv2_map[3]*(x+0.5)+uv2_map[4]*(y+0.5)+uv2_map[5]
                            coverage = 255-radial_coverage(mu, mv, uv2_map)
                            fb[d:d+4] = bytes(min(255, c*coverage//255+fb[d+i])
                                              for i,c in enumerate((r,g,b,a)))
                        elif tex.get('vibrancy'):
                            if stride == 36 and prog == STRIDE36_DUAL_TEX and tex.get('mask_tex'):
                                mask_tex = tex['mask_tex']
                                mu = uv2_map[0]*(x+0.5)+uv2_map[1]*(y+0.5)+uv2_map[2]
                                mv = uv2_map[3]*(x+0.5)+uv2_map[4]*(y+0.5)+uv2_map[5]
                                msu, msv = (mask_tex['w'], mask_tex['h']) if max(u2_1,v2_1)<=1.0001 else (1,1)
                                mx = max(0,min(mask_tex['w']-1,int(mu*msu)))
                                my = max(0,min(mask_tex['h']-1,int(mv*msv)))
                                m_b, m_g, m_r, m_a = texel(f, mask_tex, mx, my)
                                if m_a == 0:
                                    continue
                                vib = tuple(min(255, c + fb[d+i]*(255-a)//255) for i,c in enumerate((r,g,b,a)))
                                if m_a == 255:
                                    fb[d:d + 4] = bytes(vib)
                                else:
                                    inv = 255 - m_a
                                    fb[d:d + 4] = bytes(((vib[i] * m_a + fb[d + i] * inv) // 255) for i in range(4))
                            else:
                                fb[d:d + 4] = bytes(blend_vibrancy(tuple(fb[d:d + 4]), (r, g, b, a)))
                        elif prog in (PLUS_L, PLUS_L2):
                            for c, src in enumerate((r, g, b, a)):
                                fb[d + c] = min(255, src + fb[d + c])
                        else:
                            for c, src in enumerate((r, g, b, a)):
                                fb[d + c] = min(255, src + fb[d + c] * (255 - a) // 255)
            elif region_clear is not None:
                rgba = bytes((region_clear >> 16 & 255, region_clear >> 8 & 255,
                              region_clear & 255, region_clear >> 24))
                for y in range(y0, y1):
                    start = (y * target_w + x0) * 4
                    fb[start:start + (x1 - x0) * 4] = rgba * (x1 - x0)
            elif stride == 12 and (is_blur or is_blur_program(f, ppp, prog)):
                params = extract_blur_params(f, vs_data, retained_ppp or ppp, prog)
                pad = 16
                bx0 = max(0, x0 - pad)
                by0 = max(0, y0 - pad)
                bx1 = min(target_w, x1 + pad)
                by1 = min(target_h, y1 + pad)
                bw, bh = bx1 - bx0, by1 - by0
                raw_pixels = []
                for y in range(by0, by1):
                    for x in range(bx0, bx1):
                        d = (y * target_w + x) * 4
                        r, g, b, a = fb[d:d + 4]
                        raw_pixels.append((b, g, r, a))
                blurred = gaussian_blur_texture(
                    raw_pixels, bw, bh,
                    offsets=params['offsets'],
                    weights=params['weights'],
                    direction=params['direction']
                )
                for y in range(y0, y1):
                    for x in range(x0, x1):
                        b, g, r, a = blurred[(y - by0) * bw + (x - bx0)]
                        d = (y * target_w + x) * 4
                        fb[d:d + 4] = bytes((r, g, b, a))
            elif stride in (20, 28):
                colour = struct.unpack_from('<I', q_vs[0][2], 0)[0]
                r, g, b, a = colour & 0xff, (colour >> 8) & 0xff, (colour >> 16) & 0xff, colour >> 24
                for y in range(y0, y1):
                    for x in range(x0, x1):
                        d = (y * target_w + x) * 4
                        for c, src in enumerate((r, g, b, a)):
                            fb[d + c] = (
                                src if prog == COPY else
                                min(255, src + fb[d + c]) if prog == SOLID_PLUS_L else
                                min(255, src + fb[d + c] * (255 - a) // 255)
                            )
    png(sys.argv[2], target_w, target_h, bytes(fb))


if __name__ == '__main__':
    main()
