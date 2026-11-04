import Foundation

public struct FixtureCLIClient: CLIClient, Sendable {
    public let fixturesDirectory: URL?
    public var simulateErrors: Bool

    public init(fixturesDirectory: URL? = nil, simulateErrors: Bool = false) {
        self.fixturesDirectory = fixturesDirectory
        self.simulateErrors = simulateErrors
    }

    // MARK: - Embedded Fallback Fixtures

    public static let setupCheckFixture = """
    {
      "schema": 1,
      "ok": false,
      "items": [
        {
          "id": "xcode-clt",
          "title": "Xcode Command Line Tools",
          "status": "ok",
          "detail": "/Library/Developer/CommandLineTools",
          "fix": null
        },
        {
          "id": "homebrew",
          "title": "Homebrew",
          "status": "ok",
          "detail": "/opt/homebrew/bin/brew",
          "fix": null
        },
        {
          "id": "brew-packages",
          "title": "Homebrew packages",
          "status": "fixable",
          "detail": "missing: keystone, libslirp",
          "fix": {
            "kind": "auto",
            "command": "brew install keystone libslirp",
            "instructions": null
          }
        },
        {
          "id": "backend",
          "title": "QEMU backend",
          "status": "missing",
          "detail": "backend/bin/qemu-system-aarch64 not found",
          "fix": {
            "kind": "auto",
            "command": "scripts/qemu/build_backend.py --fetch --install-build-deps",
            "instructions": null
          }
        },
        {
          "id": "wifi-helper",
          "title": "Wi-Fi scan helper (optional)",
          "status": "missing",
          "detail": "build/host_wifi_scan.app",
          "fix": {
            "kind": "auto",
            "command": "make wifi-helper",
            "instructions": null
          }
        }
      ]
    }
    """

    public static let vmListFixture = """
    {
      "schema": 1,
      "root": "/Users/me/.vphone-legacy/VMs",
      "vms": [
        {
          "name": "ios712",
          "path": "/Users/me/.vphone-legacy/VMs/ios712",
          "ios": "7.1.2",
          "build": "11D257",
          "device": "iPhone6,2",
          "profile": "ui",
          "memory_mb": 1024,
          "disk_bytes": 7300000000,
          "phase": "stopped",
          "pid": null,
          "boot_stage": "none",
          "apps": [
            {
              "bundle_id": "com.vphone.hello",
              "path": "/Applications/Hello.app"
            }
          ]
        },
        {
          "name": "baseline",
          "path": "/Users/me/.vphone-legacy/VMs/baseline",
          "ios": "7.1.2",
          "build": "11D257",
          "device": "iPhone6,2",
          "profile": "baseline",
          "memory_mb": 1024,
          "disk_bytes": 7100000000,
          "phase": "running",
          "pid": 4242,
          "boot_stage": "userspace",
          "apps": []
        }
      ]
    }
    """

    public static let vmStatusFixture = """
    {
      "schema": 1,
      "session_id": "7e0b",
      "process": {
        "phase": "running",
        "pid": 4242,
        "exit_code": null
      },
      "shutdown": {
        "clean": false,
        "reason": null
      },
      "log_directory": "logs/20261102-0001",
      "control": {
        "qmp": "/tmp/vpl-7e0b/qmp"
      },
      "boot_stage": "home",
      "display": "player",
      "lock": "held"
    }
    """

    public static let progressOkFixture = """
    {"schema": 1, "event": "stage", "id": "extract", "status": "done", "fraction": 1.0, "message": "Extracted 5 files", "hint": null}
    {"schema": 1, "event": "stage", "id": "manifest", "status": "done", "fraction": 1.0, "message": "Wrote vm_manifest.json", "hint": null}
    {"schema": 1, "event": "result", "ok": true, "data": {"name": "ios712", "path": "/Users/me/.vphone-legacy/VMs/ios712"}}
    """

    public static let progressErrorFixture = """
    {"schema": 1, "event": "stage", "id": "preflight", "status": "start", "fraction": null, "message": "Checking inputs", "hint": null}
    {"schema": 1, "event": "stage", "id": "preflight", "status": "done", "fraction": 1.0, "message": "IPSW is iPhone6,2 7.1.2 11D257", "hint": null}
    {"schema": 1, "event": "stage", "id": "decrypt-rootfs", "status": "progress", "fraction": 0.4, "message": "Decrypting RootFS", "hint": null}
    {"schema": 1, "event": "log", "message": "vfdecrypt: 1.46 GB processed"}
    {"schema": 1, "event": "stage", "id": "convert-rootfs", "status": "error", "fraction": null, "message": "qemu-img failed", "hint": "Install qemu with: brew install qemu"}
    {"schema": 1, "event": "result", "ok": false, "data": {"exit_code": 1}}
    """

