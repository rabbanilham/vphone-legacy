"""Home Clock dirty redraw: native fixture and offline mirror share pixel oracles."""
import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import agx_contract_test as contract
from agx_frame import Frame
import agx_replay_offline as replay


def fixture_frame(blob):
    """Walk the contract's real GPU page tables, without starting a guest."""
    def physical(pa, n):
        off = pa - contract.DATA
        if off < 0 or off + n > len(blob):
            raise KeyError(pa)
        return blob[off:off + n]
    def word(pa, size):
        return int.from_bytes(physical(pa, size), 'little')
    frame = Frame.__new__(Frame)
    frame.pages, frame.physical = {}, {}
    for ci in range(1024):
        entry = word(contract.CTX_PC + ci * 4, 4)
        if not entry:
            continue
        pd = (entry >> 4) << 12
        for di in range(512):
            pt = word(pd + di * 8, 8) & ~4095
            if not pt:
                continue
            for ti in range(512):
                pa = word(pt + ti * 8, 8) & ~4095
                if pa:
                    gva = ci << 30 | di << 21 | ti << 12
                    frame.pages[gva] = physical(pa, 4096)
                    frame.physical[gva] = pa
    frame.type1_command = physical(contract.QUEUE + 0x300, 0x280)
    frame.ta_command = physical(contract.QUEUE + 0x900, 0x280)
    frame.header = {'pbe_target': contract.REPLAY_TARGET, 'flags': 1}
    return frame


class HomeDirtyTests(unittest.TestCase):
    def test_fullscreen_wallpaper_preserves_static_home_content(self):
        for case in ('home-dirty-scissor', 'home-dirty-region'):
            with self.subTest(case=case):
                blob, _, expected, (w, h), _, _ = contract.replay_blob(case)
                frame = fixture_frame(blob)
                result = []
                with patch.object(replay, 'Frame', return_value=frame), \
                     patch.object(sys, 'argv', ['replay', 'fixture', 'unused.png']), \
                     patch.object(replay, 'png', side_effect=lambda path, w, h, data: result.append(data)), \
                     contextlib.redirect_stdout(io.StringIO()):
                    replay.main()
                rgba = bytearray(expected[:w * h * 4])
                rgba[0::4], rgba[2::4] = rgba[2::4], rgba[0::4]
                self.assertEqual(result, [bytes(rgba)])

    def test_oracle_exposes_old_unclipped_behavior(self):
        blob, _, expected, (w, h), _, _ = contract.replay_blob('home-dirty-scissor')
        result = []
        with patch.object(replay, 'Frame', return_value=fixture_frame(blob)), \
             patch.object(sys, 'argv', ['replay', 'fixture', 'unused.png']), \
             patch.object(replay, 'decode_ppp_clipping', return_value={
                 'scenable': False, 'has_rc': False}), \
             patch.object(replay, 'png', side_effect=lambda path, w, h, data: result.append(data)), \
             contextlib.redirect_stdout(io.StringIO()):
            replay.main()
        self.assertEqual(result, [bytes((0x12, 0x34, 0x56, 255)) * (w * h)])
        self.assertNotEqual(result[0][:4], expected[:4])

    def test_partial_ppp_update_retains_dirty_region(self):
        state = replay.decode_ppp_clipping([0x421, 1 << 16, 7, 0x80060007, 0x00080009])
        state = replay.decode_ppp_clipping([1 << 5, 8], state)
        self.assertTrue(state['scenable'])
        self.assertEqual(state['scindex'], 8)
        self.assertEqual(state['rc_box'], (192, 256, 256, 320))
        state = replay.decode_ppp_clipping([1 << 10, 0, 0], state)
        self.assertFalse(state['has_rc'])


if __name__ == '__main__':
    unittest.main()
