import sys, struct
from pathlib import Path
ROOT = Path(".").resolve()
sys.path.insert(0, str(ROOT / "backend"))
from agx_frame import Frame
import agx_replay_offline as aro

preamble = bytes.fromhex("2a0a949999af1b800033000080408001880020ff3a8a80949999af1b80003300008240800388002100")

blur_inventory = []

for scene_dir in sorted(p for p in Path("corpus").iterdir() if (p / "dumps").is_dir()):
    for dump in sorted((scene_dir / "dumps").glob("frame-*.bin")):
        try:
            f = Frame(dump)
            f.require_matching_ta()
        except:
            continue
        pw = (struct.unpack_from("<I", f.type1_command, 0x74)[0] & 0xffff) + 1
        ph = (struct.unpack_from("<I", f.type1_command, 0x70)[0] & 0xffff) + 1
        target = f.header.get("pbe_target")
        draws = list(aro.walk(f))
        for did, (vs_data, ppp, draw, vp) in enumerate(draws):
            prog = aro.program(f, ppp)
            stride, vs = aro.vertices(f, vs_data, draw[1])
            tex = aro.texture(f, ppp)
            words = aro.fragment_words(f, ppp)
            
            # Check for preamble in USC code
            try:
                off = f.u32(0x600000000 | ppp[7])
                code = f.read(0x700000000 | off, 64)
            except:
                code = b""
            has_preamble = code.startswith(preamble)
            
            # Check for weights in fragment words
            weights = None
            for ptr, ws in words:
                fl = [struct.unpack("<f", struct.pack("<I", w))[0] for w in ws]
                for start_i in range(len(fl)):
                    for end_i in range(start_i + 3, len(fl) + 1):
                        sub = fl[start_i:end_i]
                        # check if sub looks like normalized symmetric weights summing to ~0.5
                        if len(sub) in (3, 4, 5, 6, 7) and all(0.001 <= x <= 0.6 for x in sub) and abs(sum(sub) - 0.5) < 0.005:
                            weights = [round(x, 6) for x in sub]
                            break
                    if weights: break
                if weights: break

            # Check for offsets in vertex uniforms
            offsets = []
            direction = None
            raw_vsd = [f.u32(vs_data + 4 * i) for i in range(16)]
            for k in range(0, 16, 2):
                p = raw_vsd[k] | (raw_vsd[k+1] << 32)
                if 0x1000 <= p < (1 << 40):
                    try:
                        p_words = [f.u32(p + 4 * j) for j in range(48)]
                        offs = []
                        for j in range(16, len(p_words) - 1, 4):
                            ox = struct.unpack("<f", struct.pack("<I", p_words[j]))[0]
                            oy = struct.unpack("<f", struct.pack("<I", p_words[j+1]))[0]
                            if (abs(ox) > 0.05 and abs(oy) < 1e-4) or (abs(oy) > 0.05 and abs(ox) < 1e-4):
                                offs.append((round(ox, 6), round(oy, 6)))
                            else:
                                break
                        if len(offs) in (3, 4, 5, 6, 7):
                            offsets = offs
                            if all(oy == 0.0 for _, oy in offs):
                                direction = "horizontal"
                            elif all(ox == 0.0 for ox, _ in offs):
                                direction = "vertical"
                            break
                    except:
                        pass
            
            tex_str = f"{tex['w']}x{tex['h']}@{hex(tex['addr'])}" if tex else None
            # We are interested if:
            # 1. has_preamble (the stride-12 blur / backdrop draws)
            # 2. has weights (the textured blur draws)
            # 3. has direction/offsets
            if has_preamble or weights or direction:
                blur_inventory.append({
                    "scene": scene_dir.name,
                    "frame": dump.stem,
                    "did": did,
                    "count": draw[1],
                    "stride": stride,
                    "pw": pw,
                    "ph": ph,
                    "target": hex(target) if target else None,
                    "prog": hex(prog),
                    "has_preamble": has_preamble,
                    "weights_count": len(weights) if weights else 0,
                    "weights": weights,
                    "offsets_count": len(offsets),
                    "direction": str(direction),
                    "offsets": offsets[:4],
                    "tex": tex_str,
                })

print(f"Total blur-related draws in corpus: {len(blur_inventory)}")
from collections import Counter
print("Directions:", Counter(d["direction"] for d in blur_inventory))
print("Weights count:", Counter(d["weights_count"] for d in blur_inventory))
print("Has preamble:", Counter(d["has_preamble"] for d in blur_inventory))
print("Pass sizes:", Counter((d["pw"], d["ph"]) for d in blur_inventory))

# Group by (pw, ph, stride, direction, weights_count, has_preamble)
grouped = {}
for d in blur_inventory:
    key = (d["pw"], d["ph"], d["stride"], d["direction"], d["weights_count"], d["has_preamble"])
    grouped.setdefault(key, []).append(d)

print("\nGrouped draw archetypes:")
for key in sorted(grouped.keys(), key=lambda k: (k[0], k[1], k[2], str(k[3]), k[4], k[5])):
    items = grouped[key]
    pw, ph, stride, dirn, nweights, pre = key
    sample = items[0]
    print(f"\nPass {pw}x{ph}, stride={stride}, dir={dirn}, weights={nweights}, pre={pre} (count={len(items)}):")
    print(f"   Sample: {sample['scene']}/{sample['frame']} did={sample['did']} prog={sample['prog']} tex={sample['tex']}")
    if sample['weights']:
        print(f"   Weights ({len(sample['weights'])}): {sample['weights']}")
    if sample['offsets']:
        print(f"   Offsets ({len(sample['offsets'])}): {sample['offsets']}")
