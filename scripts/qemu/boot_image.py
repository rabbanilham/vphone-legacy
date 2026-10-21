#!/usr/bin/env python3
"""Place Mach-O segments, Apple DeviceTree and boot_args in physical guest RAM.

Native secure-monitor/kernel loader, not iBoot. It synthesizes neither guest
code nor boot logs.
ABI evidence and bring-up limitations are recorded in research/bringup.md.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from macho import inspect_macho
from patchers.patch_devicetree import DeviceTreeNode, parse_node, serialize_node

DRAM_BASE = 0x800000000
RAM_SIZE = 1024 * 1024 * 1024
SEP_SIZE = 6 * 1024 * 1024
MONITOR_SIZE = 2 * 1024 * 1024
MONITOR_BASE = DRAM_BASE + SEP_SIZE
MONITOR_VIRT_BASE = 0x4100000000
KERNEL_BASE = MONITOR_BASE + MONITOR_SIZE
ANS_SIZE = 8 * 1024 * 1024
ANS_BASE = DRAM_BASE + RAM_SIZE - ANS_SIZE
FRAMEBUFFER_SIZE = 2560 * 1136
PRAM_SIZE = 0x4000
STRAM_SIZE = 0x1000
PRAM_BASE = ANS_BASE - 0x9000
STRAM_BASE = ANS_BASE - 0x5000
USABLE_RAM_SIZE = STRAM_BASE - KERNEL_BASE
TIMEBASE_FREQUENCY = 24000000
BOOT_ARGS_SIZE = 736
COMMAND_LINE_OFFSET = 108
COMMAND_LINE_LENGTH = 608


def make_chrp_partition(sig: int, name: str, size_bytes: int, data: bytes = b"") -> bytes:
    if not 0 <= sig <= 255 or not 16 <= size_bytes <= 0xffff * 16 or size_bytes % 16:
        raise ValueError("Invalid CHRP signature or partition size")
    encoded_name = name.encode("ascii")
    if len(encoded_name) > 12 or b"\0" in encoded_name:
        raise ValueError("CHRP partition name must be at most 12 ASCII bytes without NUL")
    if len(data) > size_bytes - 16:
        raise ValueError("CHRP partition data exceeds its declared size")
    hdr = bytearray(16)
    hdr[0] = sig
    hdr[1] = 0
    blocks = size_bytes // 16
    struct.pack_into("<H", hdr, 2, blocks)
    hdr[4:4 + len(encoded_name)] = encoded_name
    csum = 0
    for b in hdr[:16]:
        isum = (csum + b) & 0xff
        if isum < csum:
            isum = (isum + 1) & 0xff
        csum = isum
    hdr[1] = csum
    pad_len = size_bytes - 16 - len(data)
    return bytes(hdr) + data + (b"\x00" * pad_len)


def escape_nvram_data(data: bytes) -> bytes:
    """Escapes binary data for IODTNVRAM using Apple's 0x00/0xFF run-length encoding."""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b == 0x00 or b == 0xff:
            run = 1
            while i + run < n and data[i + run] == b and run < 0x7f:
                run += 1
            out.append(0xff)
            out.append(run | (0x80 if b == 0xff else 0x00))
            i += run
        else:
            out.append(b)
            i += 1
    return bytes(out)


def unescape_nvram_data(raw: bytes) -> bytes:
    """Decodes IODTNVRAM run-length escaped binary data."""
    out = bytearray()
    i = 0
    n = len(raw)
    while i < n:
        b = raw[i]
        if b == 0xff:
            i += 1
            if i >= n:
                raise ValueError("Truncated NVRAM escape sequence")
            cnt_byte = raw[i]
            if cnt_byte == 0:
                raise ValueError("Invalid zero-length NVRAM escape count")
            cnt = cnt_byte & 0x7f
            is_ff = (cnt_byte & 0x80) != 0
            out.extend(bytes([0xff if is_ff else 0x00] * cnt))
            i += 1
        else:
            out.append(b)
            i += 1
    return bytes(out)


