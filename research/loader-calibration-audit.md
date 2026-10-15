# Audit of Antigravity loader handoff additions

Read-only audit of `scripts/qemu/boot_image.py` against the unmodified n53ap iBoot-1940.10.58 artifact and the exact iOS 7.1.2 kernel/DeviceTree. This report distinguishes native ABI evidence from chosen emulator defaults. It does not establish hardware, userspace or graphics completeness.

## Bridge settings: byte-copy defect

At audit time the loader's hex literal decoded to **777 bytes**, despite its 776-byte comment. The original DeviceTree property is exactly 776 bytes. Native iBoot function `0x1b4f4` obtains `bridge-settings`, checks its size is greater than `0x307`, and at `0x1b530`–`0x1b53c` copies exactly **0x308 bytes** from file offset **0x2fa1c**. The loader copy first differs at byte 737; it is not a faithful byte copy.

The exact original 776-byte blob is saved as `firmware-artifacts/bridge-settings.bin`, with instruction/source provenance in `bridge-settings.provenance.json`. SHA256: `7090b9a4a8391290387e22cc06548a8de8d6bfc93295f8e9bed717563922fa25`. The root engineer owns the loader correction; this audit did not edit it.

## Performance states: proven native fallback, not fuse calibration

- `nominal-performance1=0xe2f3` and `boost-performance1=0xc587` are real immediate constants stored by iBoot at `0x1bcd0`–`0x1bcd4` and `0x1bd08`–`0x1bd0c`. Their property ABI is exactly four bytes.
- iBoot `0x1bd54`–`0x1bdcc` computes five state periods from PLL descriptors at `0x2fd28 + 0x10 + state*8`. Reproducing its 24-MHz multiplier/divider formula yields 396, 600, 840, 1128 and 1296 MHz, with period words `0x28676`, `0x1aaaa`, `0x130c3`, `0xe2f3`, `0xc587` respectively. These loader words are reproducible native values.
- Voltage words 800, 862, 987, 1137 and 1200 match **native fallback** voltage-table entries 2–6 at `0x2fa00`. The firmware also has a hardware/fuse-calibrated path through `0x1b168` which reads MMIO around `0x2022200a8`; the chosen values do not prove that calibration.
- `total-rails-leakage=5` is the result of the native formula at `0x1b2f0` only when the relevant fuse registers at `0x200e02a010` and `0x200e02a014` contribute zero. The loader should describe this as an emulator fuse/default profile, not a measured device leakage value.
- `voltage-states1` is originally 64 bytes. Native iBoot requires at least 40 bytes and fills five eight-byte pairs; preserving the original remaining zero template bytes is consistent with its handoff shape.

## Clock table and CPU fields: shape proven, experimental values

The original `arm-io/clock-frequencies` template is 384 zero bytes. Native iBoot `0x189e4`–`0x189f0` passes property-length/4 to helper `0x1baac`; that helper copies at most **0xf4 bytes**, 61 uint32 values, from a runtime table at relative `0x713d8`. The remaining template bytes remain untouched. The loader's 244-byte table plus zero padding therefore has the correct shape.

However, the claimed runtime table address `0x83d8683d8` is **beyond** the static iBoot image (`0x713d8` relative versus file size `0x6e000`). Its values are computed by PLL/divider-register logic beginning at `0x1b594`. The current 61-value list does not appear verbatim in the firmware. Until its generating register profile is recorded and matched to the backend, its numbers are **chosen emulator configuration**, not a recovered static table.

Native iBoot updates only `/cpus/cpu0` here. Its property sources are:

| CPU property | iBoot accessor selector | Runtime clock-table entry | Value in current loader clock list |
| --- | ---: | ---: | ---: |
| `clock-frequency` | 0 | 7 | 1296 MHz |
| `memory-frequency` | 2 | 9 | 798 MHz |
| `bus-frequency` | 3 | 42 | 120 MHz |
| `peripheral-frequency` | 1 | 25 | 24 MHz |
| `fixed-frequency` | 4 | 0 | 24 MHz |
| `timebase-frequency` | 5 | 0 | 24 MHz |

These mappings follow actual selector dispatch at `0x1bacc` and branches `0x1bafc`, `0x1bb2c`, `0x1bb44`, `0x1bb54`, `0x1bb64`, with property-copy sites `0x18810`–`0x189a4`. The original cpu0 properties are eight bytes; iBoot zero-extends its uint32 results to uint64 before copying, so the loader's eight-byte representation is valid.

At audit time the loader instead supplied 1300 MHz CPU, 24 MHz memory and bus, and left original `peripheral-frequency` zero. Those differ from its own clock-table profile. The 1300-MHz CPU value also differs from the recovered 1296-MHz boost state. This is a semantic coherence gap rather than evidence of a malformed eight-byte ABI. Setting the same extra frequency properties on inactive cpu1 is additional emulator policy, not this native update path.

The delegated follow-up now sets clock-table entry 7 to 1296 MHz and derives all six CPU fields from the corresponding entries above. A runtime DeviceTree built from the validated real firmware was parsed and all six values matched its table. The profile and A0 chip-revision assumption are explicitly experimental; this verifies coherence, not physical-device PLL calibration. The bridge copy was also corrected to exactly 776 native bytes by the root engineer.

`usbphy-frequency` is a four-byte value written by iBoot using selector `0x8e` at `0x18a18`–`0x18a24`. `chip-revision` is a four-byte hardware-derived value obtained from function `0x257ec` at `0x18bb8`; the chosen A0 value zero has no recorded fuse/device evidence. A coherent emulator profile can choose it explicitly.

## NVRAM proxy

The builder emits an 8192-byte boot-time proxy comprising a common partition, APL,MacOS75 partition and free partition. Headers are 16 bytes, partition lengths are in 16-byte blocks, names fit the 12-byte field, and the command-line variable is NUL terminated. Its end-around-carry checksum matches the exact kernel's `IODTNVRAM::calculatePartitionChecksum` routine at VA `0xffffff80002ca1bc`–`0xffffff80002ca1f0`.

The official [Apple IONVRAM source](https://github.com/apple/darwin-xnu/blob/main/iokit/Kernel/IONVRAM.cpp) describes the same header shape and reads native uint16 partition length. This supports little-endian length on ARM64; a direct target-version partition-parser trace remains desirable. Do not flag this as incorrect big-endian encoding solely because generic CHRP implementations use big endian.

The chosen partition sizes and empty APL data are synthetic boot configuration, not an extracted device NVRAM dump. Building this proxy on every launch also does not provide persistent guest NVRAM. Until a controller/storage path is implemented, the manifest must not claim NVRAM write persistence or rely on its variables to certify boot success.

## Acceptance follow-up

Use the exact 776-byte bridge artifact; retain native performance fallback values with explicit provenance; describe chip/fuse/PLL defaults as one coherent emulator profile; reconcile cpu0 frequencies with that profile and its runtime clock table. Verify IODTNVRAM can parse the generated partitions and verify that clock-dependent drivers make progress in a real guest trace. Passing a former panic does not establish correctness of the remaining hardware behavior.
