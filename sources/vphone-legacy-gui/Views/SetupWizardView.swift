import SwiftUI

public struct SetupWizardView: View {
    @Bindable public var appState: AppState

    @State private var isFixingAll = false
    @State private var fixingItemId: String? = nil
    @State private var currentStageMessage = ""
    @State private var currentStageHint: String? = nil
    @State private var currentFraction: Double? = nil
    @State private var fixLogLines: [String] = []
    @State private var activeFixTask: Task<Void, Never>? = nil

    public init(appState: AppState) {
        self.appState = appState
    }

    public var body: some View {
        VStack(spacing: 0) {
            // Header
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Environment Setup")
                        .font(.title2)
                        .bold()
                    Text("vphone-legacy requires host prerequisites and compiled binaries.")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button {
                    Task {
                        await appState.checkSetup()
                    }
                } label: {
                    Label("Check Again", systemImage: "arrow.clockwise")
                }
                .disabled(appState.isCheckingSetup || isFixingAll)

                if hasFixableItems {
                    Button {
                        runFixAll()
                    } label: {
                        Label("Fix All", systemImage: "wrench.and.screwdriver")
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(isFixingAll)
                }
            }
            .padding()
            .background(Color(NSColor.controlBackgroundColor))

            Divider()

            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    // Repository Locator Section
                    repoLocatorSection

                    // Prerequisites Section
                    if appState.repoLocator.isValidRepo {
                        prerequisitesSection
                    }

                    // Progress / Log stream section if active
                    if isFixingAll || fixingItemId != nil {
                        activeFixSection
                    }

                    // Manual Instructions section
                    manualInstructionsSection
                }
                .padding()
            }
        }
        .frame(minWidth: 700, minHeight: 520)
        .task {
            if appState.setupCheck == nil && appState.repoLocator.isValidRepo {
                await appState.checkSetup()
            }
        }
    }

    // MARK: - Sections

    private var repoLocatorSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Image(systemName: appState.repoLocator.isValidRepo ? "folder.badge.gearshape" : "folder.badge.questionmark")
                    .font(.title2)
                    .foregroundStyle(appState.repoLocator.isValidRepo ? .green : .orange)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Repository Location")
                        .font(.headline)
                    Text(appState.repoLocator.repoRoot?.path ?? "No repository selected")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button("Locate Repository...") {
                    appState.repoLocator.chooseDirectory()
                    appState.updateClient()
                    Task {
                        await appState.checkSetup()
                        await appState.refreshVMs()
                    }
                }
            }
            .padding()
            .background(RoundedRectangle(cornerRadius: 8).fill(Color(NSColor.controlBackgroundColor)))
            .overlay(
                RoundedRectangle(cornerRadius: 8)
                    .stroke(appState.repoLocator.isValidRepo ? Color.green.opacity(0.3) : Color.orange.opacity(0.5), lineWidth: 1)
            )
            .onDrop(of: [.fileURL], isTargeted: nil) { providers in
                guard let provider = providers.first else { return false }
                _ = provider.loadObject(ofClass: URL.self) { url, _ in
                    guard let url else { return }
                    DispatchQueue.main.async {
                        if appState.repoLocator.setRepoRoot(url: url) {
                            appState.updateClient()
                            Task {
                                await appState.checkSetup()
                                await appState.refreshVMs()
                            }
                        }
                    }
                }
                return true
            }
        }
    }

    private var prerequisitesSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Prerequisites")
                .font(.headline)

            if appState.isCheckingSetup {
                HStack(spacing: 12) {
                    ProgressView()
                        .controlSize(.small)
                    Text("Checking system status...")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
                .padding()
            } else if let items = appState.setupCheck?.items {
                VStack(spacing: 8) {
                    ForEach(items) { item in
                        setupItemRow(item)
                    }
                }
            }
        }
    }

    private func setupItemRow(_ item: SetupItem) -> some View {
        HStack(spacing: 12) {
            statusIcon(for: item.status)

            VStack(alignment: .leading, spacing: 2) {
                HStack {
                    Text(item.title)
                        .font(.body)
                        .bold()
                    if item.id == "wifi-helper" {
                        Text("Optional")
                            .font(.caption2)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2)
                            .background(Capsule().fill(Color.secondary.opacity(0.2)))
                    }
                }
                if let detail = item.detail, !detail.isEmpty {
                    Text(detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            Spacer()

            if item.status == "fixable" || (item.status == "missing" && item.fix?.kind == "auto") {
                Button {
                    runFix(itemId: item.id)
                } label: {
                    Text("Install")
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .disabled(isFixingAll || fixingItemId != nil)
            } else if item.status != "ok" && item.fix?.instructions != nil {
                Text("Manual fix required")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color(NSColor.controlBackgroundColor)))
    }

    private func statusIcon(for status: String) -> some View {
        Group {
            switch status {
            case "ok":
                Image(systemName: "checkmark.circle.fill")
                    .foregroundStyle(.green)
            case "fixable":
                Image(systemName: "gearshape.circle.fill")
                    .foregroundStyle(.orange)
            default:
                Image(systemName: "xmark.circle.fill")
                    .foregroundStyle(.red)
            }
        }
        .font(.title3)
    }

    private var activeFixSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Installation Progress")
                    .font(.headline)
                Spacer()
                Button("Cancel", role: .destructive) {
                    cancelFix()
                }
                .controlSize(.small)
            }

            if let fraction = currentFraction {
                ProgressView(value: fraction)
            } else {
                ProgressView()
            }

            if !currentStageMessage.isEmpty {
                Text(currentStageMessage)
                    .font(.subheadline)
            }

            if let hint = currentStageHint {
                HStack {
                    Image(systemName: "lightbulb.fill")
                        .foregroundStyle(.yellow)
                    Text(hint)
                        .font(.caption)
                }
                .padding(8)
                .background(RoundedRectangle(cornerRadius: 6).fill(Color.yellow.opacity(0.1)))
            }

            if !fixLogLines.isEmpty {
                ScrollView {
                    VStack(alignment: .leading, spacing: 2) {
                        ForEach(Array(fixLogLines.suffix(20).enumerated()), id: \.offset) { _, line in
                            Text(line)
                                .font(.system(.caption, design: .monospaced))
                                .foregroundStyle(.secondary)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(height: 100)
                .padding(8)
                .background(Color(NSColor.textBackgroundColor))
                .clipShape(RoundedRectangle(cornerRadius: 6))
            }
        }
        .padding()
        .background(RoundedRectangle(cornerRadius: 8).fill(Color(NSColor.controlBackgroundColor)))
    }

    private var manualInstructionsSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Manual Setup Commands")
                .font(.headline)

            VStack(alignment: .leading, spacing: 8) {
                manualCommandRow(
                    title: "Homebrew",
                    command: "/bin/bash -c \"$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
                )
                manualCommandRow(
                    title: "Xcode Command Line Tools",
                    command: "xcode-select --install"
                )
                manualCommandRow(
                    title: "Required Homebrew Packages",
                    command: "brew install python@3.13 keystone glib pixman openssl@3 qemu libslirp pcre2"
                )
            }
        }
    }

    private func manualCommandRow(title: String, command: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(.caption)
                .bold()
                .foregroundStyle(.secondary)
            HStack {
                Text(command)
                    .font(.system(.caption, design: .monospaced))
                    .textSelection(.enabled)
                Spacer()
                Button {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(command, forType: .string)
                } label: {
                    Image(systemName: "doc.on.doc")
                }
                .buttonStyle(.borderless)
                .help("Copy command")
            }
            .padding(8)
            .background(RoundedRectangle(cornerRadius: 4).fill(Color(NSColor.textBackgroundColor)))
        }
    }

    // MARK: - Actions

    private var hasFixableItems: Bool {
        guard let items = appState.setupCheck?.items else { return false }
        return items.contains { $0.status != "ok" && $0.fix?.kind == "auto" }
    }

    private func runFix(itemId: String) {
        fixingItemId = itemId
        currentStageMessage = "Starting installation..."
        currentStageHint = nil
        currentFraction = nil
        fixLogLines.removeAll()

        activeFixTask = Task {
            let stream = appState.client.setupInstall(id: itemId)
            for await event in stream {
                if Task.isCancelled { break }
                if let msg = event.message {
                    currentStageMessage = msg
                    fixLogLines.append(msg)
                }
                if let fraction = event.fraction {
                    currentFraction = fraction
                }
                if let hint = event.hint {
                    currentStageHint = hint
                }
            }
            fixingItemId = nil
            await appState.checkSetup()
        }
    }

    private func runFixAll() {
        guard let items = appState.setupCheck?.items else { return }
        let autoItems = items.filter { $0.status != "ok" && $0.fix?.kind == "auto" }
        guard !autoItems.isEmpty else { return }

        isFixingAll = true
        currentStageMessage = "Fixing all prerequisites..."
        currentStageHint = nil
        currentFraction = nil
        fixLogLines.removeAll()

        activeFixTask = Task {
            for item in autoItems {
                if Task.isCancelled { break }
                fixingItemId = item.id
                currentStageMessage = "Installing \(item.title)..."
                let stream = appState.client.setupInstall(id: item.id)
                for await event in stream {
                    if Task.isCancelled { break }
                    if let msg = event.message {
                        currentStageMessage = msg
                        fixLogLines.append(msg)
                    }
                    if let fraction = event.fraction {
                        currentFraction = fraction
                    }
                    if let hint = event.hint {
                        currentStageHint = hint
                    }
                }
            }
            isFixingAll = false
            fixingItemId = nil
            await appState.checkSetup()
        }
    }

    private func cancelFix() {
        activeFixTask?.cancel()
        activeFixTask = nil
        isFixingAll = false
        fixingItemId = nil
        currentStageMessage = "Cancelled"
    }
}

#Preview {
    SetupWizardView(appState: AppState(client: FixtureCLIClient()))
}
