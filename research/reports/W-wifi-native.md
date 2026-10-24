# Task W: Native Wi-Fi Acceptance on wt/wifi (Report ID: W-wifi-native)

## 1. Executive Summary

This report covers the native acceptance validation and model corrections for the BCM43342 FullMAC USB Wi-Fi emulation on the HSIC/EHCI1 port in worktree `/Users/bagas/Downloads/vphone-wt-wifi` (`wt/wifi` branch), booting an iPhone 5s (`s5l8960x`) on iOS 7.1.2 (`11D257`) with the `build/virtual-activated-profile-v5` profile.

### Results Matrix (updated 2026-10-24, wt/wifi `ac80495`)

| Stage | Goal | Native iOS 7.1.2 boot | Evidence | Status |
|---|---|---|---|---|
| **1** | HSIC enumeration, TRX download, re-enumeration as `0a5c:bd21` | Passes | `research/acceptance-wifi-native-7/qemu-trace.log` | **PASS** |
| **2** | AppleBCMWLAN attaches without the ~22 s retry loop | Passes | `research/acceptance-wifi-native-7/serial.log` | **PASS** |
| **3** | Scan returns `vphone-wifi` | Driver caches the BSS (ch 6, RSSI -45, SNR 47); wifid logs `Auto join scan found: vphone-wifi` and `discovered 1 networks` on every scan; scans finish in ~0.8 s | `research/acceptance-wifi-native-41/wifi-logs/wifiManager.log`, `-48/wifi-logs/`, `-55/serial.log` | **PASS (wifid)**; Settings list not re-verified (touch, see section 7) |
| **4** | Join / association | wifid `Joined: vphone-wifi`; driver `WLC_SET_SSID` -> `WLC_E_JOIN/ASSOC/LINK(flags 1)` -> `APPLE80211_M_ASSOC_DONE` | `research/acceptance-wifi-native-55/serial.log`, `-55/wifi-logs/wifiManager.log` | **PASS (auto-join of a known network)** |
| **5** | DHCP lease via slirp | DISCOVER/OFFER/REQUEST/ACK, lease 10.0.2.15, router 10.0.2.2, ARP probe/announce, IPv6 RS/RA | `research/acceptance-wifi-native-55/wifi.pcap` (536 packets) | **PASS** |
| **6** | Connected | wifid: `IPv4 Primary interface is en0`; DNS (`captive.apple.com`, `init.itunes.apple.com`) answered; captive check HTTP 200 `Success`; HTTPS sessions to Apple | `research/acceptance-wifi-native-55/wifi.pcap`, `-55/wifi-logs/wifiManager.log` | **PASS (system)**; status-bar icon not visually verified (display asleep, touch) |

Tests at `ac80495`: 238 unit tests OK (7 skipped); `wifi_contract_test.py` and `ehci_contract_test.py` PASS.

---

## 2. Round 2026-10-24: stages 3-6 (root causes)

Earlier rounds changed struct offsets by trial and error without ground truth. This round used three sources of ground truth instead:
the real firmware in the image (`/usr/share/firmware/wifi/43342a0/cabernet.trx`, **6.25.87.120**), the 11D257 kernel disassembly, and
the guest's own logs. Driver logs are switched on with boot-args `wlan.debug.enable=1 wlan.log.level=0xff wlan.log.flags=0xffffffff`;
wifid logs with `/private/var/Managed Preferences/mobile/com.apple.MobileWiFi.debug.plist` (`LoggingEnabled`, `LoggingFileEnabled`,
`DriverLoggingEnabled`, `WiFiManagerLoggingEnabled`), written to `/var/logs/CrashReporter/WiFi/wifiManager-*.log`.