def build_nvram_proxy_data(cmdline: str = "", nvram_vars: dict = None) -> bytes:
    if b"\0" in cmdline.encode("ascii"):
        raise ValueError("NVRAM boot arguments cannot contain NUL")
    data = bytearray()
    if cmdline:
        data.extend(f"boot-args={cmdline}\x00".encode("ascii"))
    if nvram_vars:
        for key, val in nvram_vars.items():
            if b"\0" in key.encode("ascii") or "=" in key:
                raise ValueError(f"Invalid NVRAM variable name: {key}")
            if isinstance(val, bytes):
                val_esc = escape_nvram_data(val)
            elif isinstance(val, str):
                if "\0" in val:
                    raise ValueError(f"String NVRAM variable cannot contain NUL: {key}={val}")
                val_esc = val.encode("ascii")
            else:
                raise TypeError(f"Unsupported NVRAM value type for {key}: {type(val)}")
            data.extend(f"{key}=".encode("ascii") + val_esc + b"\x00")
    common = make_chrp_partition(0x70, "common", 0x1000, bytes(data))
    xpram = make_chrp_partition(0x70, "APL,MacOS75", 0x510)
    free = make_chrp_partition(0x7f, "wwwwwwwwwwww", 8192 - len(common) - len(xpram))
    return common + xpram + free


def single_cpu_boot_args(cmdline):
    """Validate the A7 core limit (legacy helper name kept for callers)."""
    cpu_limits = [arg for arg in cmdline.split() if arg.startswith("cpus=")]
    if cpu_limits and cpu_limits not in (["cpus=1"], ["cpus=2"]):
        raise ValueError("The experimental A7 backend supports cpus=1 or cpus=2")
    return cmdline if cpu_limits else cmdline + " cpus=1"


def align(value, boundary=0x4000):
    return (value + boundary - 1) & ~(boundary - 1)


def boot_args(virt_base, dt_pa, dt_size, top, cmdline, framebuffer):
    command = cmdline.encode("ascii")
    if b"\0" in command or len(command) >= COMMAND_LINE_LENGTH:
        raise ValueError("boot_args must be ASCII without NUL and under 608 bytes")
    args = bytearray(BOOT_ARGS_SIZE)
    # ARM64 naturally aligns virtBase at offset 8. Kernel entry loads offsets
    # 8/16/24/32; deviceTreeP at offset 96 is expressed in the kernel's VA range.
    struct.pack_into("<HH", args, 0, 2, 2)
    # Exact iBoot partition allocator: SEP 6MiB, monitor 2MiB, and 0x805000
    # bytes at the end reserved from allocator memory. Physical RAM is 1GiB.
    struct.pack_into("<4Q", args, 8, virt_base, KERNEL_BASE, USABLE_RAM_SIZE, top)
    struct.pack_into("<6Q", args, 40, framebuffer, 1, 640 * 4, 640, 1136, 32)
    struct.pack_into("<I", args, 88, 0)
    struct.pack_into("<QI", args, 96, virt_base + dt_pa - KERNEL_BASE, dt_size)
    args[COMMAND_LINE_OFFSET:COMMAND_LINE_OFFSET + len(command)] = command
    struct.pack_into("<2Q", args, 720, 0, RAM_SIZE)
    return bytes(args)


def add_node(parent, name):
    for child in parent.children:
        if child.get_prop("name") == name.encode() + b"\0":
            return child
    child = DeviceTreeNode()
    child.set_prop("name", name.encode() + b"\0")
    parent.children.append(child)
    return child


