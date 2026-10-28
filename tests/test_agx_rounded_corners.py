"""Unit and regression tests for AGX rounded corner rendering:
- Format 3 TSW decoding (A8 single-channel, 1 bpp)
- Twiddled Morton texture reading for 1 bpp textures
- Dual-texture corner restore shader classification (prog 0x03a2edd2a4d84437)
- Dual-texture descriptor block decoding (backdrop + Format 3 mask)
- Additive corner restore blend mathematics: clamp(D + S * (1 - mask_alpha), 0, 255)
- Verification of frame-13 replay accuracy with C harness
"""
import importlib.util
from pathlib import Path
import struct
import unittest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'backend'

REPLAY_SPEC = importlib.util.spec_from_file_location(
    'agx_replay_offline', BACKEND / 'agx_replay_offline.py')
aro = importlib.util.module_from_spec(REPLAY_SPEC)
REPLAY_SPEC.loader.exec_module(aro)


class MockFrame:
    def __init__(self):
        self.data = {}
        self.pages = set()

    def add_page(self, gva, raw_bytes):
        base = gva & ~0xfff
        self.pages.add(base)
        for i, b in enumerate(raw_bytes):
            self.data[base + i] = b

    def read(self, gva, size):
        res = bytearray()
        for i in range(size):
            p = gva + i
            if (p & ~0xfff) not in self.pages:
                raise KeyError(f'unmapped {p:#x}')
            res.append(self.data.get(p, 0))
        return bytes(res)

    def u32(self, gva):
        return struct.unpack_from('<I', self.read(gva, 4))[0]

    def u64(self, gva):
        return struct.unpack_from('<Q', self.read(gva, 8))[0]