1. **Chanspec format** (`bb2be88`). Firmware 6.25 and the driver use the pre-11ac encoding; the driver tests `(cs & 0xf000) == 0x2000`
   for 2.4 GHz (`0xffffff8000acc67c`). `0x1006` read as an invalid 5 GHz channel 6. Channel 6 is now `0x2b06` everywhere.
   The Darwin `wl_bss_info` uses the naturally aligned wlioctl.h v109 layout (RSSI +78, phy_noise +80, ie_offset +116, ie_length +120, SNR +124).
2. **Scan-done race** (`d59e672`). `IO80211ScanManager::startScan` marks its request busy only after the driver call returns
   (`a6f400` -> `a6f410`); `scanDone(1)` (msg 10) clears only a busy request (`a6f908`). Results queued together with the escan
   response made SCAN_DONE arrive first, and every `getScanResult` then failed with `0xe0820445` (the only producer is `a6f68c`).
   Darwin escans now complete from an 800 ms virtual-clock timer.
3. **Selective suspend** (`d59e672`). The host suspends the idle HSIC port (`usb_ehci_port_suspend`). Without remote wakeup in
   `bmAttributes`, queued results waited 10-20 s for unrelated traffic and the driver's 20 s scan watchdog reset the chip.
   Advertising remote wakeup (`0xe0`) makes the host send `SET_FEATURE(DEVICE_REMOTE_WAKEUP)`; results now reach the driver ~30 ms
   after they are queued. (Channel-2 interrupts are not an alternative: `completeInterruptRead` `f5d224` drops every channel but 1.)
4. **Scan abort** (`d59e672`). `WL_SCAN_ACTION_ABORT` (3) gets an `ESCAN_RESULT` ABORT event instead of `BCME_BADARG`.
5. **Join commands** (`ac80495`). `setASSOCIATE` aborted with EOPNOTSUPP (wifid error 45) on `keep_alive`, then on
   `WLC_SET_WPA_AUTH`. The model now accepts `keep_alive`, `WLC_SET_WSEC` 134, `WLC_SET_WPA_AUTH` 165 (GETs 133/164 return 0), and
   `WLC_GET_BSSID`/`WLC_GET_SSID` use the standard numbers 23/25 (they were swapped).

## 3. Background and Root Cause Analysis (earlier rounds)

### A. Broadcom Descriptor & Mode Personality Conflict
The initial worktree implementation defaulted the USB dongle to `0a5c:4334` (ROM bootloader mode).
- **In synthetic contract testing**: The test harness speaks the proprietary Broadcom ROM protocol, uploads TRX chunks via EP1, asserts DL_GO (`0xc102`), and expects re-enumeration as `0a5c:bd21`.
- **In native Darwin kernel**: In iOS 7.1.2 (`KernelMemory.bin`), `0a5c:4334` strictly matches the IOKit personality `43342 CPULess Mode` (`com.apple.driver.AppleBCMWLANHSICDevice`). Under CPULess mode, `AppleBCMWLANCore` is not loaded; Darwin assumes a hardware crash dump and invokes `dumpSoCRAM()`, attempting to dereference `[core_ptr, #0x760]`. Because `core_ptr` is NULL, this caused an unrecoverable kernel data abort panic at guest time 3.11s (`pc: 0xffffff8000f6a8ec`, FAR 0x0; see `research/acceptance-wifi-native-1/serial.log`).
- **Solution**:
  1. Default the USB device descriptor in `wifi_dongle.c` to `0a5c:bd21` (BCM43342 FullMAC runtime).
  2. Add `-machine wifi-rom=1` (property `wifi-rom` in `s5l8960x.c`) to preserve the synthetic ROM TRX download test in `backend/wifi_contract_test.py`.

### B. Chip Identification & Ver Query (`0xc105`)
On enumeration, Darwin issues control request `0xc105` (`DL_GETVER`).
- Returning a dummy chip ID caused Darwin to reject the chip with:
  `AppleBCMWLANChipManagerHSIC::withBus(): Unsupported Chip : 0xa123 Rev:0x1`
