import Foundation

public struct ProcessCLIClient: CLIClient, @unchecked Sendable {
    public let repoRoot: URL
    public let vmsDir: URL?

    public init(repoRoot: URL, vmsDir: URL? = nil) {
        self.repoRoot = repoRoot
        self.vmsDir = vmsDir
    }

    private var cliURL: URL {
        repoRoot.appendingPathComponent("vphone-legacy")
    }

    private var processEnvironment: [String: String] {
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        env["VPHONE_LEGACY_ROOT"] = repoRoot.path
        if let vmsDir {
            env["VPHONE_LEGACY_VMS_DIR"] = vmsDir.path
        }
        return env
    }

    // MARK: - Process Execution

    private func runCommand(arguments: [String]) async throws -> (stdout: String, stderr: String, exitCode: Int32) {
        let task = Process()
        task.executableURL = cliURL
        task.arguments = arguments
        task.environment = processEnvironment

        let outPipe = Pipe()
        let errPipe = Pipe()
        task.standardOutput = outPipe
        task.standardError = errPipe

        try task.run()

        let outHandle = outPipe.fileHandleForReading
        let errHandle = errPipe.fileHandleForReading

        // Drain stderr concurrently: a child that fills either pipe while we block on the
        // other would otherwise stall (and so would anything it spawned).
        let errBox = DataBox()
        let errDone = DispatchSemaphore(value: 0)
        DispatchQueue.global().async {
            errBox.data = errHandle.readDataToEndOfFile()
            errDone.signal()
        }
        let outData = outHandle.readDataToEndOfFile()
        errDone.wait()
        let errData = errBox.data

        task.waitUntilExit()

        let stdoutStr = String(data: outData, encoding: .utf8) ?? ""
        let stderrStr = String(data: errData, encoding: .utf8) ?? ""
        return (stdoutStr, stderrStr, task.terminationStatus)
    }

    private func streamCommand(arguments: [String]) -> AsyncStream<ProgressEvent> {
        AsyncStream { continuation in
            let task = Process()
            task.executableURL = cliURL
            task.arguments = arguments
            task.environment = processEnvironment

            let outPipe = Pipe()
            let errPipe = Pipe()
            task.standardOutput = outPipe
            task.standardError = errPipe

            continuation.onTermination = { _ in
                if task.isRunning {
                    task.terminate()
                }
            }

            let stateLock = NSLock()
            var buffer = Data()
            var collectedStderr = ""
            var emittedResult = false

            errPipe.fileHandleForReading.readabilityHandler = { handle in
                let data = handle.availableData
                guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
                stateLock.lock()
                collectedStderr += text
                stateLock.unlock()
            }

            outPipe.fileHandleForReading.readabilityHandler = { handle in
                let data = handle.availableData
                guard !data.isEmpty else { return }

                stateLock.lock()
                buffer.append(data)

                var linesToProcess: [String] = []
                while let newlineIndex = buffer.firstIndex(of: UInt8(ascii: "\n")) {
                    let lineData = buffer.subdata(in: 0..<newlineIndex)
                    buffer.removeSubrange(0...newlineIndex)
                    if let lineStr = String(data: lineData, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines),
                       !lineStr.isEmpty {
                        linesToProcess.append(lineStr)
                    }
                }
                stateLock.unlock()

                for lineStr in linesToProcess {
                    if let event = try? ProgressEvent.decode(from: lineStr) {
                        stateLock.lock()
                        if event.isResult {
                            emittedResult = true
                        }
                        stateLock.unlock()
                        continuation.yield(event)
                    }
                }
            }

            task.terminationHandler = { process in
                outPipe.fileHandleForReading.readabilityHandler = nil
                errPipe.fileHandleForReading.readabilityHandler = nil

                stateLock.lock()
                let remaining = buffer
                buffer.removeAll()
                let finalStderr = collectedStderr
                let hadResult = emittedResult
                stateLock.unlock()

                if !remaining.isEmpty,
                   let lineStr = String(data: remaining, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines),
                   !lineStr.isEmpty,
                   let event = try? ProgressEvent.decode(from: lineStr) {
                    continuation.yield(event)
                }

                if !hadResult && process.terminationStatus != 0 {
                    let message = finalStderr.trimmingCharacters(in: .whitespacesAndNewlines)
                    continuation.yield(ProgressEvent(
                        schema: 1,
                        event: "result",
                        ok: false,
                        data: [
                            "exit_code": .int(Int(process.terminationStatus)),
                            "error": .string(message.isEmpty ? "Process exited with code \(process.terminationStatus)" : message)
                        ]
                    ))
                }

                continuation.finish()
            }

            do {
                try task.run()
            } catch {
                continuation.yield(ProgressEvent(
                    schema: 1,
                    event: "result",
                    ok: false,
                    data: ["error": .string(error.localizedDescription)]
                ))
                continuation.finish()
            }
        }
    }

