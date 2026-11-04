#!/usr/bin/env python3
"""Offline, recursive plan and byte-hash comparison. Exit 1 for any difference."""
import argparse
import json
from pathlib import Path
from bundle import digest


def field_diff(left, right, path='$'):
    if isinstance(left, dict) and isinstance(right, dict):
        return [d for key in sorted(left.keys() | right.keys())
                for d in (field_diff(left[key], right[key], path + '.' + key)
                          if key in left and key in right else
                          [dict(field=path + '.' + key, left=left.get(key), right=right.get(key))])]
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return [dict(field=path, left=left, right=right)]
        return [d for i, pair in enumerate(zip(left, right)) for d in field_diff(*pair, f'{path}[{i}]')]
    return [] if left == right else [dict(field=path, left=left, right=right)]


def tree_fields(path):
    from patchers.patch_devicetree import parse_node
    def walk(node, parent=""):
        name = (node.get_prop("name") or b"").rstrip(b"\0").decode(errors="replace")
        parent += "/" + name
        result = {parent + ":" + key: dict(size=len(node.get_prop(key)),
                  sha256=__import__('hashlib').sha256(node.get_prop(key)).hexdigest())
                  for key in node.properties}
        for child in node.children:
            result.update(walk(child, parent))
        return result
    return walk(parse_node(Path(path).read_bytes(), 0)[0])


def compare(candidate, oracle):
    candidate, oracle = Path(candidate), Path(oracle)
    result = dict(plan_diff=field_diff(json.loads((candidate / 'boot_plan.json').read_text()),
                                      json.loads((oracle / 'boot_plan.json').read_text())), hashes={})
    for name in ('KernelMemory.bin', 'DeviceTree.runtime', 'boot_args.bin'):
        a, b = digest(candidate / name), digest(oracle / name)
        result['hashes'][name] = dict(candidate=a, oracle=b, identical=a == b)
    plans = [json.loads((root / 'boot_plan.json').read_text()) for root in (candidate, oracle)]
    images = [(root / 'KernelMemory.bin').read_bytes() for root in (candidate, oracle)]
    tree_lengths = [(root / 'DeviceTree.runtime').stat().st_size for root in (candidate, oracle)]
    plan = plans[0]
    ba = plan['boot_args_pa'] - plan['dram_base']
    dt = plan['devicetree_pa'] - plan['dram_base']
    regions = {'boot_args': (ba, ba + 736), 'devicetree': (dt, dt + max(tree_lengths))}
    result['memory_difference_counts'] = {name: sum(a != b for a, b in zip(images[0][lo:hi], images[1][lo:hi]))
                                          for name, (lo, hi) in regions.items()}
    result['memory_difference_counts']['outside_boot_args_and_devicetree'] = sum(
        a != b and not any(lo <= i < hi for lo, hi in regions.values())
        for i, (a, b) in enumerate(zip(*images)))
    result['memory_lengths'] = [len(value) for value in images]
    result['devicetree_diff'] = field_diff(tree_fields(candidate / 'DeviceTree.runtime'),
                                           tree_fields(oracle / 'DeviceTree.runtime'))
    result['identical'] = not result['plan_diff'] and all(x['identical'] for x in result['hashes'].values())
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('candidate', type=Path)
    p.add_argument('oracle', type=Path)
    a = p.parse_args()
    report = compare(a.candidate, a.oracle)
    print(json.dumps(report, indent=2))
    return 0 if report['identical'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
