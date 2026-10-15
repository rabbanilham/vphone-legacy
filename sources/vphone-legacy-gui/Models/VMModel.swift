import Foundation
import Combine
import SwiftUI
import Darwin

public struct VirtualMachineInfo: Identifiable, Hashable, Codable {
    public var id: String { name }
    public var name: String
    public var version: String
    public var device: String
    public var chip: String
    public var board: String
    public var memoryMB: Int
    public var cpu: String
    public var bootArgs: String
    public var path: String
    public var isRunning: Bool = false
}

public struct VMStageStatus: Decodable {
    public var phase: String = "unverified"
    public var evidence: String? = nil
    public var endpoint: AgentEndpoint? = nil

    public var description: String {
        if phase == "verified", evidence?.isEmpty != false { return "unverified" }
        return phase
    }
}

public struct AgentEndpoint: Decodable, Equatable {
    public let host: String
    public let port: UInt16
}

public struct VMRuntimeStatus: Decodable {
    public struct ProcessStatus: Decodable {
        public var phase: String
        public var pid: Int?
        public var exit_code: Int?
    }
    public struct ShutdownStatus: Decodable {
        public var clean: Bool?
        public var reason: String?
    }
    public var schema_version: Int
    public var process: ProcessStatus
    public var kernel: VMStageStatus
    public var userspace: VMStageStatus
    public var display: VMStageStatus
    public var agent: VMStageStatus
    public var shutdown: ShutdownStatus
}

public class VMManager: ObservableObject {
    @Published public var vms: [VirtualMachineInfo] = []
    @Published public var selectedVM: VirtualMachineInfo?
    @Published public var consoleOutput = ""
    // Launcher activity is not evidence of kernel, userspace, or display readiness.
    @Published public private(set) var isRunning = false
    @Published public private(set) var isStopping = false
    @Published public private(set) var isAgentConnected = false
    @Published public private(set) var agentDetails: [String: String] = [:]
    @Published public private(set) var runtimeStatus: VMRuntimeStatus?
    @Published public private(set) var activeVMName: String?

    private var runningProcess: Process?
    private var outputPipe: Pipe?
    private var runtimeTimer: Timer?
    private var launchTime: Date?
    private var activeVM: VirtualMachineInfo?
    private var sessionID = UUID()

