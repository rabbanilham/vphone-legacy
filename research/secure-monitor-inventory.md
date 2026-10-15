# Recoverable boot firmware and unresolved secure monitor

Target: exact iPhone6,2 / n53ap / iOS 7.1.2 build 11D257 erase BuildIdentity from the local IPSW. Original production firmware was not changed.

## Recoverable artifacts

Extracted immutable encrypted originals and decrypted research derivatives under `research/firmware-artifacts/`. All four encrypted boot-component files matched their selected BuildManifest SHA1 before decoding. Decryption used exact 16-byte IV and 32-byte AES keys from the already selected [board/build key source](https://theapplewiki.com/index.php?title=Keys:Sochi_11D257_(iPhone6,2)&oldid=282408), without filling missing digits or mixing boards. The key catalog is unofficial; the actual decrypted instructions/strings corroborate successful decoding.

| Component | Decrypted bytes | SHA256 decrypted derivative |
| --- | ---: | --- |
| iBoot | 450560 | `05b1a19952c119bfbc7e58868e484252f593efd616a16317eb4fd2c501400d81` |
| LLB | 147456 | `32fd5ca413f81e136581b26017253a7cc153e77a4ae536b24c24f38709b9424c` |
| iBEC | 450560 | `7a6f5d5874b422e78a16b951d777d10a0e78ec6a9ed8ddd931b42925dc973ac6` |
| iBSS | 139264 | `5dcf2f0a61ca9109c1ab30f62f87358967737817fa38c41b280c7a12138a555c` |

iBoot and iBEC decode as native A64 boot code and identify `iBoot-1940.10.58`. File offsets below refer to unmodified decrypted iBoot.

## Secure-monitor evidence

- The entire IPSW member inventory and BuildManifest have no separately named SecureMonitor, monitor, or `hypr` component. SEP is present, but SEP firmware is a different component and is not evidence of an application-processor EL3 monitor.
- iBoot uses EL3 registers: at `0x58`, `MSR VBAR_EL3,x10` installs the relative `0x1f000` exception vector; EL3 SCTLR/SCR/MAIR/TCR/TTBR0 accesses occur around `0x368`–`0x3a4`.
- At `0x1f400`, the EL3 synchronous vector constructs `0x5e005ec3`, compares it against ESR_EL3, and branches through x0 only on that value. This does **not** establish handling for XNU `SMC #0x11` with service x0=0x800.
- iBoot queries the `boot-monitor` variable at `0x9b0`; a nonempty value passes a constructed path to function `0x15ae0` at `0x9e4`.
- Function `0x15ae0` constructs the image FourCC `hypr` at `0x15b14`, calls validation/loading at `0x229b0`, then passes the decoded image to monitor loader `0x15a00`.
- Loader references `monitor too large`, `Monitor image not valid`, and `monitor loaded at virt: %lx, phys: %lx, entry: %lx`. It records a monitor entry/global at relative `0x757c8`; code near `0x190e0` also queries `monitor-size` from DeviceTree. Recovering these loader rules is possible from the local artifact.

The encrypted SEP original is retained with its exact SHA1/SHA256 in `inventory.json`. Decryption was not attempted because the displayed IV in the key catalog has 31 hex digits; guessing a missing digit is prohibited. Its actual source digest matches the SEP manifest digest, whereas the distinct RestoreSEP identity digest differs; both values are recorded without reporting false verification.

## Native monitor recovered from kernel payload

Subsequent complete payload inspection found the monitor embedded immediately after the compressed kernel stream, rather than as a separate IPSW member. The original AES-decrypted `krnl` payload is 7848896 bytes. Its complzss header declares compressed length 7766587; adding the 384-byte header gives monitor offset `0x7683bb`. The remaining 81925 bytes contain a complete 81920-byte ARM64 Mach-O plus five zero bytes of AES alignment padding.

Production extraction now uses that declared compressed extent and the validated Mach-O file-backed segment bounds, without searching for magic bytes or hardcoding the discovered offset. Missing, malformed or truncated monitors and unexpected trailing bytes fail preparation. Original and production `monitor.macho` files have SHA256 `1b2eade4ae032f92c79798749db39b9d279f3cf521c660b3de228a9277b81aa4`; text VM base is `0x4100000000`, entry `0x410000c120`.

The monitor's service table and native handler for XNU `SMC #0x11`, operation `0x800`, were independently examined by the backend researcher. This establishes a recoverable executable; it does not by itself establish full XNU or userspace boot.

## iBoot monitor handoff contract

The iBoot loader at file offset `0x15a00` allocates partition type 4, loads Mach-O segments, translates its entry to a physical address and places six 64-bit arguments at `monitorPhysicalBase + monitorReservedSize - 48`:

| Offset | Field | Evidence |
| ---: | --- | --- |
| 0 | version = **1** | `0x15aa4` loads 1; `0x15ab0` stores it |
| 8 | monitor virtual base | `0x15ab4` loads parsed Mach-O virtual base; `0x15ab8` stores it |
| 16 | monitor physical base | same `STP` stores allocator-returned physical base |
| 24 | monitor reserved partition size | `0x15abc` stores size from partition-type-4 accessor |
| 32 | kernel arguments pointer | completed by kernel handoff path; interpretation corroborated by native monitor entry |
| 40 | kernel entry pointer | completed by kernel handoff path; interpretation corroborated by native monitor entry |

The exact original DeviceTree `/defaults/monitor-size` is bytes `00 00 20 00`, little-endian **2 MiB**. `/defaults/sep-size` is `00 00 60 00`, **6 MiB**. iBoot partition selectors `0x19168` and `0x19240` choose monitor base/size for type 4. Code `0x190b8` sets SEP base to `0x800000000`; `0x1911c` computes monitor base as SEP base plus SEP size, yielding **`0x800600000`**. The loader uses monitor partition size, not whole DRAM size.

Code `0x190c0`–`0x190dc` deducts SEP size from usable memory and increments the kernel physical base; `0x19128`–`0x19144` repeats that for the monitor. Starting at DRAM `0x800000000` gives native kernel physical base **`0x800800000`**. `0x1914c` additionally reserves `0x805000` bytes from memory size for the bootloader. For a 1 GiB board the resulting usable size is `0x3effb000`. A loader should preserve these nonoverlapping reservations or justify and validate its relocation; placing a monitor in currently mapped kernel data is invalid.

The validated workspace bundle was upgraded by adding only monitor derivatives and manifest records. System/data disks were not changed. `vm_manifest.before-native-monitor.json` preserves the previous manifest.

See `firmware-artifacts/inventory.json` for boot-firmware paths, checksums, source digests, string offsets and instruction observations.