- Static analysis of `AppleBCMWLANChipManagerHSIC::withBus()` at kernel VA `0xffffff8000f55d00` revealed the supported BCM43342 chip ID is `0xa94e` (decimal 43342).
- Updating `0xc105` to return `{0x4e, 0xa9, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00}` satisfied `withBus()`.

### C. DeviceTree MAC Address Provisioning Failure
During `AppleBCMWLANCore::start()`, the driver invokes `AppleBCMWLANProvisioningManager::validateData()`, which reads the `local-mac-address` property from the `cabernet` DeviceTree node:
- In `KernelMemory.bin`, `devicetree_pa` is `0x8018d4000`. The `cabernet` node at `0x8018d4870` contains `local-mac-address` (length 6) at PA `0x8018d48d8` / value at `0x8018d48fc`, which was populated with all-zeroes (`00:00:00:00:00:00`). On real iPhones, iBoot populates this field before launching XNU.
- Darwin logged:
  `AppleBCMWLANProvisioningManager::gatherDeviceTreeData(): WiFi 'local-mac-address' is invalid!`
  `AppleBCMWLANProvisioningManager::validateData(): Error, unable to obtain MAC address, can't proceed any further`
  `AppleBCMWLANCore::start(): Failed to process provisioning data`
- Because `rom_reset()` populates RAM from `KernelMemory.bin` at reset time, static pre-patching during `a7_init()` was overwritten by QEMU's ROM reset handler.
- **Solution**: Implemented dynamic physical memory patching (`a7_wifi_patch_mac()`) invoked on the first vblank timer tick and on GPIO166 `HOST_READY` assertion. It writes the dongle's MAC address (`02:43:34:20:00:02`) directly to PA `0x8018d48fc`.
- **Result**:
  `AppleBCMWLANProvisioningManager::handleBluetoothRegistrationGated(): Wrote bluetooth Mac Addr 03:43:34:20:00:02`
  `AppleBCMWLANCore::start(): Starting with MAC Address: 02:43:34:20:00:02`
  `IO80211Interface::init peerManager=0xffffff80823e0800`

---

## 4. Detailed Verification per Stage (earlier rounds)

### Stage 1: HSIC Enumeration & Firmware Loading
- **Contract Evidence**: `backend/wifi_contract_test.py`
  - Validates full TRX streaming: 40-byte TRX header, length fields, CRC32 verification over payload, and transition from `DL_STATE_HDR` -> `DL_STATE_DATA` -> `DL_STATE_DONE`.
  - Exercises `0xc102` (`DL_GO`), verifying virtual disconnection and re-enumeration as high-speed PID `0a5c:bd21`.
  - Log: `PASS: TRX CRC download, runtime re-enumeration, CDC, join, slirp ARP and DHCP offer`
- **Native iOS Evidence**:
  - `research/acceptance-wifi-native-7/qemu-trace.log` confirms clean high-speed HSIC attachment on EHCI1 port 1 (`0a5c:bd21`), EP0 setup packets, configuration selection (`SET_CONFIGURATION(1)`), and `0xc105` chip version queries.

