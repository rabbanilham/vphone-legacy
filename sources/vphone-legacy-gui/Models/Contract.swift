import Foundation

// MARK: - AnyCodableValue

/// Polymorphic container for arbitrary JSON values within event payloads.
public enum AnyCodableValue: Codable, Equatable, Hashable, Sendable {
    case string(String)
    case int(Int)
    case double(Double)
    case bool(Bool)
    case object([String: AnyCodableValue])
    case array([AnyCodableValue])
    case null

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let boolVal = try? container.decode(Bool.self) {
            self = .bool(boolVal)
        } else if let intVal = try? container.decode(Int.self) {
            self = .int(intVal)
        } else if let doubleVal = try? container.decode(Double.self) {
            self = .double(doubleVal)
        } else if let stringVal = try? container.decode(String.self) {
            self = .string(stringVal)
        } else if let dictVal = try? container.decode([String: AnyCodableValue].self) {
            self = .object(dictVal)
        } else if let arrVal = try? container.decode([AnyCodableValue].self) {
            self = .array(arrVal)
        } else {
            throw DecodingError.dataCorruptedError(
                in: container,
                debugDescription: "Cannot decode AnyCodableValue"
            )
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .string(let s):
            try container.encode(s)
        case .int(let i):
            try container.encode(i)
        case .double(let d):
            try container.encode(d)
        case .bool(let b):
            try container.encode(b)
        case .object(let o):
            try container.encode(o)
        case .array(let a):
            try container.encode(a)
        case .null:
            try container.encodeNil()
        }
    }

    public var stringValue: String? {
        if case .string(let s) = self { return s }
        return nil
    }

    public var intValue: Int? {
        if case .int(let i) = self { return i }
        return nil
    }

    public var boolValue: Bool? {
        if case .bool(let b) = self { return b }
        return nil
    }
}

// MARK: - Progress / NDJSON Contract

/// Represents a single line event from `--progress-json` output.
public struct ProgressEvent: Codable, Equatable, Hashable, Sendable {
    public let schema: Int?
    public let event: String             // "stage" | "log" | "result" | "error"
    public let id: String?
    public let status: String?           // "start" | "progress" | "done" | "skipped" | "error"
    public let fraction: Double?
    public let message: String?
    public let hint: String?
    public let ok: Bool?
    public let data: [String: AnyCodableValue]?

    public init(
        schema: Int? = 1,
        event: String,
        id: String? = nil,
        status: String? = nil,
        fraction: Double? = nil,
        message: String? = nil,
        hint: String? = nil,
        ok: Bool? = nil,
        data: [String: AnyCodableValue]? = nil
    ) {
        self.schema = schema
        self.event = event
        self.id = id
        self.status = status
        self.fraction = fraction
        self.message = message
        self.hint = hint
        self.ok = ok
        self.data = data
    }

    public var isStage: Bool { event == "stage" }
    public var isLog: Bool { event == "log" }
    public var isResult: Bool { event == "result" }
    public var isError: Bool { event == "error" || (event == "stage" && status == "error") }

    public static func decode(from data: Data) throws -> ProgressEvent {
        try JSONDecoder().decode(ProgressEvent.self, from: data)
    }

    public static func decode(from line: String) throws -> ProgressEvent {
        guard let data = line.data(using: .utf8) else {
            throw DecodingError.dataCorrupted(
                DecodingError.Context(codingPath: [], debugDescription: "Invalid UTF-8 line")
            )
        }
        return try decode(from: data)
    }

    public static func decodeLines(from ndjson: String) -> [ProgressEvent] {
        ndjson.components(separatedBy: .newlines)
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
            .compactMap { try? decode(from: $0) }
    }
}

// MARK: - VM List Contract

public struct VMAppInfo: Codable, Equatable, Hashable, Sendable {
    public let bundle_id: String
    public let path: String

    public init(bundle_id: String, path: String) {
        self.bundle_id = bundle_id
        self.path = path
    }

    public var bundleId: String { bundle_id }
}

public struct VMInfo: Codable, Identifiable, Equatable, Hashable, Sendable {
    public var id: String { name }
    public let name: String
    public let path: String
    public let ios: String
    public let build: String
    public let device: String
    public let profile: String          // "baseline" | "ui"
    public let memory_mb: Int
    public let disk_bytes: Int64
    public var phase: String            // "stopped" | "starting" | "running" | "stopping" | "failed"
    public var pid: Int?
    public var boot_stage: String       // "none" | "kernel" | "userspace" | "lockscreen" | "home" | "panic"
    public var apps: [VMAppInfo]