    private var cliURL: URL {
        URL(fileURLWithPath: #file).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().appendingPathComponent("vphone-legacy")
    }

    public var backendCPUDescription: String {
        let url = cliURL.deletingLastPathComponent().appendingPathComponent("backend/backend.json")
        guard let data = try? Data(contentsOf: url),
              let metadata = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let cpu = metadata["cpu"] as? String, !cpu.isEmpty else { return "unverified" }
        return cpu
    }

    public var vmsDirectory: URL {
        if let configured = ProcessInfo.processInfo.environment["VPHONE_LEGACY_VMS_DIR"], !configured.isEmpty {
            return URL(fileURLWithPath: (configured as NSString).expandingTildeInPath, isDirectory: true)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".vphone-legacy/VMs")
    }

    public init() { refreshVMs() }

    public func refreshVMs() {
        let previousSelection = selectedVM?.name
        let items = (try? FileManager.default.contentsOfDirectory(atPath: vmsDirectory.path)) ?? []
        vms = items.sorted().compactMap { name in
            let path = vmsDirectory.appendingPathComponent(name)
            guard let data = try? Data(contentsOf: path.appendingPathComponent("vm_manifest.json")),
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
            return VirtualMachineInfo(
                name: name, version: json["version"] as? String ?? "unknown",
                device: json["device"] as? String ?? "unknown", chip: json["chip"] as? String ?? "unknown",
                board: json["board"] as? String ?? "unknown", memoryMB: json["memory_mb"] as? Int ?? 0,
                cpu: json["cpu"] as? String ?? "unknown", bootArgs: json["boot_args"] as? String ?? "",
                path: path.path, isRunning: isRunning && activeVMName == name)
        }
        selectedVM = vms.first(where: { $0.name == previousSelection }) ?? vms.first
        refreshRuntimeStatus()
    }

    public func createVM(name: String, version: String, ipswPath: String, completion: @escaping (Bool, String) -> Void) {
        let firmwarePath = ipswPath.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !firmwarePath.isEmpty else {
            completion(false, "A local IPSW path is required.")
            return
        }
        let args = ["vm", "create", name, "--ios", version, "--ipsw", firmwarePath]
        let executable = cliURL
        DispatchQueue.global(qos: .userInitiated).async {
            let result = Self.runCLI(executable: executable, arguments: args)
            DispatchQueue.main.async {
                self.refreshVMs()
                completion(result.0, result.1)
            }
        }
    }

    public func launchVM(vm: VirtualMachineInfo) {
        guard runningProcess == nil else { return }
        sessionID = UUID()
        let session = sessionID
        launchTime = Date()
        activeVM = vm
        activeVMName = vm.name
        runtimeStatus = nil
        invalidateAgent()
        consoleOutput = "[Manager] Starting launcher for '\(vm.name)'. Boot stages remain unverified until runtime evidence is available.\n"
        let task = Process()
        task.executableURL = cliURL
        task.arguments = ["vm", "launch", vm.name]
        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let output = String(data: data, encoding: .utf8) else { return }
            DispatchQueue.main.async {
                guard let self, self.sessionID == session else { return }
                self.consoleOutput += output
            }
        }
        task.terminationHandler = { [weak self] process in
            DispatchQueue.main.async {
                guard let self, self.sessionID == session else { return }
                pipe.fileHandleForReading.readabilityHandler = nil
                self.isRunning = false
                self.isStopping = false
                self.runningProcess = nil
                self.runtimeTimer?.invalidate()
                self.runtimeTimer = nil
                self.invalidateAgent()
                self.consoleOutput += "\n[Manager] Launcher exited with status \(process.terminationStatus).\n"
                self.refreshVMs()
            }
        }
        do {
            try task.run()
            runningProcess = task
            outputPipe = pipe
            isRunning = true
            refreshVMs()
            // This timer only reads structured runtime state; it never promotes a boot stage.
            runtimeTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
                self?.refreshRuntimeStatus()
            }
        } catch {
            pipe.fileHandleForReading.readabilityHandler = nil
            consoleOutput += "[Manager] Launch failed: \(error.localizedDescription)\n"
            isRunning = false
        }
    }

    public func stopVM(force: Bool = false) {
        guard let name = activeVMName, isRunning, !isStopping else { return }
        isStopping = true
        let executable = cliURL
        let session = sessionID
        let args = ["vm", "stop", name] + (force ? ["--force"] : [])
        DispatchQueue.global(qos: .userInitiated).async {
            let result = Self.runCLI(executable: executable, arguments: args)
            DispatchQueue.main.async {
                guard self.sessionID == session else { return }
                self.isStopping = false
                self.consoleOutput += result.1
                if !result.0 { self.consoleOutput += "[Manager] Stop request failed. The launcher remains active if the guest has not exited.\n" }
                self.refreshRuntimeStatus()
            }
        }
    }

    public func refreshRuntimeStatus() {
        guard let vm = (isRunning ? activeVM : selectedVM) else { runtimeStatus = nil; return }
        let url = URL(fileURLWithPath: vm.path).appendingPathComponent("runtime.json")
        if isRunning, let launchTime,
           let attributes = try? FileManager.default.attributesOfItem(atPath: url.path),
           let modified = attributes[.modificationDate] as? Date, modified < launchTime {
            return // Do not present evidence from an earlier launch as this session's status.
        }
        guard let data = try? Data(contentsOf: url),
              let status = try? JSONDecoder().decode(VMRuntimeStatus.self, from: data), status.schema_version == 1 else {
            runtimeStatus = nil
            invalidateAgent()
            return
        }
        runtimeStatus = status
        if status.process.phase != "running" || status.agent.description != "verified" || status.agent.endpoint == nil {
            invalidateAgent()
        }
    }

    private func invalidateAgent() {
        isAgentConnected = false
        agentDetails = [:]
    }

    private var configuredAgent: AgentEndpoint? {
        guard isRunning, runtimeStatus?.process.phase == "running",
              runtimeStatus?.agent.description == "verified",
              let endpoint = runtimeStatus?.agent.endpoint,
              endpoint.host == "127.0.0.1", endpoint.port > 0 else { return nil }
        return endpoint
    }

    public func verifyAgent(completion: @escaping (String) -> Void) {
        invalidateAgent()
        guard let endpoint = configuredAgent, let vm = activeVM else {
            completion("Unavailable: no verified guest transport in runtime.json.")
            return
        }
        let session = sessionID
        DispatchQueue.global(qos: .userInitiated).async {
            let response = Self.agentRequest(endpoint: endpoint, command: "file_read", args: ["path": "/System/Library/CoreServices/SystemVersion.plist"])
            var details: [String: String]?
            if response?["status"] as? String == "ok",
               let base64 = response?["data"] as? String, let data = Data(base64Encoded: base64) {
                details = Self.verifiedSystemVersion(data, expectedVersion: vm.version)
            }
            DispatchQueue.main.async {
                guard self.sessionID == session, self.configuredAgent == endpoint else {
                    completion("Unavailable: guest session changed during verification.")
                    return
                }
                if let details {
                    self.agentDetails = details
                    self.isAgentConnected = true
                    completion("Verified iPhone OS 7.1.2 (11D257) through the configured guest transport.")
                } else {
                    completion("Rejected: endpoint did not prove the expected iPhone OS identity. Darwin or pong alone is insufficient.")
                }
            }
        }
    }

    public func agentSend(cmd: String, args: [String: Any] = [:], completion: @escaping ([String: Any]?) -> Void) {
        guard isAgentConnected, let endpoint = configuredAgent else { completion(nil); return }
        let session = sessionID
        DispatchQueue.global(qos: .userInitiated).async {
            let response = Self.agentRequest(endpoint: endpoint, command: cmd, args: args)
            DispatchQueue.main.async {
                guard self.sessionID == session, self.configuredAgent == endpoint else { completion(nil); return }
                if response == nil { self.invalidateAgent() }
                completion(response)
            }
        }
    }

    static func verifiedSystemVersion(_ data: Data, expectedVersion: String) -> [String: String]? {
        guard let plist = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any],
              plist["ProductName"] as? String == "iPhone OS",
              plist["ProductVersion"] as? String == expectedVersion,
              expectedVersion == "7.1.2", plist["ProductBuildVersion"] as? String == "11D257" else { return nil }
        return ["ProductName": "iPhone OS", "ProductVersion": expectedVersion, "ProductBuildVersion": "11D257"]
    }

    private static func runCLI(executable: URL, arguments: [String]) -> (Bool, String) {
        let task = Process()
        task.executableURL = executable
        task.arguments = arguments
        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = pipe
        do {
            try task.run()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            task.waitUntilExit()
            return (task.terminationStatus == 0, String(data: data, encoding: .utf8) ?? "")
        } catch { return (false, "\(error.localizedDescription)\n") }
    }

    private static func agentRequest(endpoint: AgentEndpoint, command: String, args: [String: Any]) -> [String: Any]? {
        var request = args
        request["cmd"] = command
        request["id"] = 1
        guard let data = try? JSONSerialization.data(withJSONObject: request) else { return nil }
        let sock = socket(AF_INET, SOCK_STREAM, 0)
        guard sock >= 0 else { return nil }
        defer { close(sock) }
        var timeout = timeval(tv_sec: 2, tv_usec: 0)
        var noSIGPIPE: Int32 = 1
        setsockopt(sock, SOL_SOCKET, SO_NOSIGPIPE, &noSIGPIPE, socklen_t(MemoryLayout<Int32>.size))
        setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = endpoint.port.bigEndian
        inet_pton(AF_INET, endpoint.host, &address.sin_addr)
        let connected = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(sock, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) }
        }
        guard connected == 0 else { return nil }
        var length = UInt32(data.count).bigEndian
        var packet = Data(bytes: &length, count: 4)
        packet.append(data)
        guard packet.withUnsafeBytes({ bytes in
            var sent = 0
            while sent < bytes.count {
                let count = Darwin.write(sock, bytes.baseAddress!.advanced(by: sent), bytes.count - sent)
                guard count > 0 else { return false }
                sent += count
            }
            return true
        }) else { return nil }
        func receive(_ count: Int) -> Data? {
            var buffer = Data(count: count)
            let success = buffer.withUnsafeMutableBytes { bytes in
                var received = 0
                while received < count {
                    let n = Darwin.read(sock, bytes.baseAddress!.advanced(by: received), count - received)
                    guard n > 0 else { return false }
                    received += n
                }
                return true
            }
            return success ? buffer : nil
        }
        guard let header = receive(4) else { return nil }
        let count = header.reduce(0) { ($0 << 8) | Int($1) }
        guard count > 0, count <= 8 * 1024 * 1024, let body = receive(count),
              let response = try? JSONSerialization.jsonObject(with: body) as? [String: Any],
              response["id"] as? Int == 1 else { return nil }
        return response
    }
}
