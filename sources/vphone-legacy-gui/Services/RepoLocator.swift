import Foundation
import AppKit
import Observation

@Observable
public final class RepoLocator {
    public var repoRoot: URL? {
        didSet {
            if let repoRoot {
                UserDefaults.standard.set(repoRoot.path, forKey: "repoRoot")
            } else {
                UserDefaults.standard.removeObject(forKey: "repoRoot")
            }
        }
    }

    public init() {
        if let saved = UserDefaults.standard.string(forKey: "repoRoot"), !saved.isEmpty {
            let url = URL(fileURLWithPath: saved)
            if Self.validate(url: url) {
                self.repoRoot = url
                return
            }
        }

        if let envRoot = ProcessInfo.processInfo.environment["VPHONE_LEGACY_ROOT"], !envRoot.isEmpty {
            let url = URL(fileURLWithPath: (envRoot as NSString).expandingTildeInPath)
            if Self.validate(url: url) {
                self.repoRoot = url
                return
            }
        }

        // dist/vphone-legacy.app lives two levels below the checkout it was built from.
        let beside = Bundle.main.bundleURL.deletingLastPathComponent().deletingLastPathComponent()
        if Self.validate(url: beside) {
            self.repoRoot = beside
            return
        }

        let cwd = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        if Self.validate(url: cwd) {
            self.repoRoot = cwd
            return
        }

        self.repoRoot = nil
    }

    public var isValidRepo: Bool {
        guard let repoRoot else { return false }
        return Self.validate(url: repoRoot)
    }

    public var cliURL: URL? {
        guard isValidRepo, let repoRoot else { return nil }
        return repoRoot.appendingPathComponent("vphone-legacy")
    }

    public var vmsDirectory: URL {
        if let configured = ProcessInfo.processInfo.environment["VPHONE_LEGACY_VMS_DIR"], !configured.isEmpty {
            return URL(fileURLWithPath: (configured as NSString).expandingTildeInPath, isDirectory: true)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".vphone-legacy/VMs", isDirectory: true)
    }

    public static func validate(url: URL) -> Bool {
        let fm = FileManager.default
        let cliPath = url.appendingPathComponent("vphone-legacy").path
        let scriptsPath = url.appendingPathComponent("scripts").path
        let sourcesPath = url.appendingPathComponent("sources").path

        guard fm.fileExists(atPath: cliPath) else { return false }

        var isDir: ObjCBool = false
        guard fm.fileExists(atPath: scriptsPath, isDirectory: &isDir), isDir.boolValue else { return false }

        isDir = false
        guard fm.fileExists(atPath: sourcesPath, isDirectory: &isDir), isDir.boolValue else { return false }

        return true
    }

    public func setRepoRoot(url: URL) -> Bool {
        if Self.validate(url: url) {
            self.repoRoot = url
            return true
        }
        return false
    }

    @MainActor
    public func chooseDirectory() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.canCreateDirectories = false
        panel.prompt = "Select Repository"
        panel.message = "Choose the root folder of the vphone-legacy checkout."

        if panel.runModal() == .OK, let selectedURL = panel.url {
            _ = setRepoRoot(url: selectedURL)
        }
    }
}
