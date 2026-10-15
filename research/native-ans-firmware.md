# Native n53ap ANS firmware and bootloader handoff

The SHA1-verified selected BuildManifest iBoot component contains genuine ANS firmware. Its unmodified decrypted SHA256 is `05b1a19952c119bfbc7e58868e484252f593efd616a16317eb4fd2c501400d81`. The exact extracted source is `firmware-artifacts/ans-firmware.bin`, 0x39000 bytes, SHA256 `c066bf9d1edef96e59fe618eb30933847ba00ac82bff2b8a7b3ef0b7a32c9b7e`. No firmware name was invented and no original bytes were patched.

## Source extent and provider chain

All addresses here are relative to decrypted iBoot; its relocated base is 0x83d7f7000. Native loader function 0x6cc4 selects provider table at 0x333d8, whose first pointer resolves to 0x333f8. Provider offsets 0x18, 0x20 and 0x28 point to getters 0x711c, 0x7110 and 0x7100 respectively. The source getter returns iBoot offset **0x35000**; size getter returns **0x39000**. This extent ends exactly at the original iBoot image end, 0x6e000. Config getter reads the word at descriptor 0x33a58, returning **0x365a0 relative to the firmware blob**.

The provider's banner strings resolve through descriptor 0x33a40 to `s5l8960xans`, `release`, `AppleStorageProcessor-378.5.46`. Its source begins ARM32 exception/vector instructions, including a BLX at vector zero. This is an ANS coprocessor image, not an ARM64 XNU executable; loading it into RAM does not establish coprocessor execution.

Vector zero's `BLX 0x1040` enters **Thumb** code. At 0x1040 it masks interrupts in supervisor mode and updates CP15 SCTLR. Bootstrap literal words at 0x112c/0x1130 describe a zero-initialized interval 0x3a000–0x982f0, while 0x1134/0x1138 describe 0x39000–0x39018. These native MCU-relative addresses independently support the RAM extent versus source-file-size distinction. The coprocessor's address-zero execution view is distinct from host DMA physical address 0x83f800000; an AArch64 CPU jump into this blob would not execute it correctly.

The firmware preparation extractor pins the entire audited decrypted iBoot hash, checks provider pointers, source bounds and config fields, and checks the extracted hash. A firmware with any other content fails explicitly rather than searching for similar strings or signatures.

## Native reservation and initialization

Native iBoot caller 0x383c–0x3858 supplies region base **0x83f800000** and capacity **0x800000**. Independent getters 0x70e4 and 0x70dc return the same geometry. The DeviceTree writes at 0x13b7c–0x13bf0 populate `arm-io/ans/region-base` and `region-size` using reservation selector 3. This eight-MiB reservation belongs to ANS; it must not overlap framebuffer allocations.

At relative firmware config 0x365a0, original fields are:

| Offset | Width | Value | Native meaning |
| --- | ---: | --- | --- |
| 0x00 | 4 | `CisC` | magic checked at iBoot 0x6d44–0x6d54 |
| 0x04 | 4 | 2 | config version checked at 0x6d5c–0x6d64 |
| 0x08 | 4 | 0x982f0 | firmware RAM extent, checked against region capacity |
| 0x0c | 4 | 0x1000 | alignment, checked power-of-two and capped to 0x1000 |
| 0x14 | 4 | 0x400000 | requested shared-memory size |
| 0x18 | 8 | 2 | shared-region descriptor count |

Native iBoot zeroes **0x982f0** bytes at the region base (0x6eec–0x6ef8), then copies exactly **0x39000** original firmware bytes (0x6efc–0x6f20). The distinction matters: the raw source does not contain the entire firmware RAM/BSS extent.

## Required runtime config fixups

After copying, iBoot validates `CisC` again and mutates the copied config, leaving the source unchanged:

| Config offset | Width | Native source/store |
| --- | ---: | --- |
| 0x40 | 4 | chip ID getter 0x1b0c4 returns 0x8960; store 0x6f9c |
| 0x44 | 4 | chip revision getter 0x1b0cc reads fuse register 0x200e02a000; store 0x6fa4 |
| 0x48 | 4 | clock selector 0x8d returns clock-table entry 0; store 0x6fb0 |
| 0x4c | 4 | clock selector 0x90 returns clock-table entry 41; store 0x6fbc |
| 0x20 | 8 | first shared region offset relative to ANS base; store 0x7040 |
| 0x28 | 4 | first shared region length; store 0x7054 |
| 0x30 | 8 | second shared region offset cleared at 0x6ff8 |
| 0x38 | 4 | second shared region length cleared at 0x6ffc |
| 0x50 | 4 | debug flag derived from boot-policy helper 0x938c; store 0x70a0 |

For the native source config and eight-MiB region, the first shared offset is align-up(0x982f0, 0x1000) = **0x99000**, hence shared physical address **0x83f899000**. The remaining capacity is 0x767000, so the requested **0x400000** bytes fit. The native routine derives these values at 0x7010–0x7060. Config source bytes for these fields are zero; passing an unchanged blob alone is insufficient.

The clock list remains an experimental emulator profile. Entry 0 currently advertises 24 MHz and entry 41 advertises 133333333 Hz; these selected values must remain coherent with the guest DeviceTree and backend. Chip revision A0 and the debug flag need explicit policy or hardware evidence, rather than being presented as recovered device fuse state.

## State and remaining work

Fresh firmware bundles and the existing workspace validation bundle now preserve byte-exact iBoot and ANS originals, with validated artifact checksums and `runtime_fixups_applied=false`, `execution_verified=false`. System and data images were preserved. The previous manifest is backed up as `vm_manifest.before-native-ans.json`.

The kernel's current AppleCSI error is consistent with zero ANS region metadata and an empty native `IOPFirmwareName`; it is not evidence that an arbitrary firmware path should be substituted. Native A7IOP register setup, coprocessor execution or a faithful ANS service backend, mailbox protocol, and real storage operations still require implementation and runtime proof. The extracted source and handoff contract enable that work; they do not certify storage detection, filesystem mount, launchd, or SpringBoard.
