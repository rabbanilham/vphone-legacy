# Verified iOS 7.1.2 firmware contents

Inspected the exact `build/ios712-validated/system.raw` HFSX partition read-only via macOS disk-image services. Initial encrypted/decrypted originals were preserved. This confirms firmware contents; no guest process was executed and no boot/display capability is established.

SystemVersion.plist: `iPhone OS 7.1.2 (11D257)`.

## Native executables

| Guest path | Bytes | SHA256 |
| --- | ---: | --- |
| `/sbin/launchd` | 200864 | `e03b9eea881f9f2c36ccac6c82919e1600067b1246bf193d065a739f0f5215a5` |
| `/usr/libexec/backboardd` | 565264 | `f6b8742886a53f91d610e9ba36b6029fefc09ffcaa526386d6ae5d603c338a7c` |
| `/System/Library/CoreServices/SpringBoard.app/SpringBoard` | 5332672 | `7ac0648541d8b4efef35bff46de2c5b2e0aa45e48fdd1f902e580eac5ca22620` |
| `/Applications/MobileNotes.app/MobileNotes` | 371008 | `06f773d14c85daedb567bb312e1f07bfe1c40a6f9859dbeb3dad50a77d9176a9` |
| `/Applications/Calculator.app/Calculator` | 143392 | `8efc6b70b8e9a93fc14b32456375d1003d2858b4cbf4cea06ef2898a9634d507` |
| `/Applications/Preferences.app/Preferences` | 853904 | `7ce3fdc1aa912b20e70b5b1fdc126ec9e355e05e3badfb9121430136332f4b7b` |

All six are real ARM64 Mach-O binaries. `com.apple.backboardd.plist` and `com.apple.SpringBoard.plist` launch the corresponding native binaries as user `mobile`, with RunAtLoad/KeepAlive. Backboard owns `com.apple.CARenderServer`, display services and HID services.

## Graphics libraries

The ARM64 dyld shared cache contains 702 image entries. Framework executables absent as individual files are present in the actual cache image table; searching only filesystem filenames would incorrectly report them missing.

| Cached guest image | VM address |
| --- | --- |
| `/System/Library/Extensions/AGXGLDriver.bundle/AGXGLDriver` | `0x180260000` |
| `/System/Library/Frameworks/OpenGLES.framework/OpenGLES` | `0x183fe4000` |
| `/System/Library/Frameworks/QuartzCore.framework/QuartzCore` | `0x184240000` |
| `/System/Library/PrivateFrameworks/BackBoardServices.framework/BackBoardServices` | `0x185cb8000` |
| `/System/Library/PrivateFrameworks/IOMobileFramebuffer.framework/IOMobileFramebuffer` | `0x187730000` |
| `/System/Library/PrivateFrameworks/IOSurface.framework/IOSurface` | `0x18773c000` |

Cache SHA256: `d3d6e3be15108b04c33e36f62afb9230400c348cc63a11b2fdc91c82b2961427` (373058050 bytes). AGXGLDriver bundle metadata identifies `com.apple.AGXGLDriver`, version 27.23; presence does not prove emulator compatibility.

## Original storage mapping and data seeds

Firmware `/private/etc/fstab` is unchanged:

```fstab
/dev/disk0s1 / hfs ro 0 1
/dev/disk0s2 /private/var hfs rw,nosuid,nodev 0 2
```

`/var` points to `private/var`. The extracted system partition is from an APM container whose logical blocks are 2048 bytes, system offset 32768 bytes and partition size 2185363456 bytes. The manifest intentionally supplies separate raw partitions; a future Apple storage driver must expose `/dev/disk0s1` and `/dev/disk0s2` with exactly this mapping, or a justified fstab patch is required. Generic two-disk virtio attachment does not establish this mapping.

The firmware system image includes these initial `/private/var` directories:

- `Keychains` (mode `0755`)
- `Managed Preferences` (mode `0755`)
- `MobileDevice` (mode `0755`)
- `audit` (mode `0700`)
- `db` (mode `0755`)
- `ea` (mode `0755`)
- `empty` (mode `0755`)
- `folders` (mode `0755`)
- `log` (mode `0755`)
- `logs` (mode `0755`)
- `mobile` (mode `0711`)
- `msgs` (mode `0755`)
- `networkd` (mode `0755`)
- `preferences` (mode `0755`)
- `root` (mode `0755`)
- `run` (mode `0775`)
- `spool` (mode `0755`)
- `tmp` (mode `1777`)
- `vm` (mode `0755`)
- `wireless` (mode `0755`)

The seed tree contains 7 regular files totaling 336768902 bytes. It includes mobile `.forward`, `Applications`, `Library`, `Media`; `Library/PreinstalledAssets` font and dictionary CPIO archives; `db/launchd.db`, `db/lsd`, `db/timezone`; and root `Library`. An empty data filesystem mounted over `/private/var` hides these seeds. Restore/first-boot initialization must preserve symlinks, guest ownership, permissions and extended attributes when provisioning it.

The inspection mount uses host ownership mapping, so host-reported UID/GID values were not accepted as guest ownership evidence. Guest account metadata/ownership must be read with HFS ownership enabled before provisioning. No passcode, activation, keybag or application databases have been initialized. `init_data_protection` exists in `/usr/libexec`; its runtime requirements still need analysis.

Manifest accurately records `storage.data_provisioned=false` and `storage.guest_mapping_verified=false`. Neither partition has been written by an emulated guest.

## Prepared artifact provenance

| Artifact | Bytes | SHA256 |
| --- | ---: | --- |
| `kernel.macho` | 17100800 | `1e0f584ff54040ded8a2ef4ed532b4298f2500b774fc14d88020453c91465666` |
| `DeviceTree.raw` | 142016 | `583bec798dbe0eaebd9d861685abd150fdf3b47c64ca00f710872003ad7c4205` |
| `system.raw` | 2185363456 | `c7a987439e75bb8e989f3e1ee76bea3f0fec7d9ae804e5ad1d618409f0524c30` |
| `data.raw` | 1073741824 | `7bc1b7e2daa3d7ffc9919057b98ba022bd78785275dfcad618b4697ec928f8f0` |

IPSW SHA256: `3d080c18cd0eeacd27070203a9fb55954331ae748518b8846c3830dac9a146e9`. Original KernelCache/DeviceTree encrypted files also matched the selected BuildManifest SHA1 digests before decryption. These are integrity/provenance checks, not a claim of independent Apple signature verification.

Inspection mount was detached after recording the evidence.