    // MARK: - File Reading Helpers

    private func readFixtureString(named fileName: String, fallback: String) -> String {
        if let dir = fixturesDirectory {
            let fileURL = dir.appendingPathComponent(fileName)
            if let content = try? String(contentsOf: fileURL, encoding: .utf8) {
                return content
            }
        }
        let localPath = "tests/fixtures/gui/\(fileName)"
        if let content = try? String(contentsOfFile: localPath, encoding: .utf8) {
            return content
        }
        return fallback
    }

    public func rawSetupCheck() -> String {
        readFixtureString(named: "setup-check.json", fallback: Self.setupCheckFixture)
    }

    public func rawVMList() -> String {
        readFixtureString(named: "vm-list.json", fallback: Self.vmListFixture)
    }

    public func rawVMStatus() -> String {
        readFixtureString(named: "vm-status.json", fallback: Self.vmStatusFixture)
    }

    public func rawProgressOk() -> String {
        readFixtureString(named: "progress-ok.ndjson", fallback: Self.progressOkFixture)
    }

    public func rawProgressError() -> String {
        readFixtureString(named: "progress-error.ndjson", fallback: Self.progressErrorFixture)
    }

    // MARK: - CLIClient Protocol Methods

    public func setupCheck() async throws -> SetupCheckResponse {
        let content = rawSetupCheck()
        guard let data = content.data(using: .utf8) else {
            throw CLIError.invalidOutput("Could not read setup-check fixture data")
        }
        return try SetupCheckResponse.decode(from: data)
    }

    public func vmList() async throws -> VMListResponse {
        let content = rawVMList()
        guard let data = content.data(using: .utf8) else {
            throw CLIError.invalidOutput("Could not read vm-list fixture data")
        }
        return try VMListResponse.decode(from: data)
    }

    public func vmStatus(name: String) async throws -> VMStatus {
        let content = rawVMStatus()
        guard let data = content.data(using: .utf8) else {
            throw CLIError.invalidOutput("Could not read vm-status fixture data")
        }
        return try VMStatus.decode(from: data)
    }

    private func streamEvents(from ndjson: String, delayPerEvent: UInt64 = 20_000_000) -> AsyncStream<ProgressEvent> {
        let events = ProgressEvent.decodeLines(from: ndjson)
        return AsyncStream { continuation in
            Task {
                for event in events {
                    if Task.isCancelled { break }
                    continuation.yield(event)
                    if delayPerEvent > 0 {
                        try? await Task.sleep(nanoseconds: delayPerEvent)
                    }
                }
                continuation.finish()
            }
        }
    }

    public func setupInstall(id: String) -> AsyncStream<ProgressEvent> {
        let raw = simulateErrors ? rawProgressError() : rawProgressOk()
        return streamEvents(from: raw)
    }

    public func vmCreate(name: String, ios: String, ipswPath: String, force: Bool) -> AsyncStream<ProgressEvent> {
        let raw = simulateErrors ? rawProgressError() : rawProgressOk()
        return streamEvents(from: raw)
    }

    public func vmPrepareUI(name: String, force: Bool, ipswPath: String?) -> AsyncStream<ProgressEvent> {
        let raw = simulateErrors ? rawProgressError() : rawProgressOk()
        return streamEvents(from: raw)
    }

    public func vmLaunch(name: String, display: String?, wifi: Bool) async throws -> String {
        return "Fixture: Launched VM \(name)"
    }

    public func vmStop(name: String, force: Bool) async throws -> String {
        return "Fixture: Stopped VM \(name)"
    }

    public func vmDelete(name: String) async throws -> String {
        return "Fixture: Deleted VM \(name)"
    }

    public func vmImportProfile(name: String, fromPath: String) -> AsyncStream<ProgressEvent> {
        let raw = simulateErrors ? rawProgressError() : rawProgressOk()
        return streamEvents(from: raw)
    }

    public func appInstall(name: String, ipaPath: String, enableUnsigned: Bool) -> AsyncStream<ProgressEvent> {
        let raw = simulateErrors ? rawProgressError() : rawProgressOk()
        return streamEvents(from: raw)
    }
}
