import Foundation
import SwiftUI
import Observation

/// Result of a detached `vm launch` call, shared between the launch task and the watcher.
final class LaunchOutcome: @unchecked Sendable {
    private let lock = NSLock()
    private var failure: String?
    private var finished = false
    private var checks = 0

    func record(error: String) { lock.lock(); failure = error; lock.unlock() }
    func markFinished() { lock.lock(); finished = true; lock.unlock() }
    var error: String? { lock.lock(); defer { lock.unlock() }; return failure }
    var isFinished: Bool { lock.lock(); defer { lock.unlock() }; return finished }
    /// A launcher stuck in `starting` for several polls is still considered up (the player is booting).
    func elapsedLaunchCheck() -> Bool { lock.lock(); defer { lock.unlock() }; checks += 1; return checks >= 6 }
}

@Observable
@MainActor
public final class AppState {
    public var repoLocator: RepoLocator
    public var client: CLIClient

    public var vms: [VMInfo] = []
    public var selectedVMName: String?
    public var setupCheck: SetupCheckResponse?
    public var isCheckingSetup: Bool = false
    public var isRefreshingVMs: Bool = false
    public var vmStatuses: [String: VMStatus] = [:]
    public var activeOperations: [String: String] = [:]
    public var errorMessage: String?
    /// Set by a card's Recreate button; LibraryView opens the New VM sheet prefilled with this name.
    public var recreateRequest: String?
    public var vmLogs: [String: String] = [:]

    // Wi-Fi and AMFI setting toggles per VM name
    public var vmWifiSettings: [String: Bool] = [:]
    public var vmAmfiSettings: [String: Bool] = [:]

    private var pollingTasks: [String: Task<Void, Never>] = [:]

    public init(repoLocator: RepoLocator = RepoLocator(), client: CLIClient? = nil) {
        self.repoLocator = repoLocator
        if let client {
            self.client = client
        } else if repoLocator.isValidRepo, let root = repoLocator.repoRoot {
            self.client = ProcessCLIClient(repoRoot: root, vmsDir: repoLocator.vmsDirectory)
        } else {
            self.client = FixtureCLIClient()
        }
    }

    public var selectedVM: VMInfo? {
        if let selectedVMName {
            return vms.first(where: { $0.name == selectedVMName })
        }
        return vms.first
    }

    public var isSetupValid: Bool {
        repoLocator.isValidRepo && (setupCheck?.ok ?? true)
    }

    public func updateClient() {
        if repoLocator.isValidRepo, let root = repoLocator.repoRoot {
            self.client = ProcessCLIClient(repoRoot: root, vmsDir: repoLocator.vmsDirectory)
        } else {
            self.client = FixtureCLIClient()
        }
    }

    public func checkSetup() async {
        isCheckingSetup = true
        errorMessage = nil
        do {
            let res = try await client.setupCheck()
            self.setupCheck = res
        } catch {
            self.errorMessage = "Setup check failed: \(error.localizedDescription)"
        }
        isCheckingSetup = false
    }

    public func refreshVMs() async {
        guard repoLocator.isValidRepo else {
            if client is FixtureCLIClient {
                if let list = try? await client.vmList() {
                    self.vms = list.vms
                    if selectedVMName == nil { selectedVMName = vms.first?.name }
                }
            }
            return
        }
        isRefreshingVMs = true
        do {
            let list = try await client.vmList()
            self.vms = list.vms
            if selectedVMName == nil || !vms.contains(where: { $0.name == selectedVMName }) {
                selectedVMName = vms.first?.name
            }
            // Update statuses for all VMs
            for vm in vms {
                await updateStatus(for: vm.name)
            }
        } catch {
            self.errorMessage = "Failed to list VMs: \(error.localizedDescription)"
        }
        isRefreshingVMs = false
    }

    public func updateStatus(for vmName: String) async {
        do {
            let status = try await client.vmStatus(name: vmName)
            self.vmStatuses[vmName] = status
            if let idx = vms.firstIndex(where: { $0.name == vmName }) {
                if let proc = status.process {
                    vms[idx].phase = proc.phase
                    vms[idx].pid = proc.pid
                }
                if let bs = status.boot_stage {
                    vms[idx].boot_stage = bs
                }
            }
        } catch {
            // Ignore background refresh errors
        }
    }