    public init(
        name: String,
        path: String,
        ios: String,
        build: String,
        device: String,
        profile: String,
        memory_mb: Int,
        disk_bytes: Int64,
        phase: String,
        pid: Int? = nil,
        boot_stage: String = "none",
        apps: [VMAppInfo] = []
    ) {
        self.name = name
        self.path = path
        self.ios = ios
        self.build = build
        self.device = device
        self.profile = profile
        self.memory_mb = memory_mb
        self.disk_bytes = disk_bytes
        self.phase = phase
        self.pid = pid
        self.boot_stage = boot_stage
        self.apps = apps
    }

    public var isRunning: Bool { phase == "running" }
    public var isUiProfile: Bool { profile == "ui" }
    public var memoryMB: Int { memory_mb }
    public var diskBytes: Int64 { disk_bytes }
    public var bootStage: String { boot_stage }

    public var formattedDiskSize: String {
        let formatter = ByteCountFormatter()
        formatter.allowedUnits = [.useGB, .useMB]
        formatter.countStyle = .file
        return formatter.string(fromByteCount: disk_bytes)
    }
}

public struct VMListResponse: Codable, Equatable, Sendable {
    public let schema: Int?
    public let root: String
    public let vms: [VMInfo]

    public init(schema: Int? = 1, root: String, vms: [VMInfo]) {
        self.schema = schema
        self.root = root
        self.vms = vms
    }

    public static func decode(from data: Data) throws -> VMListResponse {
        try JSONDecoder().decode(VMListResponse.self, from: data)
    }
}

// MARK: - VM Status Contract

public struct VMProcessStatus: Codable, Equatable, Hashable, Sendable {
    public let phase: String
    public let pid: Int?
    public let exit_code: Int?

    public init(phase: String, pid: Int? = nil, exit_code: Int? = nil) {
        self.phase = phase
        self.pid = pid
        self.exit_code = exit_code
    }
}

public struct VMShutdownStatus: Codable, Equatable, Hashable, Sendable {
    public let clean: Bool
    public let reason: String?

    public init(clean: Bool, reason: String? = nil) {
        self.clean = clean
        self.reason = reason
    }
}

public struct VMControlStatus: Codable, Equatable, Hashable, Sendable {
    public let qmp: String?

    public init(qmp: String? = nil) {
        self.qmp = qmp
    }
}

public struct VMStatus: Codable, Equatable, Hashable, Sendable {
    public let schema: Int?
    public let session_id: String?
    public let process: VMProcessStatus?
    public let shutdown: VMShutdownStatus?
    public let log_directory: String?
    public let control: VMControlStatus?
    public let boot_stage: String?
    public let display: String?
    public let lock: String?

    public init(
        schema: Int? = 1,
        session_id: String? = nil,
        process: VMProcessStatus? = nil,
        shutdown: VMShutdownStatus? = nil,
        log_directory: String? = nil,
        control: VMControlStatus? = nil,
        boot_stage: String? = nil,
        display: String? = nil,
        lock: String? = nil
    ) {
        self.schema = schema
        self.session_id = session_id
        self.process = process
        self.shutdown = shutdown
        self.log_directory = log_directory
        self.control = control
        self.boot_stage = boot_stage
        self.display = display
        self.lock = lock
    }

    public var sessionId: String? { session_id }
    public var bootStage: String? { boot_stage }
    public var logDirectory: String? { log_directory }
    public var isProcessRunning: Bool { process?.phase == "running" }

    public static func decode(from data: Data) throws -> VMStatus {
        try JSONDecoder().decode(VMStatus.self, from: data)
    }
}

// MARK: - Setup Check Contract

public struct SetupFix: Codable, Equatable, Hashable, Sendable {
    public let kind: String       // "auto" | "manual"
    public let command: String?
    public let instructions: String?

    public init(kind: String, command: String? = nil, instructions: String? = nil) {
        self.kind = kind
        self.command = command
        self.instructions = instructions
    }

    public var isAuto: Bool { kind == "auto" }
}

public struct SetupItem: Codable, Identifiable, Equatable, Hashable, Sendable {
    public let id: String
    public let title: String
    public let status: String     // "ok" | "missing" | "fixable"
    public let detail: String?
    public let fix: SetupFix?

    public init(
        id: String,
        title: String,
        status: String,
        detail: String? = nil,
        fix: SetupFix? = nil
    ) {
        self.id = id
        self.title = title
        self.status = status
        self.detail = detail
        self.fix = fix
    }

    public var isOk: Bool { status == "ok" }
    public var isFixable: Bool { status == "fixable" }
    public var isMissing: Bool { status == "missing" }
}

public struct SetupCheckResponse: Codable, Equatable, Sendable {
    public let schema: Int?
    public let ok: Bool
    public let items: [SetupItem]

    public init(schema: Int? = 1, ok: Bool, items: [SetupItem]) {
        self.schema = schema
        self.ok = ok
        self.items = items
    }

    public static func decode(from data: Data) throws -> SetupCheckResponse {
        try JSONDecoder().decode(SetupCheckResponse.self, from: data)
    }
}
