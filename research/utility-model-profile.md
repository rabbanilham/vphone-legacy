# Persistent logical utility profile v1

This is a model-private container for the ASP software device. It is **not an
Apple NAND dump, native UtilDM/FTL metadata or execution of the ANS formatter**.
Native firmware wire command 0x15 changes utility and LBA state; commands
0x15/0x16 remain unsupported. No `nand-enable-reformat` boot argument is added.

Native AP utility APIs and their page capacities are retained in
[the storage audit](continuation-20261013-storage.md) and
[the physical-format investigation](native-utility-format.md). The software
model provisions those logical regions independently of System/Data.

| Region | Read / write | 4096-byte pages | First file page |
| --- | --- | ---: | ---: |
| LLB | 0x30 / 0x40 | 253 | 1 |
| Firmware | 0x31 / 0x41 | 2048 | 254 |
| Control bits | 0x34 / 0x44 | 2 | 2302 |
| Effaceable | 0x35 / 0x45 | 1 | 2304 |
| NVRAM | 0x36 / 0x46 | 2 | 2305 |
| SysCfg | 0x37 / 0x47 | 32 | 2307 |

All newly allocated logical pages contain actual erased bytes `0xff`. These
are empty virtual-device state: no signed firmware, physical SysCfg calibration,
device identity, valid keybag or SEP capability is supplied by them. Guest
consumers may reject absent content. Guest reads return these real bytes;
guest writes update and flush them. Unknown commands retain error completions.

## Container and commit

The file is exactly **9580544 bytes**, consisting of one 4096-byte header and
2338 logical pages. Integer fields are little endian.

| Header offset | Field |
| ---: | --- |
| 0 | 15 ASCII bytes `VPHONE-UTILITY1` and one NUL, 16 bytes total |
| 16 / 20 / 24 / 28 | uint32 version 1, page size 4096, region count 6, reserved zero |
| 32 | uint64 actual virtual-provisioning epoch, within native uint32 range |
| 40 / 48 | uint64 System/Data LBA counts, bound to the active image lengths |
| 56 | 32 raw bytes of the verified native ANS source SHA256 |
| 88 | zero-padded 32-byte profile `n53ap-11D257-ASP10-utility-v1` |
| 128 | Six 32-byte region entries: uint32 read/write opcodes, uint64 first page/page count/reserved zero |
| 4092 | Standard CRC32 of preceding 4092 header bytes |

Remaining header bytes are zero. Validation checks the complete layout,
source/profile, geometry, reserved bytes, CRC and exact file length. Readiness
derives from this initialized allocated map and working backing, rather than a
standalone success flag. Provisioning writes and fsyncs all erased pages first,
commits the header last, atomically publishes the new file and fsyncs its
directory. Existing files are validated/reused or rejected; they are never
reinitialized during launch. The epoch is a virtual provisioning event, not a
recovered device's original format date.

The VM manifest registers `utility_disk` as mutable, with its initial checksum,
native-source binding and profile. `vm doctor` separately validates its header;
payload changes from guest writes are expected. Each launch snapshots the whole
utility container with System/Data before opening them. Runtime and generated
boot evidence record the exact selected paths and hashes.

The C backend locks the utility file for writing and limits utility DMA to
machine RAM, native tag capacities and the chosen region extent. Header bytes
are outside every guest region. Errors, short/failed host I/O, truncated files
or changed headers reach the guest as errors. Geometry readiness and ASP format
epoch use the validated container. Native physical NAND formatting, wear,
original contents and cryptographic services remain outside this profile.

Implementations: `scripts/qemu/utility.py`, `scripts/qemu/runtime.py`,
`scripts/bundle.py` and `backend/s5l8960x.c`. Tests distinguish virtual allocation
and byte persistence from successful native filesystem/userspace/UI boot.