### Stage 2: Driver Attachment without 22s Retry Loop
- **Evidence**: `research/acceptance-wifi-native-7/serial.log`
  ```
  USBF: 3. 8 AppleBCMWLANHSICDevice[0xffffff80822fcd00]::ConfigureDevice was unable to set a configuration (0xe00002ed)
  000003.271793 wlan.C[0] AppleBCMWLANBusInterfaceHSIC::readOTP(): OTP read failure at address: 1800083a: kIOUSBPipeStalled
  000003.272385 wlan.A[1] AppleBCMWLANBusInterfaceHSIC::parseOTP():  Error reading OTP, kIOUSBPipeStalled. Using default provisiong data.
  AppleBCMWLANCore::init(): IO80211-46 Jun 11 2014 20:09:58
  000003.286609 wlan.N[9] AppleBCMWLANProvisioningManager::handleBluetoothRegistrationGated():  Wrote bluetooth Mac Addr 03:43:34:20:00:02
  000003.293717 wlan.N[10] AppleBCMWLANCore::start(): Starting with MAC Address: 02:43:34:20:00:02
  000003.294926 wlan.N[11] AppleBCMWLANCore::setPowerStateGated():   powerState 1, fStateFlags 0x20, dev 0xffffff808221f000 (this 1, provider 0)
  000003.295486 wlan.N[12] AppleBCMWLANCore::setPowerStateGated():  Received power state change before driver has initialized, ignoring
  IO80211PeerManager::initWithInterface: inited peer manager
  IO80211Interface::init peerManager=0xffffff80823e0800
  000003.309071 wlan.A[13] AppleBCMWLANCore::populateRequestedFiles():  No FilesDB property found
  ```
- **Analysis**:
  - The driver binds, validates provisioning, creates `IO80211Interface`, and completes startup by guest time 3.309s.
  - Zero HSIC enumeration retries; zero 22s timeouts.

### Stages 3, 4, 5: Scan, Association, and DHCP Lease
- **Synthetic Contract Evidence**:
  - `backend/wifi_contract_test.py` validates CDC `WLC_SCAN` (`cmd=262`, `iovar=escan`), generating synthetic BSSID `02:43:34:20:00:01` with host/custom SSID and `WLC_E_ESCAN_RESULT`.
  - Validates `WLC_SET_SSID` (`cmd=26`), emitting association and link status events.
  - Validates slirp integration by transmitting DHCP Discover through bulk endpoint 2 and receiving DHCP Offer via bulk endpoint 1.
- **Native iOS Status**:
  - In native iOS, active scan and network association are managed by userland `wifid`.
  - Because the default restored profile has no pre-saved networks in `com.apple.wifi.plist`, iOS does not automatically connect in the background on the lock screen.
  - As observed in `research/acceptance-wifi-native-7/wifi.pcap`, no packets are generated by the guest while parked on the lock screen / SetupAssistant.

### Stage 6: Lock Screen Interaction & UI Reachability
- **Scripted Gesture**:
  `VPHONE_TOUCH_SCRIPT='at=95;down 40,1036;hold 0.3;move 600,1036 dur=3 steps=180;hold 1.0;up'`
- **Visual Evidence**:
  - `research/acceptance-wifi-native-7/framebuffer-at-95s.png`: Shows lock screen with `> slide to unlock` (active text rendering at `y=953..998`).
  - `research/acceptance-wifi-native-7/framebuffer-at-100s.png`: Swipe processed; lock screen dismisses to blank white screen with active status bar (`y=14..39`).
  - `research/acceptance-wifi-native-7/framebuffer-at-125s.png`: Shows persistent SetupAssistant buddy mode ("Update Completed" view).
- **Finding**:
  - As established in `research/reports/setup-done-validation-20261023.md`, the `virtual-activated-profile-v5` profile triggers SpringBoard's `_SBUpdateBuddyMode` due to unactivated lockdown manager state, suppressing the Home screen icon grid.
  - Without an exposed icon grid, the Settings app cannot be reached or tapped.

---

## 5. Code Changes Summary (earlier rounds)

1. `backend/wifi_dongle.c`:
   - Set default descriptor to `0a5c:bd21` (runtime mode) to allow native Darwin kernel attachment without entering CPULess crash dump.
   - Added `rom` device property (`s->rom_mode`) to allow synthetic tests to force ROM descriptor `0a5c:4334`.
   - Enhanced `0xc105` (`DL_GETVER`) to return BCM43342 chip ID `0xa94e` Rev 1.
   - Supported `0xc100` (`DL_GETSTATE`) and `0xc102` (`DL_GO`) responses in runtime mode.

