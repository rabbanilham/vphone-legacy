# Task W (agy): native Wi-Fi acceptance on branch wt/wifi
Worktree: ${WORKTREES_ROOT}/vphone-wt-wifi (branch wt/wifi). Report id: W-wifi-native.

## Background
wt/wifi (4 Codex commits up to 46aa62a) emulates a BCM43342 FullMAC USB Wi-Fi on the HSIC/EHCI1 port with slirp networking (backend/wifi_dongle.c, wifi_protocol.h, host_wifi.py; README section "Optional BCM43342 HSIC Wi-Fi"). Synthetic contracts pass (`backend/wifi_contract_test.py`), but the native iOS boot acceptance has never been run.

## Step 1: bring the branch up to date
`git merge agx-meta-hle` (that branch is the main integration branch, now at e63a591). Expect conflicts in backend/live_run.py and backend/display_probe.py: keep BOTH the `--wifi` option and the new virtual-activated default + MTTCG default of live_run.py. Rebuild QEMU here, run `backend/wifi_contract_test.py`, `backend/ehci_contract_test.py` and `.venv/bin/python -m unittest discover -s tests`. Commit the merge.

## Step 2: native acceptance (virtual-activated profile, Cocoa window, `-d unimp` / qemu-trace)
Verify, in order, each with evidence:
1. HSIC enumeration: device 0a5c:4334, TRX download (length/CRC ok), DL_GO, re-enumeration as 0a5c:bd21.
2. AppleBCMWLAN driver attaches (serial/kernel log), without the old ~22 s HSIC retry loop.
3. A scan returns the synthetic AP whose SSID is the host SSID (or `VPHONE_WIFI_SSID`).
4. Join/association succeeds.
5. DHCP lease: use a QEMU `-object filter-dump,id=f0,netdev=wifi0,file=<out>/wifi.pcap` and show DISCOVER/OFFER/REQUEST/ACK; optionally show guest traffic (DNS) afterwards.
6. If reachable: unlock (scripted swipe) and open Settings -> Wi-Fi with taps to show the network connected (screenshot).
Fix model bugs you find on wt/wifi (keep Wi-Fi absent when --wifi is not given).

## Done when
Report with PASS/FAIL per stage 1-6 and evidence paths, contracts/tests passing, commits on wt/wifi.
