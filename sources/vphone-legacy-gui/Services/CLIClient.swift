import Foundation

public enum CLIError: LocalizedError, Equatable {
    case executionFailed(code: Int32, stderr: String)
    case invalidOutput(String)
    case commandTimedOut
    case cancelled

    public var errorDescription: String? {
        switch self {
        case .executionFailed(let code, let stderr):
            return "CLI execution failed (exit code \(code)): \(stderr)"
        case .invalidOutput(let details):
            return "Invalid CLI output: \(details)"
        case .commandTimedOut:
            return "CLI operation timed out."
        case .cancelled:
            return "Operation was cancelled."
        }
    }
}

public protocol CLIClient: Sendable {
    func setupCheck() async throws -> SetupCheckResponse
    func setupInstall(id: String) -> AsyncStream<ProgressEvent>
    func vmList() async throws -> VMListResponse
    func vmStatus(name: String) async throws -> VMStatus
    func vmCreate(name: String, ios: String, ipswPath: String, force: Bool) -> AsyncStream<ProgressEvent>
    func vmPrepareUI(name: String, force: Bool) -> AsyncStream<ProgressEvent>
    func vmLaunch(name: String, display: String?, wifi: Bool) async throws -> String
    func vmStop(name: String, force: Bool) async throws -> String
    func vmDelete(name: String) async throws -> String
    func vmImportProfile(name: String, fromPath: String) -> AsyncStream<ProgressEvent>
    func appInstall(name: String, ipaPath: String, enableUnsigned: Bool) -> AsyncStream<ProgressEvent>
}
