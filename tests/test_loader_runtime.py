"""Loader memory correctness and fail-closed bundle/runtime regression coverage."""
import hashlib
import json
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from macho import inspect_macho
from bundle import validate_bundle, resolve_artifact
from patchers.patch_devicetree import DeviceTreeNode, serialize_node, parse_node
from qemu.boot_image import build_image, omit_unpopulated_devices, add_node, DRAM_BASE, RAM_SIZE, USABLE_RAM_SIZE, PRAM_BASE, PRAM_SIZE, STRAM_BASE, STRAM_SIZE, TIMEBASE_FREQUENCY, FRAMEBUFFER_SIZE, ANS_BASE, ANS_SIZE, KERNEL_BASE, MONITOR_BASE, MONITOR_SIZE, MONITOR_VIRT_BASE, SEP_SIZE, boot_args, make_chrp_partition, build_nvram_proxy_data, single_cpu_boot_args, escape_nvram_data, unescape_nvram_data
from qemu.runtime import QMP, stop, kernel_panic, PanicMonitor


def kernel_fixture(base=0xFFFFFF8000202000):
    commands = []
    for name, address, vmsize, off, size, prot in [
            (b"__TEXT", base, 0x800, 0, 0x600, 5),
            (b"__DATA", base + 0x2000, 0x1000, 0x600, 8, 3)]:
        commands.append(struct.pack("<II16s4Q4I", 0x19, 72, name, address, vmsize, off, size, prot, prot, 0, 0))
    registers = bytearray(272)
    struct.pack_into("<Q", registers, 32 * 8, base + 0x500)
    commands.append(struct.pack("<4I", 5, 288, 6, 68) + registers)
    table = b"".join(commands)
    value = bytearray(0x608)
    value[:32] = struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 2, len(commands), len(table), 0, 0)
    value[32:32 + len(table)] = table
    value[0x500:0x504] = b"\xc0\x03\x5f\xd6"
    value[0x600:] = b"DATAREAL"
    return bytes(value)


def tree_fixture(compatible=b"N53AP\0iPhone6,2\0"):
    root = DeviceTreeNode()
    root.set_prop("name", b"device-tree\0")
    root.set_prop("model", b"iPhone6,2\0")
    root.set_prop("compatible", compatible)
    defaults = DeviceTreeNode()
    defaults.set_prop("name", b"defaults\0")
    defaults.set_prop("monitor-size", struct.pack("<I", MONITOR_SIZE))
    defaults.set_prop("sep-size", struct.pack("<I", SEP_SIZE))
    root.children.append(defaults)
    cpus = DeviceTreeNode()
    cpus.set_prop("name", b"cpus\0")
    for index in range(2):
        cpu = DeviceTreeNode()
        cpu.set_prop("name", f"cpu{index}\0".encode())
        cpu.set_prop("reg", struct.pack("<I", index))
        cpu.set_prop("timebase-frequency", bytes(8))
        cpu.set_prop("function-enable_core", b"\0\0\0\x11eroC" + struct.pack("<I", 1 << index))
        cpus.children.append(cpu)
    root.children.append(cpus)
    for name in ("pmgr", "arm-io"):
        node = DeviceTreeNode()
        node.set_prop("name", name.encode() + b"\0")
        root.children.append(node)
    return serialize_node(root)