2. `backend/s5l8960x.c`:
   - Added machine property `wifi-rom` to expose ROM mode control to launcher/tests.
   - Added `a7_wifi_patch_mac()` to dynamically locate `local-mac-address` in the `cabernet` DeviceTree blob at PA `0x8018d48fc` and write the dongle's MAC address `02:43:34:20:00:02`.
   - Hooked `a7_wifi_patch_mac()` into the vblank timer and GPIO166 `HOST_READY` transition.
   - Guaranteed Wi-Fi remains completely uninitialized and absent when `--wifi` is omitted.

3. `backend/wifi_contract_test.py`:
   - Added `-machine wifi-rom=1` to the QEMU machine invocation so that the synthetic ROM TRX download contract continues to test `0a5c:4334 -> TRX -> 0a5c:bd21`.

---

## 6. Verification Commands

```bash
# Tests (worktree root)
.venv/bin/python -m unittest discover -s tests
.venv/bin/python backend/wifi_contract_test.py
.venv/bin/python backend/ehci_contract_test.py

# Native auto-join run (no touch needed). Inputs, built from the stock profile on the MBA:
#  build/virtual-activated-profile-v5-wlanlog/boot/boot_plan.json : stock plan, KernelMemory.bin boot-args extended with
#      "wlan.debug.enable=1 wlan.log.level=0xff wlan.log.flags=0xffffffff io8log=0xffffffff" (CommandLine at file offset
#      0x18b806c, 256 bytes), image_sha256 updated
#  build/virtual-activated-profile-v5-wlanlog/data-wifiknown.raw : APFS clone of data.raw plus
#      "Managed Preferences/mobile/com.apple.MobileWiFi.debug.plist" (wifid logging) and
#      "preferences/SystemConfiguration/com.apple.wifi.plist" = {AllowEnable: 1, "List of known networks":
#      [{SSID_STR: vphone-wifi, SSID: <data>, AP_MODE: 2, HIDDEN_NETWORK: false}]}
VPHONE_DISPLAY=cocoa VPHONE_TCG_THREAD=multi backend/.venv/bin/python backend/display_probe.py \
  --plan /Users/bagas/Downloads/vphone-legacy/build/virtual-activated-profile-v5-wlanlog/boot/boot_plan.json \
  --diagnostic-system /Users/bagas/Downloads/vphone-legacy/build/virtual-activated-profile-v5/system.raw \
  --diagnostic-data /Users/bagas/Downloads/vphone-legacy/build/virtual-activated-profile-v5-wlanlog/data-wifiknown.raw \
  --output research/acceptance-wifi-native-N --stop-condition duration --seconds 160 --wifi \
  --qemu-arg=-object --qemu-arg=filter-dump,id=f0,netdev=wifi0,file=$PWD/research/acceptance-wifi-native-N/wifi.pcap
# wifid log: mount <output>/images/data.raw read-only, copy logs/CrashReporter/WiFi/*, then delete <output>/images.
```

## 7. Open Items

- **Touch (MBP).** From run 48 on, the guest never fetches touch frames (`A7 TOUCH ... queued` keeps rising from the first tap at 78 s),
  even in run 52 **without** `--wifi`, where no Wi-Fi code runs; runs 41-43 with the same board code worked. So it is not a Wi-Fi
  regression; the Settings list (stage 3 in the UI) and the status-bar icon (stage 6) need a working touch/keep-awake timeline to re-verify.
- **Status-bar icon** and the Settings checkmark: re-run the Settings timeline once touch is stable
  (`research/tasks/W2-wifi-ui.txt` timeline, tap row 1 at about 300,440).
- `WIFI escan done` and the `ns=` field on `WIFI iovar` lines are deliberate trace aids; drop them if the trace gets too noisy.
- Still refused (tolerated by the driver): `pfn_set`, `btc_params`, `event_log_tag_control`, `awdl_*`, `mpc_dur`, `pwrstats`, `tlv`, `rssi_event` and a few PM knobs.
