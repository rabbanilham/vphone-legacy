#!/usr/bin/env swift
// CoreWLAN Wi-Fi scan helper for vphone-legacy host network discovery.
// Outputs contract JSON to stdout:
// {"version":1,"updated":<epoch>,"networks":[{"ssid":...,"channel":1..14,"rssi":-100..-30,"security":"open|wpa|wpa2|wpa3|enterprise","connected":bool}]}
//
// One-time grant steps for macOS Location authorization (if SSIDs are redacted):
// macOS requires Location Services authorization to reveal Wi-Fi SSIDs to unprivileged callers.
// Without permission, CoreWLAN returns nil/empty SSIDs (<redacted>).
//
// 1. To package as a minimal app bundle with Info.plist:
//    mkdir -p build/host_wifi_scan.app/Contents/MacOS
//    cat << 'EOF' > build/host_wifi_scan.app/Contents/Info.plist
//    <?xml version="1.0" encoding="UTF-8"?>
//    <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
//    <plist version="1.0">
//    <dict>
//      <key>CFBundleIdentifier</key>
//      <string>com.vphone.host-wifi-scan</string>
//      <key>CFBundleExecutable</key>
//      <string>host_wifi_scan</string>
//      <key>NSLocationUsageDescription</key>
//      <string>vphone requires Wi-Fi network names to mirror nearby Wi-Fi in the guest iOS settings.</string>
//      <key>LSUIElement</key>
//      <true/>
//    </dict>
//    </plist>
//    EOF
//    swiftc -O -framework CoreLocation -framework CoreWLAN -framework Foundation scripts/host_wifi_scan.swift -o build/host_wifi_scan.app/Contents/MacOS/host_wifi_scan
//    codesign -s - --force build/host_wifi_scan.app
//
// 2. Grant authorization:
//    System Settings -> Privacy & Security -> Location Services -> Enable for Terminal (or host_wifi_scan).

import Foundation
import CoreWLAN
import AppKit
import CoreLocation

func isValidSSID(_ ssid: String?) -> Bool {
    guard let s = ssid?.trimmingCharacters(in: .whitespacesAndNewlines), !s.isEmpty else { return false }
    let lower = s.lowercased()
    if lower == "<redacted>" || lower == "redacted" || lower == "<hidden>" || lower == "n/a" {
        return false
    }
    let count = s.utf8.count
    return count >= 1 && count <= 32 && !s.contains("\0")
}

func mapChannel(_ channel: Int) -> Int {
    if channel <= 0 { return 1 }
    if channel >= 1 && channel <= 14 { return channel }
    return ((channel - 1) % 14) + 1
}

func clampRSSI(_ rssi: Int) -> Int {
    return max(-100, min(-30, rssi))
}

func mapSecurity(for net: CWNetwork) -> String {
    // Enterprise: WPA3Enterprise (12), WPA2Enterprise (9), WPAEnterpriseMixed (8), WPAEnterprise (7), Enterprise (10), DynamicWEP (6)
    let enterpriseRaws = [12, 9, 8, 7, 10, 6]
    for raw in enterpriseRaws {
        if let sec = CWSecurity(rawValue: raw), net.supportsSecurity(sec) {
            return "enterprise"
        }
    }
    // WPA3 Personal (11)
    if let sec = CWSecurity(rawValue: 11), net.supportsSecurity(sec) {
        return "wpa3"
    }
    // WPA2: WPA2Personal (4), Personal (5), WPA3Transition (13)
    let wpa2Raws = [4, 5, 13]
    for raw in wpa2Raws {
        if let sec = CWSecurity(rawValue: raw), net.supportsSecurity(sec) {
            return "wpa2"
        }
    }
    // WPA: WPAPersonal (2), WPAPersonalMixed (3)
    let wpaRaws = [2, 3]
    for raw in wpaRaws {
        if let sec = CWSecurity(rawValue: raw), net.supportsSecurity(sec) {
            return "wpa"
        }
    }
    // Open: None (0), WEP (1), OWE (14), OWETransition (15)
    return "open"
}

func mapCWSecurity(_ sec: CWSecurity) -> String {
    switch sec.rawValue {
    case 6, 7, 8, 9, 10, 12:
        return "enterprise"
    case 11:
        return "wpa3"
    case 4, 5, 13:
        return "wpa2"
    case 2, 3:
        return "wpa"
    case 0, 1, 14, 15:
        return "open"
    default:
        return "open"
    }
}

// Request Location authorization before scanning
class LocationAuthDelegate: NSObject, CLLocationManagerDelegate {
    var hasDetermined = false

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        hasDetermined = true
    }

    func locationManager(_ manager: CLLocationManager, didChangeAuthorization status: CLAuthorizationStatus) {
        hasDetermined = true
    }
}

// The system prompt only appears for a process with a window-server connection.
if CommandLine.arguments.contains("--authorize") {
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    app.finishLaunching()
    app.activate(ignoringOtherApps: true)
}
let locationManager = CLLocationManager()
let locationDelegate = LocationAuthDelegate()
locationManager.delegate = locationDelegate

let authStatus: CLAuthorizationStatus
if #available(macOS 11.0, *) {
    authStatus = locationManager.authorizationStatus
} else {
    authStatus = CLLocationManager.authorizationStatus()
}