    public func startMonitoring(for vmName: String) {
        pollingTasks[vmName]?.cancel()
        pollingTasks[vmName] = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { break }
                await self.updateStatus(for: vmName)
                await self.tailLog(for: vmName)

                let currentPhase = self.vmStatuses[vmName]?.process?.phase
                if currentPhase == "stopped" || currentPhase == "failed" {
                    // Update once more and stop polling
                    await self.refreshVMs()
                    break
                }
                try? await Task.sleep(nanoseconds: 1_000_000_000)
            }
        }
    }

    public func stopMonitoring(for vmName: String) {
        pollingTasks[vmName]?.cancel()
        pollingTasks.removeValue(forKey: vmName)
    }

    /// `vm launch` stays alive for the whole VM session (the launcher owns QEMU and the
    /// player), so it must never be awaited: watch the launcher's status instead and only
    /// surface the CLI's message if it exits before the VM is up.
    public func launchVM(name: String) async {
        guard let vm = vms.first(where: { $0.name == name }) else { return }
        let wifi = vmWifiSettings[name] ?? false
        let display = vm.isUiProfile ? "player" : "cocoa"
        errorMessage = nil
        activeOperations[name] = "Starting..."
        let outcome = LaunchOutcome()
        let client = self.client
        Task.detached {
            do {
                _ = try await client.vmLaunch(name: name, display: display, wifi: wifi)
            } catch {
                outcome.record(error: error.localizedDescription)
            }
            outcome.markFinished()
        }
        var running = false
        for _ in 0..<80 {
            try? await Task.sleep(nanoseconds: 500_000_000)
            if outcome.isFinished { break }
            await updateStatus(for: name)
            let phase = vmStatuses[name]?.process?.phase
            if phase == "running" || (phase == "starting" && outcome.elapsedLaunchCheck()) {
                running = true
                break
            }
        }
        if let failure = outcome.error {
            errorMessage = "Could not start \(name): \(failure)"
        } else if !running && !outcome.isFinished {
            errorMessage = "Could not start \(name): the launcher did not report a running VM within 40 seconds."
        }
        activeOperations.removeValue(forKey: name)
        await refreshVMs()
        if running {
            startMonitoring(for: name)
        }
    }

    public func stopVM(name: String, force: Bool = false) async {
        activeOperations[name] = force ? "Force Stopping..." : "Stopping..."
        do {
            _ = try await client.vmStop(name: name, force: force)
            await updateStatus(for: name)
        } catch {
            self.errorMessage = "Stop failed: \(error.localizedDescription)"
        }
        activeOperations.removeValue(forKey: name)
    }

    public func deleteVM(name: String) async {
        stopMonitoring(for: name)
        do {
            _ = try await client.vmDelete(name: name)
            await refreshVMs()
        } catch {
            self.errorMessage = "Delete failed: \(error.localizedDescription)"
        }
    }

    public func tailLog(for vmName: String) async {
        guard let vm = vms.first(where: { $0.name == vmName }) else { return }
        let vmPath = URL(fileURLWithPath: vm.path)

        // Try serial.log or logs directory
        let serialLog = vmPath.appendingPathComponent("serial.log")
        if let data = try? Data(contentsOf: serialLog) {
            let maxBytes = 100_000
            let slice = data.count > maxBytes ? data.suffix(maxBytes) : data
            if let str = String(data: slice, encoding: .utf8) {
                self.vmLogs[vmName] = str
                return
            }
        }

        // Try logs/ directory
        let logsDir = vmPath.appendingPathComponent("logs")
        if let items = try? FileManager.default.contentsOfDirectory(atPath: logsDir.path).sorted() {
            if let latest = items.last {
                let latestSerial = logsDir.appendingPathComponent(latest).appendingPathComponent("serial.log")
                if let data = try? Data(contentsOf: latestSerial) {
                    let maxBytes = 100_000
                    let slice = data.count > maxBytes ? data.suffix(maxBytes) : data
                    if let str = String(data: slice, encoding: .utf8) {
                        self.vmLogs[vmName] = str
                        return
                    }
                }
            }
        }
    }
}
