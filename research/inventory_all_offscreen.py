import sys, struct
from pathlib import Path
ROOT = Path(".").resolve()
sys.path.insert(0, str(ROOT / "backend"))
from agx_frame import Frame
import agx_replay_offline as aro

offscreen_inventory = []

for scene_dir in sorted(p for p in Path("corpus").iterdir() if (p / "dumps").is_dir()):
    for dump in sorted((scene_dir / "dumps").glob("frame-*.bin")):
        try:
            f = Frame(dump)
            f.require_matching_ta()
        except:
            continue
        pw = (struct.unpack_from("<I", f.type1_command, 0x74)[0] & 0xffff) + 1
        ph = (struct.unpack_from("<I", f.type1_command, 0x70)[0] & 0xffff) + 1
        if pw == 640 and ph == 1136:
            continue
        target = f.header.get("pbe_target")
        draws = list(aro.walk(f))
        pass_info = {
            "scene": scene_dir.name,
            "frame": dump.stem,
            "pw": pw,
            "ph": ph,
            "target": hex(target) if target else None,
            "draws": [],
        }
        for did, (vs_data, ppp, draw, vp) in enumerate(draws):
            prog = aro.program(f, ppp)
            stride, vs = aro.vertices(f, vs_data, draw[1])
            words = aro.fragment_words(f, ppp)
            tex = aro.texture(f, ppp)
            # Find any float arrays (taps/weights)
            all_weights = []
            for ptr, ws in words:
                fl = [struct.unpack("<f", struct.pack("<I", w))[0] for w in ws]
                for start_i in range(len(fl)):
                    for end_i in range(start_i + 3, len(fl) + 1):
                        sub = fl[start_i:end_i]
                        if all(0.0 <= x <= 1.0 for x in sub) and (abs(sum(sub) - 0.5) < 0.01 or abs(sum(sub) - 1.0) < 0.01):
                            all_weights.append((start_i, [round(x, 4) for x in sub]))
            # Also check if any TSW exists in words even if aro.texture missed it
            tsw_list = []
            for ptr, ws in words:
                for k in range(len(ws) - 3):
                    if (ws[k] >> 28) == 6:
                        t = aro.decode_tsw(f, ws[k:k+4])
                        if t:
                            tsw_list.append((k, f"{t['w']}x{t['h']}@{hex(t['addr'])}"))
            pass_info["draws"].append({
                "did": did,
                "count": draw[1],
                "stride": stride,
                "prog": hex(prog),
                "tex": f"{tex['w']}x{tex['h']}@{hex(tex['addr'])}" if tex else None,
                "tsws": tsw_list,
                "weights": all_weights,
            })
        offscreen_inventory.append(pass_info)

print(f"Total offscreen passes: {len(offscreen_inventory)}")
# Print distinct sequences of passes
scenes = set(p["scene"] for p in offscreen_inventory)
for sc in sorted(scenes):
    print(f"\n=== Scene {sc} ===")
    sc_passes = [p for p in offscreen_inventory if p["scene"] == sc]
    for p in sc_passes[:15]:
        print(f"  {p['frame']}: {p['pw']}x{p['ph']} target={p['target']} draws={len(p['draws'])}")
        for d in p["draws"]:
            print(f"     did={d['did']} count={d['count']} stride={d['stride']} prog={d['prog']} tex={d['tex']} tsws={d['tsws']} weights={d['weights']}")
