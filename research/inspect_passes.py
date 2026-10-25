import sys, struct
from pathlib import Path
ROOT = Path(".").resolve()
sys.path.insert(0, str(ROOT / "backend"))
from agx_frame import Frame
import agx_replay_offline as aro

def inspect_frame(path):
    f = Frame(path)
    f.require_matching_ta()
    pw = (struct.unpack_from("<I", f.type1_command, 0x74)[0] & 0xffff) + 1
    ph = (struct.unpack_from("<I", f.type1_command, 0x70)[0] & 0xffff) + 1
    target = f.header.get("pbe_target")
    print(f"=== {path.name}: {pw}x{ph} target={hex(target) if target else None} ===")
    for did, (vs_data, ppp, draw, vp) in enumerate(aro.walk(f)):
        prog = aro.program(f, ppp)
        stride, vs = aro.vertices(f, vs_data, draw[1])
        tex = aro.texture(f, ppp)
        print(f" draw {did}: count={draw[1]} stride={stride} prog={hex(prog)} tex={tex is not None}")
        if tex:
            print(f"    tex: {tex['w']}x{tex['h']} addr={hex(tex['addr'])} strip={tex['strip']} stride={tex['stride']}")
        # Check all possible TSWs in fragment words
        words = aro.fragment_words(f, ppp)
        for ptr, ws in words:
            # Look for floats (weights, texel sizes, direction)
            floats = [struct.unpack("<f", struct.pack("<I", w))[0] for w in ws]
            good_floats = [(i, round(fl, 6)) for i, fl in enumerate(floats) if (abs(fl) < 100.0 and abs(fl) > 1e-6) or fl == 0.0]
            print(f"    ptr {hex(ptr)}: {len(ws)} words, floats: {good_floats}")
            # Check TSW candidates in ws
            for k in range(len(ws) - 3):
                if (ws[k] >> 28) == 6:
                    tsw_cand = aro.decode_tsw(f, ws[k:k+4])
                    if tsw_cand:
                        print(f"      TSW at word {k}: {tsw_cand['w']}x{tsw_cand['h']} addr={hex(tsw_cand['addr'])}")

print("--- Settings 128x64 switch shadow (frames 27..29) ---")
for fnum in (27, 28, 29):
    inspect_frame(Path(f"corpus/settings-all/dumps/frame-{fnum}.bin"))

print("\n--- Settings 640x64 navbar (frames 10..13) ---")
for fnum in (10, 11, 12, 13):
    inspect_frame(Path(f"corpus/settings-all/dumps/frame-{fnum}.bin"))