class LoaderTests(unittest.TestCase):
    def test_absent_devices_in_built_dt_and_ram_leave_input_untouched(self):
        root, _ = parse_node(tree_fixture())
        modem = add_node(root, "baseband")
        modem.set_prop("compatible", b"baseband,n41\0")
        modem.set_prop("class", struct.pack("<I", 3))
        arm = root.find_node("arm-io")
        uart = add_node(arm, "uart2")
        uart.set_prop("interrupts", struct.pack("<I", 142))
        oscar = add_node(uart, "oscar")
        oscar.set_prop("compatible", b"apple-oscar\0oscar\0")
        for name in ("gyro", "accelerometer", "compass", "cma"):
            add_node(oscar, name)
        ehci = add_node(add_node(arm, "usb-complex"), "usb-ehci0")
        add_node(ehci, "hsic-baseband")
        # Unrelated devices and even equal names at other paths must survive.
        add_node(ehci, "hsic-wlan")
        pmu = add_node(add_node(arm, "i2c0"), "pmu")
        pmu.set_prop("event_name-gpio2", b"baseband\0")
        add_node(add_node(arm, "mca1"), "audio-baseband-voice")
        add_node(add_node(arm, "uart1"), "bluetooth")
        add_node(add_node(root, "unrelated"), "oscar")
        original = serialize_node(root)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "kernel").write_bytes(kernel_fixture())
            (path / "monitor").write_bytes(kernel_fixture(MONITOR_VIRT_BASE))
            (path / "tree").write_bytes(original)
            plan = build_image(path / "kernel", path / "tree", path / "boot",
                               "-v cpus=2", monitor_path=path / "monitor")
            raw = (path / "boot/DeviceTree.runtime").read_bytes()
            built, _ = parse_node(raw)
            # Baseband nubs stay: CommCenter needs them (probe-132 regression).
            self.assertIsNotNone(built.find_node("baseband"))
            self.assertIsNotNone(built.find_node("hsic-baseband"))
            self.assertEqual(built.find_node("uart2").children, [])
            self.assertEqual(built.find_node("uart2").get_prop("interrupts"),
                             struct.pack("<I", 142))
            for name in ("hsic-wlan", "audio-baseband-voice", "bluetooth", "oscar"):
                self.assertIsNotNone(built.find_node(name))
            self.assertEqual(built.find_node("pmu").get_prop("event_name-gpio2"),
                             b"baseband\0")
            ram = Path(plan["image"]).read_bytes()
            dt_off = plan["devicetree_pa"] - DRAM_BASE
            self.assertEqual(ram[dt_off:dt_off + len(raw)], raw)
            self.assertEqual((path / "tree").read_bytes(), original)
            omit_unpopulated_devices(built)
            self.assertEqual(serialize_node(built), raw)

    def test_absent_device_filter_accepts_missing_parent_paths(self):
        root, _ = parse_node(tree_fixture())
        original = serialize_node(root)
        omit_unpopulated_devices(root)
        self.assertEqual(serialize_node(root), original)

    def test_nvram_partitions_make_forward_progress(self):
        data = build_nvram_proxy_data("-v cpus=1")
        self.assertEqual(len(data), 8192)
        offset, names = 0, []
        while offset < len(data):
            header = data[offset:offset + 16]
            size = struct.unpack_from("<H", header, 2)[0] * 16
            self.assertGreaterEqual(size, 16)
            self.assertLessEqual(offset + size, len(data))
            names.append(header[4:].rstrip(b"\0"))
            offset += size
        self.assertEqual(names, [b"common", b"APL,MacOS75", b"wwwwwwwwwwww"])
        self.assertIn(b"boot-args=-v cpus=1\0", data)
        for size in (0, 15, 17, 0x100000):
            with self.assertRaises(ValueError):
                make_chrp_partition(0x70, "common", size)
        with self.assertRaises(ValueError):
            make_chrp_partition(0x70, "common", 16, b"oversized")

    def test_nvram_art_persistence_contract(self):
        # 1. Absent ART on first boot: NVRAM contains only boot-args, no sep.art.
        first_boot_data = build_nvram_proxy_data("-v cpus=1")
        self.assertEqual(len(first_boot_data), 8192)
        self.assertNotIn(b"com.apple.System.sep.art", first_boot_data)
        self.assertIn(b"boot-args=-v cpus=1\0", first_boot_data)

        # 2. Valid persisted ART contract: arbitrary ASN.1/binary tokens with 0x00 and 0xFF
        # are safely run-length escaped and roundtripped through IODTNVRAM encoding.
        sample_art = b"\x30\x82\x01\x20\x02\x01\x01\x04\x20" + bytes(range(32)) + b"\x00\x00\x00\xff\xff\x04\x10" + bytes(16)
        escaped_art = escape_nvram_data(sample_art)
        self.assertNotIn(b"\x00", escaped_art, "Escaped NVRAM binary data must not contain NUL bytes")
        self.assertEqual(unescape_nvram_data(escaped_art), sample_art)

        # Persisted NVRAM partition contains the escaped property in common partition.
        persisted_data = build_nvram_proxy_data("-v cpus=1", {"com.apple.System.sep.art": sample_art})
        self.assertEqual(len(persisted_data), 8192)
        self.assertIn(b"com.apple.System.sep.art=" + escaped_art + b"\x00", persisted_data)

        # 3. Invalid / truncated escape content fails decoding explicitly.
        with self.assertRaises(ValueError):
            unescape_nvram_data(b"abc\xff")  # Truncated lone 0xFF
        with self.assertRaises(ValueError):
            unescape_nvram_data(b"abc\xff\x00def")  # Invalid zero-length run
        with self.assertRaises(ValueError):
            build_nvram_proxy_data("-v", {"invalid=name": b"data"})  # '=' in key
        with self.assertRaises(ValueError):
            build_nvram_proxy_data("-v", {"com.apple.System.sep.art": b"x" * 5000})  # Partition overflow

        # 4. CHRP structure & checksum invariance across boots.
        offset = 0
        names = []
        while offset < len(persisted_data):
            hdr = persisted_data[offset:offset + 16]
            sig, csum, blocks = struct.unpack_from("<BBH", hdr)
            size = blocks * 16
            self.assertGreaterEqual(size, 16)
            names.append(hdr[4:].rstrip(b"\0"))
            calc_csum = 0
            for i, b in enumerate(hdr):
                if i == 1:
                    continue
                isum = (calc_csum + b) & 0xff
                if isum < calc_csum:
                    isum = (isum + 1) & 0xff
                calc_csum = isum
            self.assertEqual(csum, calc_csum)
            offset += size
        self.assertEqual(names, [b"common", b"APL,MacOS75", b"wwwwwwwwwwww"])

    def test_build_image_with_persisted_art_contract(self):
        kernel = kernel_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "kernel").write_bytes(kernel)
            (path / "tree").write_bytes(tree_fixture())
            monitor = kernel_fixture(MONITOR_VIRT_BASE)
            (path / "monitor").write_bytes(monitor)
            sample_art = b"\x30\x81\xa0\x02\x01\x00\x31\x59\x02\x01\x01" + bytes(32)
            plan = build_image(path / "kernel", path / "tree", path / "boot", monitor_path=path / "monitor",
                               nvram_vars={"com.apple.System.sep.art": sample_art})
            mem = Path(plan["image"]).read_bytes()
            dt_off = plan["devicetree_pa"] - DRAM_BASE
            dt_data = mem[dt_off:]
            self.assertIn(b"com.apple.System.sep.art=" + escape_nvram_data(sample_art), dt_data)

    def test_cpu_limit_is_explicit_and_conflicts_fail(self):
        self.assertEqual(single_cpu_boot_args("-v"), "-v cpus=1")
        self.assertEqual(single_cpu_boot_args("-v cpus=1"), "-v cpus=1")
        self.assertEqual(single_cpu_boot_args("-v cpus=2"), "-v cpus=2")
        for args in ("cpus=0", "cpus=3", "cpus=1 cpus=1", "cpus=1 cpus=2"):
            with self.assertRaises(ValueError):
                single_cpu_boot_args(args)

    def test_two_cpu_image_preserves_core_ids_and_enable_functions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "kernel").write_bytes(kernel_fixture())
            (path / "tree").write_bytes(tree_fixture())
            (path / "monitor").write_bytes(kernel_fixture(MONITOR_VIRT_BASE))
            plan = build_image(path / "kernel", path / "tree", path / "boot",
                               "-v cpus=2", monitor_path=path / "monitor")
            self.assertEqual(plan["cpu_count"], 2)
            self.assertIn("cpus=2", plan["boot_args"])
            root, _ = parse_node((path / "boot" / "DeviceTree.runtime").read_bytes(), 0)
            cores = root.find_node("cpus").children
            self.assertEqual(len(cores), 2)
            for index, cpu in enumerate(cores):
                self.assertEqual(cpu.get_prop("reg"), struct.pack("<I", index))
                self.assertEqual(cpu.get_prop("state"), b"off\0" if index else b"running\0")
                self.assertEqual(cpu.get_prop("function-enable_core"),
                                 b"\0\0\0\x11eroC" + struct.pack("<I", 1 << index))

    def test_segment_mapping_bss_and_bootargs(self):
        kernel = kernel_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "kernel").write_bytes(kernel)
            (path / "tree").write_bytes(tree_fixture())
            monitor = kernel_fixture(MONITOR_VIRT_BASE)
            (path / "monitor").write_bytes(monitor)
            plan = build_image(path / "kernel", path / "tree", path / "boot", monitor_path=path / "monitor")
            memory = Path(plan["image"]).read_bytes()
            image = inspect_macho(kernel)
            text, data = image["segments"]
            text_off = KERNEL_BASE - DRAM_BASE + text["vmaddr"] - plan["virt_base"]
            self.assertEqual(memory[text_off:text_off + 0x600], kernel[:0x600])
            off = KERNEL_BASE - DRAM_BASE + data["vmaddr"] - plan["virt_base"]
            self.assertEqual(memory[off:off + 8], b"DATAREAL")
            self.assertEqual(memory[off + 8:off + data["vmsize"]], bytes(data["vmsize"] - 8))
            args_off = plan["boot_args_pa"] - DRAM_BASE
            args = memory[args_off:args_off + 736]
            self.assertEqual(struct.unpack_from("<4Q", args, 8), (plan["virt_base"], KERNEL_BASE, USABLE_RAM_SIZE, plan["top_of_kernel_data"]))
            self.assertEqual(struct.unpack_from("<Q", args, 728)[0], RAM_SIZE)
            self.assertGreater(plan["framebuffer_pa"], plan["devicetree_pa"])
            self.assertLessEqual(plan["framebuffer_pa"] + FRAMEBUFFER_SIZE, plan["top_of_kernel_data"])
            self.assertLess(plan["top_of_kernel_data"], PRAM_BASE)
            self.assertLess(plan["framebuffer_pa"] + FRAMEBUFFER_SIZE, ANS_BASE)
            self.assertEqual(plan["ans_reserved"], dict(address=ANS_BASE, size=ANS_SIZE,
                                                       runtime_image_prepared=False,
                                                       execution_verified=False))
            self.assertEqual(STRAM_BASE, KERNEL_BASE + USABLE_RAM_SIZE)
            self.assertEqual(PRAM_BASE + PRAM_SIZE, STRAM_BASE)
            self.assertLess(STRAM_BASE + STRAM_SIZE, ANS_BASE)
            self.assertLess(plan["top_of_kernel_data"], PRAM_BASE)
            tree, _ = parse_node((path / "boot/DeviceTree.runtime").read_bytes(), 0)
            bridge = tree.find_node("pmgr").get_prop("bridge-settings")
            self.assertEqual(len(bridge), 776)
            self.assertEqual(hashlib.sha256(bridge).hexdigest(), "7090b9a4a8391290387e22cc06548a8de8d6bfc93295f8e9bed717563922fa25")
            self.assertEqual(struct.unpack("<QQ", tree.find_node("pram").get_prop("reg")), (PRAM_BASE, PRAM_SIZE))
            self.assertEqual(struct.unpack("<QQ", tree.find_node("stram").get_prop("reg")), (STRAM_BASE, STRAM_SIZE))
            self.assertEqual(struct.unpack("<Q", tree.find_node("cpu0").get_prop("timebase-frequency")), (TIMEBASE_FREQUENCY,))
            self.assertEqual(struct.unpack_from("<Q", args, 96)[0], plan["virt_base"] + plan["devicetree_pa"] - KERNEL_BASE)
            self.assertEqual(plan["entry_pa"], KERNEL_BASE + image["entry_va"] - plan["virt_base"])
            self.assertEqual(plan["boot_el"], 3)
            mon_offset = MONITOR_BASE - DRAM_BASE
            self.assertEqual(memory[mon_offset:mon_offset + 0x600], monitor[:0x600])
            self.assertEqual(memory[mon_offset + 0x2008:mon_offset + 0x3000], bytes(0xff8))
            mon_args_offset = plan["initial_boot_args_pa"] - DRAM_BASE
            self.assertEqual(struct.unpack_from("<6Q", memory, mon_args_offset), (1, MONITOR_VIRT_BASE, MONITOR_BASE, MONITOR_SIZE, plan["boot_args_pa"], plan["entry_pa"]))
            self.assertEqual(plan["initial_entry_pa"], MONITOR_BASE + 0x500)

    def test_reject_wrong_board_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "kernel").write_bytes(kernel_fixture())
            (path / "tree").write_bytes(tree_fixture(b"NOTN53AP\0"))
            (path / "monitor").write_bytes(kernel_fixture(MONITOR_VIRT_BASE))
            with self.assertRaisesRegex(ValueError, "n53ap"):
                build_image(path / "kernel", path / "tree", path / "boot", monitor_path=path / "monitor")

    def test_native_monitor_is_required(self):
        with self.assertRaisesRegex(ValueError, "Native secure monitor is required"):
            build_image("missing", "missing", "unused")

    def test_reject_truncated_or_overlapping_macho(self):
        with self.assertRaises(ValueError):
            inspect_macho(kernel_fixture()[:100])
        overlap = bytearray(kernel_fixture())
        struct.pack_into("<Q", overlap, 32 + 72 + 24, 0xFFFFFF8000202100)
        with self.assertRaisesRegex(ValueError, "Overlapping"):
            inspect_macho(overlap)

    def test_reject_command_line_overflow(self):
        with self.assertRaises(ValueError):
            boot_args(0, 0, 0, 0, "x" * 608, 0)
        with self.assertRaises(ValueError):
            boot_args(0, 0, 0, 0, "abc\0def", 0)


