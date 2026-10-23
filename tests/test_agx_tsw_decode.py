import importlib.util
from pathlib import Path
import struct
import tempfile
import unittest

FRAME_SPEC = importlib.util.spec_from_file_location(
    'agx_frame', Path(__file__).resolve().parents[1] / 'backend/agx_frame.py')
frame_module = importlib.util.module_from_spec(FRAME_SPEC)
FRAME_SPEC.loader.exec_module(frame_module)

REPLAY_SPEC = importlib.util.spec_from_file_location(
    'agx_replay_offline', Path(__file__).resolve().parents[1] / 'backend/agx_replay_offline.py')
replay_module = importlib.util.module_from_spec(REPLAY_SPEC)
REPLAY_SPEC.loader.exec_module(replay_module)


def make_frame_with_pds(pds_data_words, desc_blocks):
    """Create a minimal synthetic frame with a TA/VDM draw referencing given PDS data and descriptor blocks."""
    # Context 1, flags 1
    # 3 initial pages + desc_blocks + 1 texture page
    total_records = 3 + len(desc_blocks) + 1
    header = struct.pack('<4sIIIQQIIIIQQ', b'AGXF', 1, 1, 1,
                         0x803000000, 0x400000000, 0x280, 0x280,
                         0xc0474000, 0xc0470000, total_records, 0x803cbd000)
    render = struct.pack('<II', 1, 1) + bytes(0x70 - 8) + struct.pack('<II', 0x3f, 0x27f) + bytes(0x280 - 0x78)
    ta = struct.pack('<II', 0, 1) + bytes(0x68 - 8) + struct.pack('<Q', 0x1000) + bytes(0x280 - 0x70)

    # VDM control stream at 0x1000:
    # 1. Type 2 (VDM state, bit 27 set -> vs_data)
    # 2. Type 0 (PPP state: count, gva)
    # 3. Type 3 (Draw: count, index)
    # 4. Type 6 (Terminate)
    vdm_stream = bytearray(4096)
    # Block 2: w0 = (2 << 29) | (1 << 27), vs_data = 0x680004000
    struct.pack_into('<II', vdm_stream, 0, (2 << 29) | (1 << 27), 0x80004000)
    # Block 0: count = 1, gva = 0x2000
    struct.pack_into('<II', vdm_stream, 8, (0 << 29) | (1 << 8), 0x2000)
    # Block 3: Draw
    struct.pack_into('<III', vdm_stream, 16, (3 << 29) | 4, 4, 0)
    # Block 6: Terminate
    struct.pack_into('<I', vdm_stream, 28, 6 << 29)

    # PPP stream at 0x2000: one word pointing to fragment PDS data at 0x680004020
    ppp_page = bytearray(4096)
    struct.pack_into('<I', ppp_page, 0, 0x80004020)

    # PDS heap page at 0x680004000:
    # +0x00: vertex PDS data: stride 12, buffer GVA 0x3000
    # +0x20: fragment PDS data: pds_data_words
    pds_page = bytearray(4096)
    struct.pack_into('<IIQ', pds_page, 0, 12, 0, 0x3000)
    for i, w in enumerate(pds_data_words):
        struct.pack_into('<I', pds_page, 0x20 + 4 * i, w)

    # Descriptor blocks mapped at their respective addresses
    records = [
        (0x1000, 0x801000000, bytes(vdm_stream)),
        (0x2000, 0x801100000, bytes(ppp_page)),
        (0x680004000, 0x801200000, bytes(pds_page)),
    ]
    for gva, words in desc_blocks:
        page = bytearray(4096)
        off = gva & 0xfff
        for i, w in enumerate(words):
            struct.pack_into('<I', page, off + 4 * i, w)
        records.append((gva & ~0xfff, 0x802000000 + (gva & ~0xfff), bytes(page)))

    # Dummy texture page mapped at 0x10200000
    tex_page = bytearray(4096)
    records.append((0x10200000, 0x803000000, bytes(tex_page)))

    data = header + render + ta + b''.join(
        struct.pack('<QQ4096s', gva, pa, page) for gva, pa, page in records)
    return data


class AgxTswDecodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _make_tsw(self, w=158, h=48, addr=0x10200000):
        # TSW word 0: layout 0 (linear), format 6 (BGRA)
        w0 = (6 << 28) | 0x8864
        w1 = ((h - 1) << 16) | ((w - 1) << 2)
        q = (addr >> 2) << 16
        w2 = q & 0xffffffff
        w3 = (q >> 32) & 0xffffffff
        return [w0, w1, w2, w3]

    def test_decode_tsw_at_offset_0_and_4(self):
        """TSW at offset 0 (stand-alone) and offset 4 (after sampler) must be found."""
        tsw = self._make_tsw(158, 48, 0x10200000)

        # Stand-alone TSW at offset 0
        desc_0 = tsw + [0, 0, 0, 0]
        # TSW preceded by 4-word sampler at offset 4
        sampler = [0x00000fff, 0x00002400, 0, 0]
        desc_4 = sampler + tsw

        # Verify decode_tsw recognizes them
        f_path = Path(self.temp.name) / 'frame_offset0.bin'
        f_path.write_bytes(make_frame_with_pds([0x10100000, 0], [(0x10100000, desc_0)]))
        frame = frame_module.Frame(f_path)
        ppp = [0x80004020]
        tex = replay_module.texture(frame, ppp)
        self.assertIsNotNone(tex)
        self.assertEqual(tex['w'], 158)
        self.assertEqual(tex['h'], 48)
        self.assertEqual(tex['addr'], 0x10200000)

        f_path4 = Path(self.temp.name) / 'frame_offset4.bin'
        f_path4.write_bytes(make_frame_with_pds([0x10100000, 0], [(0x10100000, desc_4)]))
        frame4 = frame_module.Frame(f_path4)
        tex4 = replay_module.texture(frame4, ppp)
        self.assertIsNotNone(tex4)
        self.assertEqual(tex4['w'], 158)
        self.assertEqual(tex4['h'], 48)

    def test_reject_tsw_at_offset_6_and_8(self):
        """TSWs at unaligned or overrun offsets (k=6, k=8) must NOT be matched."""
        tsw = self._make_tsw(158, 48, 0x10200000)
        # Block with 6 words of non-TSW, followed by TSW at k=6
        desc_k6 = [0, 0, 0, 0, 0x4088000, 0x2000] + tsw[:2]  # TSW at offset 6

        f_path = Path(self.temp.name) / 'frame_k6.bin'
        f_path.write_bytes(make_frame_with_pds([0x10100000, 0], [(0x10100000, desc_k6)]))
        frame = frame_module.Frame(f_path)
        ppp = [0x80004020]
        tex = replay_module.texture(frame, ppp)
        self.assertIsNone(tex, "TSW at offset k=6 must not be matched (overrun prevention)")

    def test_vibrancy_requires_selected_descriptor_luma_prefix(self):
        """A luma prefix is vibrancy; adjacent allocation bytes are not."""
        tsw = self._make_tsw(158, 48, 0x10200000)
        luma = [0x3d93a000, 0x3e59a000, 0x3f372000]

        # Case A: LUMA at offset 0, TSW at offset 4 (or second pointer)
        desc_a = luma + [0] + tsw
        f_path_a = Path(self.temp.name) / 'frame_vib_a.bin'
        f_path_a.write_bytes(make_frame_with_pds([0x10100000, 0], [(0x10100000, desc_a)]))
        frame_a = frame_module.Frame(f_path_a)
        tex_a = replay_module.texture(frame_a, [0x80004020])
        self.assertIsNotNone(tex_a)
        self.assertTrue(tex_a['vibrancy'], "LUMA at offset 0 must set vibrancy=True")

        # Case B: stand-alone TSW followed by the next allocation's luma.
        desc_b = tsw + luma + [0]
        f_path_b = Path(self.temp.name) / 'frame_vib_b.bin'
        f_path_b.write_bytes(make_frame_with_pds([0x10100000, 0], [(0x10100000, desc_b)]))
        frame_b = frame_module.Frame(f_path_b)
        tex_b = replay_module.texture(frame_b, [0x80004020])
        self.assertIsNotNone(tex_b)
        self.assertFalse(tex_b['vibrancy'], "Adjacent luma must not make an opaque draw translucent")

    def test_luma_in_another_pointer_does_not_taint_selected_texture(self):
        tsw = self._make_tsw(158, 48, 0x10200000)
        luma = [0x3d93a000, 0x3e59a000, 0x3f372000]
        path = Path(self.temp.name) / 'adjacent_pointer.bin'
        path.write_bytes(make_frame_with_pds(
            [0x10100000, 0, 0x10101000, 0],
            [(0x10100000, tsw), (0x10101000, luma + [0] + tsw)]))
        tex = replay_module.texture(frame_module.Frame(path), [0x80004020])
        self.assertIsNotNone(tex)
        self.assertFalse(tex['vibrancy'])

    def test_glyph_neighbour_hole_does_not_select_display_pitch(self):
        class Memory:
            pages = {0x10200000: bytes(4096), 0x10209000: bytes(4096)}
        tex = replay_module.decode_tsw(Memory(), self._make_tsw(106, 30))
        self.assertFalse(tex['strip'])
        self.assertEqual(tex['stride'], 448)
        # Even a full-width uploaded suballocation must be aligned to a
        # display strip before holes can imply display addressing.
        tex = replay_module.decode_tsw(Memory(), self._make_tsw(640, 32, 0x10200020))
        self.assertFalse(tex['strip'])
        tex = replay_module.decode_tsw(Memory(), self._make_tsw(640, 1136))
        self.assertTrue(tex['strip'])
        self.assertEqual(tex['stride'], 2560)

    def test_stride_12_blur_backdrop_no_texture(self):
        """A stride-12 draw whose PDS block has uniforms at 0 and next draw's TSW at +32 must get tex=None."""
        # Draw 0's uniform data is 4 words of 0s.
        # Draw 1's TSW is at +32 (offset 8 in words).
        tsw = self._make_tsw(158, 48, 0x10200000)
        desc = [0, 0, 0, 0, 0x00000fff, 0x00002400, 0, 0] + tsw

        f_path = Path(self.temp.name) / 'frame_blur_backdrop.bin'
        f_path.write_bytes(make_frame_with_pds([0x10100000, 0], [(0x10100000, desc)]))
        frame = frame_module.Frame(f_path)
        tex = replay_module.texture(frame, [0x80004020])
        self.assertIsNone(tex, "Stride-12 blur backdrop must not borrow next draw's TSW at offset +32")


if __name__ == '__main__':
    unittest.main()