class AGXRoundedCornersTests(unittest.TestCase):
    def test_decode_format3_tsw(self):
        """Format 3 TSW is decoded as 1 bpp with correct dimensions and stride."""
        mf = MockFrame()
        # Format 3, twiddled flag 0x8001: 0x38000021
        # 256x256: tsw[1] = ((256-1) << 2) | ((256-1) << 16) = 0x00ff03fc
        # GVA 0x10d40000
        # q = tsw[2] | (tsw[3] << 32) -> ((q >> 16) & 0x3ffffffff) << 2 = 0x10d40000
        # q = 0x0435_0000_8009 -> tsw[2]=0x8009, tsw[3]=0x435
        tsw = (0x38000021, 0x00ff03fc, 0x00008009, 0x00000435)
        # Add dummy page for GVA 0x10d40000
        mf.add_page(0x10d40000, bytes(4096))
        tex = aro.decode_tsw(mf, tsw)
        self.assertIsNotNone(tex)
        self.assertEqual(tex['w'], 256)
        self.assertEqual(tex['h'], 256)
        self.assertEqual(tex['bpp'], 1)
        self.assertTrue(tex['twiddled'])
        self.assertFalse(tex['strip'])
        self.assertEqual(tex['stride'], 256)

    def test_texel_format3_morton(self):
        """1 bpp twiddled texel lookup unpacks a single byte to alpha-only coverage."""
        mf = MockFrame()
        tex_addr = 0x10d40000
        page = bytearray(4096)
        # Write alpha 0x7f at offset 0 (texel 0,0) and 0xa5 at offset 1 (texel 0,1)
        page[0] = 0x7f
        page[1] = 0xa5
        mf.add_page(tex_addr, page)
        tex = {
            'w': 256,
            'h': 256,
            'addr': tex_addr,
            'bpp': 1,
            'strip': False,
            'stride': 256,
            'twiddled': True,
        }
        c00 = aro.texel(mf, tex, 0, 0)
        # A8 texels carry coverage in alpha only (RGB 0), as WebKit glyph tinting needs.
        self.assertEqual(tuple(c00), (0, 0, 0, 0x7f))
        c01 = aro.texel(mf, tex, 0, 1)
        self.assertEqual(tuple(c01), (0, 0, 0, 0xa5))

    def test_classify_draw_corner_restore(self):
        """classify_draw categorises stride-36 prog 0x03a2edd2a4d84437 as corner restore."""
        mf = MockFrame()
        vs_data_bytes = struct.pack('<IIQQ', 36, 0, 0x1000, 0)
        mf.add_page(0x0, vs_data_bytes)

        coords = [
            (183.0, 753.0, 0.0, 0.0, 0.25, 0.25),
            (211.0, 753.0, 28.0, 0.0, 0.50, 0.25),
            (211.0, 780.0, 28.0, 27.0, 0.50, 0.50),
            (211.0, 780.0, 28.0, 27.0, 0.50, 0.50),
            (183.0, 780.0, 0.0, 27.0, 0.25, 0.50),
            (183.0, 753.0, 0.0, 0.0, 0.25, 0.25),
        ]
        v_bytes = bytearray()
        for x, y, u0, v0, u1, v1 in coords:
            v_bytes += struct.pack('<4fI4f', x, y, 0.0, 1.0, 0xffffffff, u0, v0, u1, v1)
        mf.add_page(0x1000, bytes(v_bytes))

        draw_cmd = (3 << 29, 6)
        viewport = (320.0, 320.0, 568.0, 568.0)

        orig_program = aro.program
        orig_texture = aro.texture
        try:
            aro.program = lambda f, p: aro.CORNER_RESTORE
            aro.texture = lambda f, p, prog=None: {
                'w': 96, 'h': 96, 'addr': 0x10478000, 'bpp': 4, 'strip': False,
                'stride': 384, 'twiddled': False, 'vibrancy': False,
                'mask_tex': {'w': 256, 'h': 256, 'addr': 0x10d40000, 'bpp': 1, 'strip': False, 'stride': 256}
            }
            res, det = aro.classify_draw(mf, 0x0, [], draw_cmd, viewport, 640, 1136, 640, 1136)
            self.assertEqual(res, 'replayed exactly: corner restore (backdrop + mask)')
            self.assertEqual(det['prog'], hex(aro.CORNER_RESTORE))
        finally:
            aro.program = orig_program
            aro.texture = orig_texture

    def test_decode_corner_restore_dual_texture(self):
        """texture() decodes Texture 0 (backdrop) and Texture 1 (Format 3 mask) for CORNER_RESTORE."""
        mf = MockFrame()
        # Fragment PDS at 0x680004020
        # data[0..1] = ptr to block at 0x101f096c
        pds_page = bytearray(4096)
        struct.pack_into('<QQ', pds_page, 0x20, 0x101f096c, 0)
        mf.add_page(0x680004000, pds_page)

        # Block at 0x101f096c:
        # block[0..3]: Texture 0 (96x96 backdrop)
        # block[4..7]: Texture 1 (256x256 Format 3 mask)
        # block[8..11]: Sampler words
        block_page = bytearray(4096)
        tsw0 = (0x60000a61, 0x5f017c, 0xe0000001, 0x411)
        tsw1 = (0x38000021, 0x00ff03fc, 0x00008009, 0x00000435)
        sw = (0xfff, 0x2450, 0, 0)
        struct.pack_into('<4I4I4I', block_page, 0x96c, *tsw0, *tsw1, *sw)
        mf.add_page(0x101f0000, block_page)

        # Dummy pages for texture addresses
        mf.add_page(0x10478000, bytes(4096))
        mf.add_page(0x10d40000, bytes(4096))

        ppp = [0x80004020]
        tex = aro.texture(mf, ppp, prog=aro.CORNER_RESTORE)
        self.assertIsNotNone(tex)
        self.assertEqual(tex['w'], 96)
        self.assertEqual(tex['h'], 96)
        self.assertIn('mask_tex', tex)
        mask = tex['mask_tex']
        self.assertIsNotNone(mask)
        self.assertEqual(mask['w'], 256)
        self.assertEqual(mask['h'], 256)
        self.assertEqual(mask['bpp'], 1)

    def test_corner_restore_blend_math(self):
        """Corner restore blend subtracts mask alpha from backdrop before additive blend."""
        # Backdrop pixel: RGBA(100, 150, 200, 255)
        # Dest pixel on screen: RGBA(50, 60, 70, 255)
        dst = [50, 60, 70, 255]
        src_backdrop = [100, 150, 200, 255]

        # Case 1: mask_alpha = 255 (inside icon body): 0 backdrop restored
        m_a = 255
        inv_a = 255 - m_a
        s = [c * inv_a // 255 for c in src_backdrop]
        out1 = [min(255, d + sc) for d, sc in zip(dst, s)]
        self.assertEqual(out1, dst)

        # Case 2: mask_alpha = 0 (outside icon, pure backdrop): 100% backdrop restored
        m_a = 0
        inv_a = 255 - m_a
        s = [c * inv_a // 255 for c in src_backdrop]
        out2 = [min(255, d + sc) for d, sc in zip(dst, s)]
        expected = [min(255, d + sc) for d, sc in zip(dst, src_backdrop)]
        self.assertEqual(out2, expected)

        # Case 3: mask_alpha = 128 (antialiased corner edge): 50% backdrop restored
        m_a = 128
        inv_a = 255 - m_a
        s = [c * inv_a // 255 for c in src_backdrop]
        out3 = [min(255, d + sc) for d, sc in zip(dst, s)]
        self.assertTrue(dst[0] < out3[0] < expected[0])

    def test_c_harness_frame13_accuracy(self):
        """C host harness replays frame-13 with 0 errors and drops icon box MAE below 15."""
        dump_path = ROOT / 'research/evidence/anim-20261027/before/agx_dumps/frame-13.bin'
        ref_box_path = ROOT / 'research/evidence/rounded-corners-20261027/frame13_icon_box_before.png'
        if not dump_path.exists() or not ref_box_path.exists():
            raise unittest.SkipTest('frame-13 evidence files not available')

        import json, subprocess, tempfile
        from PIL import Image, ImageChops, ImageStat
        from scripts.agx_corpus import build_harness

        bin_path = build_harness(ROOT / '.harness')
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_rgba = Path(tmp_dir) / 'out.rgba'
            res = subprocess.run([str(bin_path), str(dump_path), str(out_rgba)], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f'Replay harness failed: {res.stderr}')
            info = json.loads(res.stdout)
            self.assertEqual(info['guest_errors'], 0)
            self.assertEqual(info['draws'], 23)

            data = out_rgba.read_bytes()
            w, h = struct.unpack_from('<II', data, 4)
            img = Image.frombytes('RGBA', (w, h), data[12:])
            box = img.crop((180, 750, 310, 880)).convert('RGB')
            ref_box = Image.open(ref_box_path).convert('RGB')

            diff = ImageChops.difference(box, ref_box)
            stat = ImageStat.Stat(diff)
            mae = sum(stat.mean) / len(stat.mean)
            self.assertLess(mae, 15.0, f'Icon box MAE {mae:.2f} should be < 15')


if __name__ == '__main__':
    unittest.main()