class DoctorTests(unittest.TestCase):
    def test_malformed_and_legacy_manifests_return_errors(self):
        for manifest in [[], {}, {"schema_version": 1}, {"schema_version": 2, "version": "9.3.5"}]:
            with self.subTest(manifest=manifest), tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / "vm_manifest.json").write_text(json.dumps(manifest))
                report = validate_bundle(tmp, False)
                self.assertFalse(report["prepared"])
                self.assertFalse(report["bootable"])
                self.assertTrue(report["errors"])

    def test_reject_artifact_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                resolve_artifact(tmp, {"path": "../kernel"})

    def test_no_shutdown_claim_when_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "runtime.json").write_text(json.dumps({"process": {"phase": "running"}, "capabilities": {}}))
            with self.assertRaisesRegex(ValueError, "shutdown is unavailable"):
                stop(tmp)

    def test_stale_runtime_cannot_stop_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "runtime.json").write_text(json.dumps({"process": {"phase": "running"}}))
            (path / "runtime.lock").touch()
            with self.assertRaisesRegex(ValueError, "stale"):
                stop(tmp, True)


class QMPTests(unittest.TestCase):
    def test_native_panic_after_unterminated_driver_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "serial"
            monitor = PanicMonitor()
            path.write_bytes(b"AppleDialogPMU: failed count 1panic(cpu 0 caller 0xffffff80010a73a0): ANS invalid\n")
            self.assertFalse(monitor.poll(path))
            with path.open("ab") as stream:
                stream.write(b"\rDebugger message: pan")
            self.assertFalse(monitor.poll(path))
            with path.open("ab") as stream:
                stream.write(b"ic\n" + b"backtrace\n" * 20000)
            self.assertTrue(monitor.poll(path))
            self.assertTrue(kernel_panic(path))

    def test_panic_survives_large_backtrace_and_partial_uart_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "serial"
            monitor = PanicMonitor()
            path.write_bytes(b"\n\rpanic(cpu 0 caller 0xffffff80010a7044")
            self.assertFalse(monitor.poll(path))
            with path.open("ab") as stream:
                stream.write(b"): ANS firmware failure\n" + b"backtrace\n" * 20000)
            self.assertTrue(monitor.poll(path))
            self.assertTrue(kernel_panic(path))
            self.assertTrue(monitor.poll(path))

    def test_panic_header_is_failure_but_boot_words_are_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "serial"
            self.assertFalse(kernel_panic(path))
            for text in (b"Darwin SpringBoard launchd ready\n", b"app: panic(cpu 0 caller 0x123): quoted\n", b"panic(cpu 0 caller "):
                path.write_bytes(text)
                self.assertFalse(kernel_panic(path))
            path.write_bytes(b"\n\rpanic(cpu 0 caller 0xffffff800022194c): Unexpected abort\n")
            self.assertTrue(kernel_panic(path))

    def test_partial_frames_events_and_matching_request_ids(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            path = str(Path(tmp) / "qmp")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(path)
            server.listen(1)
            errors = []
            def run():
                try:
                    client, _ = server.accept()
                    with client, client.makefile("rwb", buffering=0) as stream:
                        client.sendall(b'{"QM')
                        client.sendall(b'P": {}}\r\n')
                        for command in ("qmp_capabilities", "query-status"):
                            req = json.loads(stream.readline())
                            if req["execute"] != command:
                                raise AssertionError(req)
                            stream.write(b'{"event":"STOP"}\n')
                            stream.write(json.dumps({"return": {"status": "paused"}, "id": req["id"]}).encode() + b"\n")
                except Exception as exc:
                    errors.append(exc)
            worker = threading.Thread(target=run)
            worker.start()
            qmp = QMP(path)
            try:
                self.assertEqual(qmp.execute("query-status"), {"status": "paused"})
            finally:
                qmp.close()
                worker.join(timeout=5)
                server.close()
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