def build_image(kernel_path, tree_path, out_dir, cmdline="-v serial=3 debug=0x8 cpus=1", *, monitor_path=None, ans_path=None, nvram_vars=None):
    if monitor_path is None:
        raise ValueError("Native secure monitor is required; rebuild firmware bundle")
    cmdline = single_cpu_boot_args(cmdline)
    cpu_count = int(next(arg[5:] for arg in cmdline.split() if arg.startswith("cpus=")))
    kernel = Path(kernel_path).read_bytes()
    info = inspect_macho(kernel)
    monitor = Path(monitor_path).read_bytes()
    monitor_info = inspect_macho(monitor)
    if monitor_info["text_vmaddr"] != MONITOR_VIRT_BASE:
        raise ValueError("Unexpected native monitor link address")
    for segment in monitor_info["segments"]:
        relative = segment["vmaddr"] - MONITOR_VIRT_BASE
        if relative < 0 or relative + segment["vmsize"] > MONITOR_SIZE - 48:
            raise ValueError("Native monitor segments exceed reserved partition")
    # This legacy image's linked kernel base is 2MiB; __TEXT starts 8KiB in.
    # Rounding to 1GiB instead gives a false -2MiB KASLR slide and makes the
    # prelink loader interpret instructions as kext Mach-O headers.
    virt_base = info["text_vmaddr"] - 0x2000
    if virt_base != 0xFFFFFF8000200000:
        raise ValueError("Unexpected target kernel virtual base")
    tree_bytes = Path(tree_path).read_bytes()
    root, consumed = parse_node(tree_bytes, 0)
    if any(tree_bytes[consumed:]):
        raise ValueError("Unexpected DeviceTree trailing data")
    if root.get_prop("model") != b"iPhone6,2\0" or b"N53AP" not in (root.get_prop("compatible") or b"").split(b"\0"):
        raise ValueError("Loader requires n53ap/iPhone6,2 DeviceTree")
    defaults = root.find_node("defaults")
    if defaults is None or defaults.get_prop("monitor-size") != struct.pack("<I", MONITOR_SIZE) or defaults.get_prop("sep-size") != struct.pack("<I", SEP_SIZE):
        raise ValueError("DeviceTree must declare native SEP/monitor reservations")
    defaults.set_prop("no-effaceable-storage", b"")
    segments = [s for s in info["segments"] if s["vmsize"]]
    for seg in segments:
        relative = seg["vmaddr"] - virt_base
        if relative < 0 or relative + seg["vmsize"] > USABLE_RAM_SIZE:
            raise ValueError("Kernel segment is outside usable RAM")
    highest = KERNEL_BASE - DRAM_BASE + max(s["vmaddr"] - virt_base + s["vmsize"] for s in segments)
    args_offset = align(highest)
    dt_offset = align(args_offset + BOOT_ARGS_SIZE)
    chosen = add_node(root, "chosen")
    chosen.set_prop("boot-args", cmdline.encode("ascii") + b"\0")
    chosen.set_prop("nvram-proxy-data", build_nvram_proxy_data(cmdline, nvram_vars=nvram_vars))
    root_matching = (
        b"<dict><key>IOProviderClass</key><string>IOMedia</string>"
        b"<key>IOPropertyMatch</key><dict><key>Partition ID</key>"
        b"<integer>1</integer></dict></dict>\x00"
    )
    chosen.set_prop("root-matching", root_matching.ljust(256, b"\x00"))
    chosen.set_prop("firmware-version", b"iBoot-1940.10.58\x00".ljust(256, b"\x00"))
    chosen.set_prop("display-scale", struct.pack("<I", 2))
    disp0 = root.find_node("disp0")
    if disp0:
        disp0.set_prop("dot-pitch", struct.pack("<I", 326))
        disp0.set_prop("display-timing-info", struct.pack("<8I", 640, 32, 4, 6, 1136, 13, 352, 3))
    lcd = root.find_node("lcd")
    if lcd:
        lcd.set_prop("lcd-panel-id", struct.pack("<I", 0xa6970dc8))
    oscar = root.find_node("oscar")
    if oscar:
        oscar.children = [c for c in oscar.children if c.get_prop("name") != b"compass\0"]
    memory = add_node(root, "memory")
    memory.set_prop("reg", struct.pack("<QQ", DRAM_BASE, RAM_SIZE))
    # Firmware DT is a bootloader template: original pram/reg is all zeros.
    # arm_init maps and dereferences this pair for its panic/trace buffer.
    # A real reserved range avoids a debugger-proven zero-size mapping abort.
    pram = root.find_node("pram") or add_node(root, "pram")
    pram.set_prop("reg", struct.pack("<QQ", PRAM_BASE, PRAM_SIZE))
    stram = root.find_node("stram") or add_node(root, "stram")
    stram.set_prop("reg", struct.pack("<QQ", STRAM_BASE, STRAM_SIZE))
    # Populate PMGR performance and voltage states template for domain 1 (CPU):
    # In Apple hardware, S5L8960X iBoot-1940.10.58 pmgr_update_device_tree populates
    # nominal-performance1, boost-performance1, and voltage-states1 from calibrated
    # fuses/frequencies. Without this, voltage-states1 has 0 valid states, causing
    # AppleARMPerformanceController to create an IOStateReporter with dimension 0,
    # which panics with "handleSwapCleanup - can't free 1 channels of dimension 0".
    pmgr = root.find_node("pmgr")
    if pmgr:
        pmgr.set_prop("nominal-performance1", struct.pack("<I", 0xe2f3))
        pmgr.set_prop("boost-performance1", struct.pack("<I", 0xc587))
        pmgr.set_prop("total-rails-leakage", struct.pack("<I", 5))
        # 5 genuine Cyclone performance states (Apple iBoot-1940.10.58):
        # State 0: 396 MHz, divider=0x00028676 (165494), 800 mV
        # State 1: 600 MHz, divider=0x0001aaaa (109226), 862 mV
        # State 2: 840 MHz, divider=0x000130c3 (78019),  987 mV
        # State 3: 1128 MHz (nominal), divider=0x0000e2f3 (58099), 1137 mV
        # State 4: 1296 MHz (boost), divider=0x0000c587 (50567), 1200 mV
        # Buffer in DeviceTree is 64 bytes (8 entries * 8 bytes); remaining 3 are zero.
        voltage_states1 = struct.pack(
            "<10I",
            0x00028676, 800,
            0x0001aaaa, 862,
            0x000130c3, 987,
            0x0000e2f3, 1137,
            0x0000c587, 1200,
        ) + (b"\x00" * 24)
        pmgr.set_prop("voltage-states1", voltage_states1)
        # Genuine bridge-settings (776 bytes) from S5L8960X iBoot-1940.10.58 pmgr_update_device_tree
        bridge_settings_hex = (
            "0400000020280000080000002a3500000c000000101400001000000052670000"
            "1400000010140000180000002a3500003c000000043c01800000000000000000"
            "14000000040001801c00000028280000001000000f000fc00000000000000000"
            "1400000004000180001000000f000fc000000000000000001400000004000180"
            "2000000010100000001000000f000fc004100000040001c00400030000101100"
            "18000300674523011c0003000fcdab892000030067452301240003000fcdab89"
            "0000000000000000140000000400018004100000040001c0001000000f000fc0"
            "040003000010000018000300674523011c000300efcdab892000030067452301"
            "240003000fcdab890000000000000000040000000c1000002c00000004000080"
            "0000000000000000140000000400018018000000100024001c00000030300000"
            "2000000040400000001000000f000fc01400010004000180001001000f000fc0"
            "000000000000000008000000181e00001c000000040c01800000000000000000"
            "1400000004000180001000000f000fc000000000000000001400000004000180"
            "001000000f000fc000000000000000001400000004000180001000000f000fc0"
            "00000000000000001400000004000180000000000000000000000000017c7c00"
            "04000000017c7c0008000000017c7c000c00000001797c001000000001797c00"
            "14000000017c7c0018000000017c7c001c000000017c7c0020000000017c7c00"
            "24000000017c7c0028000000017c7c002c000000017c7c003000000001696c00"
            "3400000001797c0038000000017c7c003c000000017c7c004000000001727400"
            "440000000172740048000000017274004c0000000172740050000000017c7c00"
            "54000000017c7c0058000000017c7c005c00000001696c0060000000017c7c00"
            "640000000179790068000000017979006c000000017c7c007000000001696c00"
            "740000000179790078000000017979007c000000017979000000000000000000"
            "0010000000000000041000000000000010100000000100000000000000000000"
            "0000000000000000"
        )
        bridge_settings = bytes.fromhex(bridge_settings_hex)
        if len(bridge_settings) != 776 or hashlib.sha256(bridge_settings).hexdigest() != "7090b9a4a8391290387e22cc06548a8de8d6bfc93295f8e9bed717563922fa25":
            raise ValueError("Native iBoot bridge-settings table is corrupt")
        pmgr.set_prop("bridge-settings", bridge_settings)
    # This PLL/clock list is an experimental emulator profile, not a verified
    # physical-device table. iBoot computes its clock table from PLL registers.
    # Use the native 1296-MHz boost fallback consistently for the CPU clock.
    clocks_61 = [
        24000000, 837000000, 798000000, 0, 24000000, 1200000000, 0, 0,
        798000000, 798000000, 837000000, 171428571, 600000000, 24000000,
        50000000, 12000000, 240000000, 240000000, 24000000, 60000000,
        30000000, 0, 0, 0, 240000000, 24000000,
        240000000, 171428571, 6000000, 6000000, 150000000, 171428571,
        30769230, 200000000, 200000000, 100000000, 133333333, 200000000,
        240000000, 0, 120000000, 133333333, 120000000, 0,
        0, 0, 0, 0, 200000000, 24000000,
        30000000, 30000000, 30000000, 30000000, 60000000, 0,
        0, 0, 0, 6000000, 6000000,
    ]
    clocks_61[7] = 1296000000
    arm_io = root.find_node("arm-io")
    if arm_io:
        # Native iBoot copies at most 61 uint32 entries into this 384-byte template.
        raw_clocks = struct.pack(f"<{len(clocks_61)}I", *clocks_61) + (b"\x00" * (384 - len(clocks_61) * 4))
        arm_io.set_prop("clock-frequencies", raw_clocks)
        arm_io.set_prop("usbphy-frequency", struct.pack("<I", clocks_61[0]))
        # A0 revision is an explicit emulator assumption, not a measured fuse.
        arm_io.set_prop("chip-revision", struct.pack("<I", 0))
    memory_map = add_node(chosen, "memory-map")
    memory_map.properties = {"name": memory_map.properties["name"]}
    for seg in segments:
        memory_map.set_prop("Kernel-" + seg["name"], struct.pack("<QQ", KERNEL_BASE + seg["vmaddr"] - virt_base, seg["vmsize"]))
    memory_map.set_prop("SecureMonitor", struct.pack("<QQ", MONITOR_BASE, MONITOR_SIZE))
    memory_map.set_prop("SEP", struct.pack("<QQ", DRAM_BASE, SEP_SIZE))
    cpus = root.find_node("cpus")
    if cpus:
        for index, cpu in enumerate(cpus.children):
            cpu.set_prop("state", b"running\0" if index == 0 else b"off\0")
            cpu.set_prop("timebase-frequency", struct.pack("<Q", clocks_61[0]))
            cpu.set_prop("clock-frequency", struct.pack("<Q", clocks_61[7]))
            cpu.set_prop("memory-frequency", struct.pack("<Q", clocks_61[9]))
            cpu.set_prop("bus-frequency", struct.pack("<Q", clocks_61[42]))
            cpu.set_prop("peripheral-frequency", struct.pack("<Q", clocks_61[25]))
            cpu.set_prop("fixed-frequency", struct.pack("<Q", clocks_61[0]))
    # Native selector 3 is ANS firmware RAM, not display RAM. Keep it free for
    # the real IOP image. Diagnostic scanout uses a separate loader allocation
    # below topOfKernelData, so it cannot corrupt ANS or be reclaimed by XNU.
    memory_map.set_prop("ANSReserved", struct.pack("<QQ", ANS_BASE, ANS_SIZE))
    memory_map.set_prop("DiagnosticFramebuffer", struct.pack("<QQ", 0, FRAMEBUFFER_SIZE))
    memory_map.set_prop("PanicRAM", struct.pack("<QQ", PRAM_BASE, PRAM_SIZE))
    memory_map.set_prop("SleepTokenRAM", struct.pack("<QQ", STRAM_BASE, STRAM_SIZE))
    for name, offset, length in [("BootArgs", args_offset, BOOT_ARGS_SIZE), ("DeviceTree", dt_offset, 0)]:
        memory_map.set_prop(name, struct.pack("<QQ", DRAM_BASE + offset, length))
    raw_tree = serialize_node(root)
    memory_map.set_prop("DeviceTree", struct.pack("<QQ", DRAM_BASE + dt_offset, len(raw_tree)))
    framebuffer_offset = align(dt_offset + len(raw_tree))
    framebuffer = DRAM_BASE + framebuffer_offset
    memory_map.set_prop("DiagnosticFramebuffer", struct.pack("<QQ", framebuffer, FRAMEBUFFER_SIZE))
    # iBoot fills the /vram placeholder with the display carveout.
    # IOSurfaceRoot::installMemoryRegions waits for this region (vram ->
    # PurpleGfxMem) before any IOSurfaceRoot client can open; a zero reg
    # blocks backboardd's render server. Same 16-byte size: offsets stay put.
    vram = root.find_node("vram")
    if vram is not None:
        vram.set_prop("reg", struct.pack("<QQ", framebuffer, FRAMEBUFFER_SIZE))
    raw_tree = serialize_node(root)
    top = DRAM_BASE + align(framebuffer_offset + FRAMEBUFFER_SIZE)
    total = top - DRAM_BASE + 0x10000
    if total >= PRAM_BASE - DRAM_BASE:
        raise ValueError("Loader allocations overlap framebuffer")
    args = boot_args(virt_base, DRAM_BASE + dt_offset, len(raw_tree), top, cmdline, framebuffer)
    monitor_args_pa = MONITOR_BASE + MONITOR_SIZE - 48
    kernel_entry_pa = KERNEL_BASE + info["entry_va"] - virt_base
    monitor_args = struct.pack("<6Q", 1, MONITOR_VIRT_BASE, MONITOR_BASE, MONITOR_SIZE, DRAM_BASE + args_offset, kernel_entry_pa)
    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    ans_region = None
    if ans_path is not None:
        from qemu.ans_image import prepare_ans_region
        ans_region = prepare_ans_region(ans_path, output, clocks_61)
        ans = root.find_node("ans")
        if ans is None:
            raise ValueError("Native DeviceTree lacks the ANS provider")
        ans.set_prop("region-base", struct.pack("<Q", ANS_BASE))
        ans.set_prop("region-size", struct.pack("<Q", ANS_SIZE))
        updated_tree = serialize_node(root)
        # Size grows by four bytes when the original uint32 template becomes
        # iBoot's uint64 handoff. Both remain in the already reserved DT pages.
        if len(updated_tree) > framebuffer_offset - dt_offset:
            raise ValueError("ANS DeviceTree fixups exceed reserved tree pages")
        memory_map.set_prop("DeviceTree", struct.pack("<QQ", DRAM_BASE + dt_offset, len(updated_tree)))
        raw_tree = serialize_node(root)
        args = boot_args(virt_base, DRAM_BASE + dt_offset, len(raw_tree), top, cmdline, framebuffer)
    image_path = output / "KernelMemory.bin"
    with image_path.open("wb") as stream:
        stream.truncate(total)  # sparse holes initialize BSS and page-table reserve
        for seg in segments:
            stream.seek(KERNEL_BASE - DRAM_BASE + seg["vmaddr"] - virt_base)
            stream.write(kernel[seg["fileoff"]:seg["fileoff"] + seg["filesize"]])
        for seg in monitor_info["segments"]:
            stream.seek(MONITOR_BASE - DRAM_BASE + seg["vmaddr"] - MONITOR_VIRT_BASE)
            stream.write(monitor[seg["fileoff"]:seg["fileoff"] + seg["filesize"]])
        stream.seek(monitor_args_pa - DRAM_BASE)
        stream.write(monitor_args)
        stream.seek(args_offset)
        stream.write(args)
        stream.seek(dt_offset)
        stream.write(raw_tree)
    (output / "DeviceTree.runtime").write_bytes(raw_tree)
    (output / "boot_args.bin").write_bytes(args)
    (output / "monitor_args.bin").write_bytes(monitor_args)
    plan = dict(schema_version=1, image=str(image_path.resolve()),
                image_sha256=hashlib.sha256(image_path.read_bytes()).hexdigest(),
                dram_base=DRAM_BASE, memory_size=RAM_SIZE, virt_base=virt_base,
                usable_memory_size=USABLE_RAM_SIZE,
                entry_va=info["entry_va"], entry_pa=kernel_entry_pa, kernel_phys_base=KERNEL_BASE,
                boot_el=3, initial_entry_pa=MONITOR_BASE + monitor_info["entry_va"] - MONITOR_VIRT_BASE,
                initial_boot_args_pa=monitor_args_pa,
                monitor=dict(physical_base=MONITOR_BASE, virtual_base=MONITOR_VIRT_BASE, reserved_size=MONITOR_SIZE, segments=monitor_info["segments"]),
                boot_args_pa=DRAM_BASE + args_offset, devicetree_pa=DRAM_BASE + dt_offset,
                top_of_kernel_data=top, framebuffer_pa=framebuffer,
                pram=dict(address=PRAM_BASE, size=PRAM_SIZE, persistent=False),
                stram=dict(address=STRAM_BASE, size=STRAM_SIZE), timebase_frequency=TIMEBASE_FREQUENCY,
                ans_reserved=dict(address=ANS_BASE, size=ANS_SIZE,
                                  runtime_image_prepared=ans_region is not None,
                                  execution_verified=False),
                auxiliary_images=[ans_region] if ans_region else [],
                framebuffer=dict(width=640, height=1136, stride=2560, depth=32),
                segments=segments, boot_args=cmdline, cpu_count=cpu_count,
                validation="prepared; guest execution not yet verified")
    (output / "boot_plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel", required=True)
    parser.add_argument("--devicetree", required=True)
    parser.add_argument("--monitor", required=True)
    parser.add_argument("--ans-firmware", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--boot-args", default="-v serial=3 debug=0x8 cpus=1")
    args = parser.parse_args()
    try:
        print(json.dumps(build_image(args.kernel, args.devicetree, args.out_dir, args.boot_args,
                                    monitor_path=args.monitor, ans_path=args.ans_firmware), indent=2))
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Loader error: {exc}\n")


if __name__ == "__main__":
    main()