if CommandLine.arguments.contains("--authorize") {
    fputs("Location services enabled: \(CLLocationManager.locationServicesEnabled()); authorization status: \(authStatus.rawValue) (0 notDetermined, 1 restricted, 2 denied, 3 always, 4 whenInUse)\n", stderr)
}
if authStatus == .notDetermined {
    if #available(macOS 10.15, *) {
        locationManager.requestWhenInUseAuthorization()
    } else {
        locationManager.requestAlwaysAuthorization()
    }
    // --authorize (run via `open`) keeps the process alive while the user answers the dialog.
    let waitSeconds: TimeInterval = CommandLine.arguments.contains("--authorize") ? 120 : 0.5
    let deadline = Date(timeIntervalSinceNow: waitSeconds)
    // The delegate also fires once when it is attached, so poll the real status instead.
    while Date() < deadline {
        RunLoop.current.run(mode: .default, before: Date(timeIntervalSinceNow: 0.2))
        let now: CLAuthorizationStatus
        if #available(macOS 11.0, *) { now = locationManager.authorizationStatus } else { now = CLLocationManager.authorizationStatus() }
        if now != .notDetermined { break }
    }
} else {
    RunLoop.current.run(mode: .default, before: Date(timeIntervalSinceNow: 0.05))
}

guard let iface = CWWiFiClient.shared().interface() else {
    fputs("Error: No Wi-Fi interface found\n", stderr)
    exit(1)
}

let currentSSID = iface.ssid()
let scanResults: Set<CWNetwork>
do {
    scanResults = try iface.scanForNetworks(withSSID: nil)
} catch {
    fputs("Error: Wi-Fi scan failed: \(error.localizedDescription)\n", stderr)
    exit(1)
}

// SSIDs are <redacted> without Location permission: detect nil/empty SSIDs and exit with code 2.
if scanResults.isEmpty && !isValidSSID(currentSSID) {
    fputs("Location authorization required or no networks found (exit code 2)\n", stderr)
    exit(2)
}

let validCount = scanResults.filter { isValidSSID($0.ssid) }.count
if !scanResults.isEmpty && validCount == 0 {
    fputs("Location authorization required: all scanned SSIDs are redacted or nil (exit code 2)\n", stderr)
    exit(2)
}

var networksBySSID = [String: (channel: Int, rssi: Int, security: String, connected: Bool)]()

for net in scanResults {
    guard let ssid = net.ssid, isValidSSID(ssid) else { continue }
    let ch = mapChannel(net.wlanChannel?.channelNumber ?? 1)
    let rssi = clampRSSI(net.rssiValue)
    let sec = mapSecurity(for: net)
    let isConnected = (currentSSID != nil && currentSSID == ssid)

    if let existing = networksBySSID[ssid] {
        if isConnected || (!existing.connected && rssi > existing.rssi) {
            networksBySSID[ssid] = (ch, rssi, sec, isConnected || existing.connected)
        }
    } else {
        networksBySSID[ssid] = (ch, rssi, sec, isConnected)
    }
}

// If connected network was not in scan results, include it.
if let curr = currentSSID, isValidSSID(curr), networksBySSID[curr] == nil {
    let ch = mapChannel(iface.wlanChannel()?.channelNumber ?? 6)
    let rssi = clampRSSI(iface.rssiValue())
    let sec = mapCWSecurity(iface.security())
    networksBySSID[curr] = (ch, rssi, sec, true)
}

var networkList = networksBySSID.map { (ssid, val) -> [String: Any] in
    return [
        "ssid": ssid,
        "channel": val.channel,
        "rssi": val.rssi,
        "security": val.security,
        "connected": val.connected
    ]
}

// Sort connected first, then highest signal (RSSI descending).
networkList.sort { (a, b) -> Bool in
    let aConn = a["connected"] as? Bool ?? false
    let bConn = b["connected"] as? Bool ?? false
    if aConn != bConn { return aConn }
    let aRssi = a["rssi"] as? Int ?? -100
    let bRssi = b["rssi"] as? Int ?? -100
    return aRssi > bRssi
}

// Ensure exactly one network is marked connected.
if !networkList.isEmpty {
    let hasConnected = networkList.contains { ($0["connected"] as? Bool) == true }
    if !hasConnected {
        networkList[0]["connected"] = true
    } else {
        var foundFirst = false
        for i in 0..<networkList.count {
            if (networkList[i]["connected"] as? Bool) == true {
                if !foundFirst {
                    foundFirst = true
                } else {
                    networkList[i]["connected"] = false
                }
            }
        }
    }
}

// Max 16 networks contract requirement.
let cappedList = Array(networkList.prefix(16))

let result: [String: Any] = [
    "version": 1,
    "updated": Int(Date().timeIntervalSince1970),
    "networks": cappedList
]

do {
    let jsonData = try JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys])
    if let jsonString = String(data: jsonData, encoding: .utf8) {
        // --output PATH: write the JSON to a file (used when launched through `open`, which has no stdout).
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--output"), i + 1 < args.count {
            try jsonString.write(toFile: args[i + 1], atomically: true, encoding: .utf8)
        } else {
            print(jsonString)
        }
    }
} catch {
    fputs("Error serializing scan result: \(error)\n", stderr)
    exit(1)
}