    // MARK: - CLIClient Protocol Methods

    public func setupCheck() async throws -> SetupCheckResponse {
        let result = try await runCommand(arguments: ["setup", "check", "--json"])
        guard let data = result.stdout.data(using: .utf8), !data.isEmpty else {
            throw CLIError.invalidOutput(result.stderr.isEmpty ? "No output from setup check" : result.stderr)
        }
        return try SetupCheckResponse.decode(from: data)
    }

    public func setupInstall(id: String) -> AsyncStream<ProgressEvent> {
        streamCommand(arguments: ["setup", "install", id, "--progress-json"])
    }

    public func vmList() async throws -> VMListResponse {
        let result = try await runCommand(arguments: ["vm", "list", "--json"])
        guard result.exitCode == 0 else {
            throw CLIError.executionFailed(code: result.exitCode, stderr: result.stderr)
        }
        guard let data = result.stdout.data(using: .utf8), !data.isEmpty else {
            throw CLIError.invalidOutput(result.stderr.isEmpty ? "No output from vm list" : result.stderr)
        }
        return try VMListResponse.decode(from: data)
    }

    public func vmStatus(name: String) async throws -> VMStatus {
        let result = try await runCommand(arguments: ["vm", "status", name, "--json"])
        guard result.exitCode == 0 else {
            throw CLIError.executionFailed(code: result.exitCode, stderr: result.stderr)
        }
        guard let data = result.stdout.data(using: .utf8), !data.isEmpty else {
            throw CLIError.invalidOutput(result.stderr.isEmpty ? "No output from vm status" : result.stderr)
        }
        return try VMStatus.decode(from: data)
    }

    public func vmCreate(name: String, ios: String, ipswPath: String, force: Bool) -> AsyncStream<ProgressEvent> {
        var args = ["vm", "create", name, "--ios", ios, "--ipsw", ipswPath, "--progress-json"]
        if force {
            args.append("--force")
        }
        return streamCommand(arguments: args)
    }

    public func vmPrepareUI(name: String, force: Bool, ipswPath: String?) -> AsyncStream<ProgressEvent> {
        var args = ["vm", "prepare-ui", name, "--progress-json"]
        if let ipswPath, !ipswPath.isEmpty {
            args.append(contentsOf: ["--ipsw", ipswPath])
        }
        if force {
            args.append("--force")
        }
        return streamCommand(arguments: args)
    }

    public func vmLaunch(name: String, display: String?, wifi: Bool) async throws -> String {
        var args = ["vm", "launch", name]
        if let display, !display.isEmpty {
            args.append(contentsOf: ["--display", display])
        }
        if wifi {
            args.append("--wifi")
        }
        let result = try await runCommand(arguments: args)
        if result.exitCode != 0 {
            throw CLIError.executionFailed(code: result.exitCode, stderr: result.stderr)
        }
        return result.stdout.isEmpty ? result.stderr : result.stdout
    }

    public func vmStop(name: String, force: Bool) async throws -> String {
        var args = ["vm", "stop", name]
        if force {
            args.append("--force")
        }
        let result = try await runCommand(arguments: args)
        if result.exitCode != 0 {
            throw CLIError.executionFailed(code: result.exitCode, stderr: result.stderr)
        }
        return result.stdout.isEmpty ? result.stderr : result.stdout
    }

    public func vmDelete(name: String) async throws -> String {
        let result = try await runCommand(arguments: ["vm", "delete", name, "--json"])
        if result.exitCode != 0 {
            throw CLIError.executionFailed(code: result.exitCode, stderr: result.stderr)
        }
        return result.stdout
    }

    public func vmImportProfile(name: String, fromPath: String) -> AsyncStream<ProgressEvent> {
        streamCommand(arguments: ["vm", "import-profile", name, "--from", fromPath, "--progress-json"])
    }

    public func appInstall(name: String, ipaPath: String, enableUnsigned: Bool) -> AsyncStream<ProgressEvent> {
        var args = ["app", "install", name, ipaPath, "--progress-json"]
        if enableUnsigned {
            args.append("--enable-unsigned")
        }
        return streamCommand(arguments: args)
    }
}

private final class DataBox: @unchecked Sendable {
    var data = Data()
}
