# Native userspace FIPS POST diagnosis and resolution (2026-10-13)

## 1. Problem statement

In `backend/evidence/hfs-mount-20261013/post-launchd-15s/serial.log`, immediately after `/sbin/launchd` starts and mounts `/dev/disk0s1` on `/` and `/dev/disk0s2` on `/private/var`, userspace logged:

```
/dev/disk0s1 on / (hfs, local, read-only, journaled, noatime)
/dev/disk0s2 on /private/var (hfs, local, nodev, nosuid, noatime)
FIPS USER Space POST: Integrity test failed!
FIPS_POST failed!com.apple.launchd 1 System: Userspace shutdown begun at: Sun Feb  7 06:26:09 2106
```

This caused an immediate userspace shutdown before SpringBoard, backboardd, or any system services could initialize.

## 2. Disassembly analysis

Using our read-only HFSX parser (`hfs_inspect.py`), `/sbin/launchd`, `/bin/launchctl`, `/usr/libexec/cc_fips_test`, and `libcorecrypto.dylib` (from `dyld_shared_cache_arm64` image 627) were analyzed:

1. **/bin/launchctl bootstrap (`0x100003364..0x100003398`)**:
   - `/sbin/launchd` executes `/bin/launchctl bootstrap` as PID 2.
   - `launchctl` mounts `/` (read-only) and `/private/var` (read-write), executes `tzinit`, and then executes `/usr/libexec/cc_fips_test` as PID 3.
   - If `cc_fips_test` returns `-1`, `launchctl` calls `reboot(RB_HALT = 8)` at `0x10000393c`, triggering userspace shutdown.

2. **/usr/libexec/cc_fips_test (`0x100003cbc..0x100003cc4`)**:
   - Calls `_FIPS_POST(0)`.
   - If `_FIPS_POST` returns 0, it writes `"FIPS_POST failed!"` to `stderr` and exits with `-1`.

3. **libcorecrypto.dylib `_FIPS_POST` (`0x18e13be4c`)**:
   - First sub-test is `corecrypto_integrity_POST` at `0x18e13c018`.
   - If `corecrypto_integrity_POST` returns 0, `_FIPS_POST` prints `"FIPS USER Space POST: Integrity test failed!"` and returns 0, aborting further Known Answer Tests (KATs).

4. **libcorecrypto.dylib `corecrypto_integrity_POST` (`0x18e13c018`)**:
   - Calls `stat("/var/db/FIPS", &sb)` at `0x18e13c054..0x18e13c060`.
   - If absent, it calls `mkdir("/var/db/FIPS", 0775)` at `0x18e13c0a0..0x18e13c0ac`.
   - If `mkdir` returns `-1`, it branches immediately to `0x18e13c0b0` and returns `0`.
   - If `mkdir` succeeds (or `/var/db/FIPS` exists), it opens `/var/db/FIPS/fips_data`, queries `sysctlbyname("kern.osversion")`, computes HMAC-SHA256 (`get_hmac` at `0x18e13c660`) over `libcorecrypto.dylib` `__TEXT,__text` and `/usr/libexec/cc_fips_test`, writes HMAC (32 bytes) + OS version (6 bytes) via `writev`, and returns `1`.

## 3. Disproven SIO / SmartIO hypothesis

`SIO-COVERAGE.md` previously hypothesized that inline vs DMA selector `ctrl & 0x400` in SIO SmartIO AES was causing the failure. Disassembly and runtime tracing proved that `cc_fips_test` is a pure EL0 userspace software crypto test using `libcorecrypto.dylib` and does not interact with the SIO hardware mailbox at all.

## 4. Runtime breakpoint evidence

1. **Failure probe (`backend/evidence/hfs-mount-20261013/fipspost-mkdir-return/`)**:
   - Breakpoint set at kernel `SYS_mkdir` return VA `0xffffff8000390654`.
   - Captured:
     - `PC = 0xffffff8000390654`
     - `X00 = 0x0000000000000002` (`ENOENT = 2`)
     - `X25 = 0x88` (syscall 136 = `mkdir`)
     - `X24 = 0xffffff8081eafac0`, with `[X24 + 0x10] = 3` (PID 3, `cc_fips_test`, confirmed by `hmp-0.txt`)
   - Root cause: `namei` returned `ENOENT` on `mkdir("/var/db/FIPS", 0775)` because parent directory `/var/db` did not exist on the blank `/dev/disk0s2` partition mounted at `/private/var`.

2. **Success probe (`backend/evidence/hfs-mount-20261013/fipspost-mkdir-success/`)**:
   - Tested with a diagnostic Data volume populated with the `/private/var` template from `system.raw`.
   - Captured:
     - `PC = 0xffffff8000390654`
     - `X00 = 0x0000000000000000` (success = 0)
     - `X25 = 0x88` (syscall 136 = `mkdir`)
   - `mkdir("/var/db/FIPS", 0775)` succeeded with return code 0.

## 5. Progression beyond FIPS POST (`backend/evidence/hfs-mount-20261013/fipspost-resolution/`)

With the provisioned `/private/var` template:
1. `corecrypto_integrity_POST` completed successfully and wrote `/var/db/FIPS/fips_data`:
   - Length: 38 bytes
   - Data: `058cda4929c791bf4c53fb928de6e0e9c51d89e64ec62efa1f8ca132a1360b2c` (HMAC-SHA256) + `313144323537` (`"11D257"`).
2. All subsequent `_FIPS_POST` algorithm KATs passed (AES ECB, AES CBC, AES GCM, AES XTS, TDES CBC, SHA, HMAC, RSA, ECDSA, DRBG).
3. `FIPS USER Space POST: Integrity test failed!`, `FIPS_POST failed!`, and `Userspace shutdown begun` were **completely eliminated**.
4. System services initialized:
   - `tzinit` ran without symlink errors.
   - `lockdownd` (PID 51) generated RSA pairing keys (`device_private_key.pem`, `device_public_key.pem`).
   - `installd` registered preinstalled applications (`Calculator`, `MobileSafari`, `MobileMail`, `Weather`, etc.).
   - `notifyd`, `syslogd`, and `MobileInstallation` ran.
   - Execution reached the next bounded hardware/kernel blocker: `AppleH6CamIn::ISP_LoadFirmware_gated` / `AppleH6CamIn::ISP_Init` (no wake signal from ISPCPU, timeout `0xE00002D6`).

## 6. Preserved disk integrity

All diagnostic disk copies in `backend/evidence/hfs-mount-20261013/images/` were restored and verified against `disk-inputs.json`:
- `system.raw`: `c7a987439e75bb8e989f3e1ee76bea3f0fec7d9ae804e5ad1d618409f0524c30`
- `data.raw`: `7bc1b7e2daa3d7ffc9919057b98ba022bd78785275dfcad618b4697ec928f8f0`
- `utility.raw`: `2dd418e500f85eef74993892a5e9d508b7292f66d14ba4c945bee7c5fe4329e9`
