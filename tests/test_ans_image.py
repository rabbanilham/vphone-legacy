"""Native ANS handoff integrity; these tests do not certify IOP/storage boot."""
import hashlib
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from qemu.ans_image import prepare_ans_region, ANS_BASE, ANS_SIZE, CONFIG_OFFSET
from qemu.boot_image import build_image
from patchers.patch_devicetree import parse_node, serialize_node, DeviceTreeNode
from test_loader_runtime import kernel_fixture, tree_fixture, MONITOR_VIRT_BASE


class ANSImageTests(unittest.TestCase):
    def test_unknown_payload_fails_before_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "firmware"
            source.write_bytes(bytes(0x39000))
            with self.assertRaisesRegex(ValueError, "verified native"):
                prepare_ans_region(source, root, [24000000] * 61)
            self.assertFalse((root / "ANSRegion.bin").exists())

    @unittest.skipUnless(os.environ.get("VPHONE_REAL_FIRMWARE_TEST"), "opt-in native ANS firmware")
    def test_real_fixups_bss_and_guest_tree_stay_consistent(self):
        source = Path(__file__).resolve().parents[1] / "build/ios712-validated/ans-firmware.bin"
        original = source.read_bytes()
        clocks = [0] * 61
        clocks[0], clocks[41] = 24000000, 133333333
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = prepare_ans_region(source, root, clocks)
            region = Path(record["path"]).read_bytes()
            self.assertEqual(len(region), ANS_SIZE)
            self.assertEqual(record["address"], ANS_BASE)
            self.assertEqual(record["sha256"], hashlib.sha256(region).hexdigest())
            self.assertEqual(struct.unpack_from("<QI", region, CONFIG_OFFSET + 0x20), (0x99000, 0x400000))
            self.assertEqual(struct.unpack_from("<4I", region, CONFIG_OFFSET + 0x40), (0x8960, 0, 24000000, 133333333))
            restored = bytearray(region)
            for fixup in record["fixups"]:
                start = fixup["offset"]
                restored[start:start + fixup["width"]] = bytes.fromhex(fixup["before"])
            self.assertEqual(restored[:len(original)], original)
            self.assertFalse(any(restored[len(original):]))
            self.assertEqual(source.read_bytes(), original)
            self.assertFalse(record["execution_verified"])
            with self.assertRaisesRegex(ValueError, "clocks"):
                prepare_ans_region(source, root, [0] * 61)
            tree, _ = parse_node(tree_fixture(), 0)
            ans = DeviceTreeNode()
            ans.set_prop("name", b"ans\0")
            ans.set_prop("region-base", bytes(8))
            ans.set_prop("region-size", bytes(4))
            tree.children.append(ans)
            (root / "tree").write_bytes(serialize_node(tree))
            (root / "kernel").write_bytes(kernel_fixture())
            (root / "monitor").write_bytes(kernel_fixture(MONITOR_VIRT_BASE))
            plan = build_image(root / "kernel", root / "tree", root / "boot",
                               monitor_path=root / "monitor", ans_path=source)
            actual_tree, _ = parse_node((root / "boot/DeviceTree.runtime").read_bytes(), 0)
            self.assertEqual(struct.unpack("<Q", actual_tree.find_node("ans").get_prop("region-base")), (ANS_BASE,))
            self.assertEqual(struct.unpack("<Q", actual_tree.find_node("ans").get_prop("region-size")), (ANS_SIZE,))
            args = (root / "boot/boot_args.bin").read_bytes()
            self.assertEqual(struct.unpack_from("<I", args, 104)[0], (root / "boot/DeviceTree.runtime").stat().st_size)
            self.assertEqual(len(plan["auxiliary_images"]), 1)
            self.assertTrue(plan["ans_reserved"]["runtime_image_prepared"])
            self.assertFalse(plan["ans_reserved"]["execution_verified"])
            self.assertLess(plan["top_of_kernel_data"], ANS_BASE)


if __name__ == "__main__":
    unittest.main()
