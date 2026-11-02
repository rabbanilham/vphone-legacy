import SwiftUI
import AppKit

public struct NewVMWizardView: View {
    @Bindable public var appState: AppState
    @Binding public var isPresented: Bool

    @State private var vmName: String = "ios712"
    @State private var ipswPath: String = ""
    @State private var importPath: String = ""
    @State private var isImportMode = false

    @State private var isRunning = false
    @State private var currentStageId: String? = nil
    @State private var currentMessage: String = ""
    @State private var currentFraction: Double? = nil
    @State private var errorHint: String? = nil
    @State private var errorMessage: String? = nil
    @State private var hasFailed = false
    @State private var isFinished = false
    @State private var retryWithForce = false
    @State private var logLines: [String] = []

    @State private var stageHistory: [String: (status: String, message: String)] = [:]
    @State private var activeTask: Task<Void, Never>? = nil

    public init(appState: AppState, isPresented: Binding<Bool>) {
        self.appState = appState
        self._isPresented = isPresented
    }

    public var body: some View {
        content
            .onAppear {
                // Recreate: same name, replacing the unsupported bundle (vm create --force).
                if let name = appState.recreateRequest {
                    vmName = name
                    retryWithForce = true
                    appState.recreateRequest = nil
                }
            }
    }

    private var content: some View {
        VStack(spacing: 0) {
            // Header
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(isImportMode ? "Import Existing VM Profile" : "New iPhone 5s Virtual Machine")
                        .font(.title2)
                        .bold()
                    Text("Automated restore from iOS 7.1.2 (11D257) IPSW to UI profile")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button {
                    cancelAndDismiss()
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .font(.title2)
                        .foregroundStyle(.secondary)
                }
                .buttonStyle(.plain)
            }
            .padding()
            .background(Color(NSColor.controlBackgroundColor))

            Divider()

            if isRunning || hasFailed || isFinished {
                progressViewContent
            } else {
                configurationForm
            }
        }
        .frame(width: 620, height: 500)
    }

    // MARK: - Configuration Form

    private var configurationForm: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                // Name
                VStack(alignment: .leading, spacing: 6) {
                    Text("Virtual Machine Name")
                        .font(.headline)
                    TextField("Name (e.g. ios712)", text: $vmName)
                        .textFieldStyle(.roundedBorder)
                }

                if isImportMode {
                    importModeSection
                } else {
                    ipswModeSection
                }

                Divider()

                // Mode switch & Action buttons
                HStack {
                    Button(isImportMode ? "Switch to IPSW Creation" : "Import existing profile...") {
                        isImportMode.toggle()
                    }
                    .buttonStyle(.link)

                    Spacer()

                    Button("Cancel") {
                        isPresented = false
                    }

                    Button(isImportMode ? "Import Profile" : "Create & Prepare UI") {
                        startCreation()
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(isCreateDisabled)
                }
            }
            .padding()
        }
    }

    private var ipswModeSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("iOS 7.1.2 IPSW File")
                .font(.headline)

            // Drop zone
            VStack(spacing: 12) {
                Image(systemName: "arrow.down.doc.fill")
                    .font(.system(size: 36))
                    .foregroundStyle(Color.accentColor)

                if ipswPath.isEmpty {
                    Text("Drop iPhone6,2_7.1.2_11D257_Restore.ipsw here")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                } else {
                    Text(URL(fileURLWithPath: ipswPath).lastPathComponent)
                        .font(.subheadline)
                        .bold()
                    Text(ipswPath)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }

                Button("Choose IPSW File...") {
                    chooseIPSWFile()
                }
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 24)
            .background(RoundedRectangle(cornerRadius: 10).fill(Color(NSColor.controlBackgroundColor)))
            .overlay(
                RoundedRectangle(cornerRadius: 10)
                    .strokeBorder(Color.secondary.opacity(0.3), style: StrokeStyle(lineWidth: 1, dash: [5]))
            )
            .onDrop(of: [.fileURL], isTargeted: nil) { providers in
                guard let provider = providers.first else { return false }
                _ = provider.loadObject(ofClass: URL.self) { url, _ in
                    guard let url, url.pathExtension.lowercased() == "ipsw" else { return }
                    DispatchQueue.main.async {
                        self.ipswPath = url.path
                        if vmName.isEmpty || vmName == "ios712" {
                            let filename = url.deletingPathExtension().lastPathComponent
                            if filename.contains("iPhone") {
                                self.vmName = "ios712"
                            }
                        }
                    }
                }
                return true
            }
        }
    }

    private var importModeSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Profile Directory")
                .font(.headline)
            Text("Select an existing profile folder (e.g. build/virtual-activated-profile-v5).")
                .font(.caption)
                .foregroundStyle(.secondary)

            HStack {
                TextField("Path to profile folder", text: $importPath)
                    .textFieldStyle(.roundedBorder)
                Button("Browse...") {
                    chooseImportDirectory()
                }
            }
        }
    }

    private var isCreateDisabled: Bool {
        if vmName.trimmingCharacters(in: .whitespaces).isEmpty { return true }
        if isImportMode {
            return importPath.trimmingCharacters(in: .whitespaces).isEmpty
        } else {
            return ipswPath.trimmingCharacters(in: .whitespaces).isEmpty
        }
    }

    // MARK: - Progress View

    private var progressViewContent: some View {
        VStack(alignment: .leading, spacing: 16) {
            // Stage Status
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text(isFinished ? "Virtual Machine Ready!" : (hasFailed ? "Preparation Failed" : "Building Virtual Machine..."))
                        .font(.headline)
                        .foregroundStyle(isFinished ? .green : (hasFailed ? .red : .primary))
                    Spacer()
                    if isRunning {
                        ProgressView()
                            .controlSize(.small)
                    }
                }

                if let fraction = currentFraction, isRunning {
                    ProgressView(value: fraction)
                } else if isRunning {
                    ProgressView()
                }

                if !currentMessage.isEmpty {
                    Text(currentMessage)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
            }
            .padding()
            .background(RoundedRectangle(cornerRadius: 8).fill(Color(NSColor.controlBackgroundColor)))

            // Error & Hint Card
            if hasFailed {
                VStack(alignment: .leading, spacing: 8) {
                    if let error = errorMessage {
                        HStack(alignment: .top, spacing: 8) {
                            Image(systemName: "exclamationmark.triangle.fill")
                                .foregroundStyle(.red)
                            Text(error)
                                .font(.body)
                                .foregroundStyle(.red)
                        }
                    }

                    if let hint = errorHint {
                        HStack(alignment: .top, spacing: 8) {
                            Image(systemName: "lightbulb.fill")
                                .foregroundStyle(.yellow)
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Suggested Fix")
                                    .font(.caption)
                                    .bold()
                                Text(hint)
                                    .font(.caption)
                            }
                        }
                        .padding(8)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(RoundedRectangle(cornerRadius: 6).fill(Color.yellow.opacity(0.12)))
                    }
                }
                .padding()
                .background(RoundedRectangle(cornerRadius: 8).fill(Color.red.opacity(0.08)))
            }

            // Console output preview
            VStack(alignment: .leading, spacing: 4) {
                Text("Process Log")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                ScrollView {
                    VStack(alignment: .leading, spacing: 2) {
                        ForEach(Array(logLines.suffix(30).enumerated()), id: \.offset) { _, line in
                            Text(line)
                                .font(.system(.caption2, design: .monospaced))
                                .foregroundStyle(.secondary)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxHeight: 120)
                .padding(8)
                .background(Color(NSColor.textBackgroundColor))
                .clipShape(RoundedRectangle(cornerRadius: 6))
            }

            Spacer()

            // Footer actions
            HStack {
                if hasFailed {
                    Button("Retry (--force)") {
                        retryWithForce = true
                        startCreation()
                    }
                    .buttonStyle(.borderedProminent)

                    Button("Cancel") {
                        cancelAndDismiss()
                    }
                } else if isFinished {
                    Spacer()
                    Button("Done") {
                        isPresented = false
                    }
                    .buttonStyle(.borderedProminent)
                } else {
                    Button("Cancel", role: .destructive) {
                        cancelAndDismiss()
                    }
                }
            }
        }
        .padding()
    }

    // MARK: - Actions

    private func chooseIPSWFile() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.allowedContentTypes = []
        panel.prompt = "Select IPSW"
        if panel.runModal() == .OK, let url = panel.url {
            self.ipswPath = url.path
        }
    }

    private func chooseImportDirectory() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = "Select Profile Folder"
        if panel.runModal() == .OK, let url = panel.url {
            self.importPath = url.path
        }
    }

    private func startCreation() {
        isRunning = true
        hasFailed = false
        isFinished = false
        errorMessage = nil
        errorHint = nil
        currentMessage = "Starting..."
        currentFraction = nil
        logLines.removeAll()

        let name = vmName.trimmingCharacters(in: .whitespaces)
        let force = retryWithForce

        activeTask = Task {
            if isImportMode {
                await runImport(name: name)
            } else {
                await runCreateAndPrepare(name: name, force: force)
            }
        }
    }

    private func runImport(name: String) async {
        let stream = appState.client.vmImportProfile(name: name, fromPath: importPath)
        for await event in stream {
            if Task.isCancelled { break }
            handleProgressEvent(event)
        }
        if !hasFailed {
            isFinished = true
            isRunning = false
            await appState.refreshVMs()
        }
    }

    private func runCreateAndPrepare(name: String, force: Bool) async {
        // Step 1: vm create
        currentMessage = "Step 1/2: Extracting & creating baseline bundle..."
        let createStream = appState.client.vmCreate(name: name, ios: "7.1.2", ipswPath: ipswPath, force: force)
        for await event in createStream {
            if Task.isCancelled { return }
            handleProgressEvent(event)
            if hasFailed { break }
        }

        if hasFailed {
            isRunning = false
            return
        }

        // Step 2: vm prepare-ui
        currentMessage = "Step 2/2: Preparing UI profile & activation..."
        let prepareStream = appState.client.vmPrepareUI(name: name, force: force, ipswPath: ipswPath)
        for await event in prepareStream {
            if Task.isCancelled { return }
            handleProgressEvent(event)
            if hasFailed { break }
        }

        if !hasFailed {
            isFinished = true
            isRunning = false
            currentMessage = "Successfully created and prepared \(name)!"
            await appState.refreshVMs()
            appState.selectedVMName = name
        } else {
            isRunning = false
        }
    }

    private func handleProgressEvent(_ event: ProgressEvent) {
        if let msg = event.message {
            currentMessage = msg
            logLines.append(msg)
        }
        if let frac = event.fraction {
            currentFraction = frac
        }
        if let id = event.id {
            currentStageId = id
        }
        if let hint = event.hint {
            errorHint = hint
        }

        if event.isResult {
            if event.ok == false {
                hasFailed = true
                if let data = event.data, let errVal = data["error"]?.stringValue {
                    errorMessage = errVal
                } else {
                    errorMessage = event.message ?? "Operation failed."
                }
            }
        } else if event.isError {
            hasFailed = true
            errorMessage = event.message ?? "An error occurred."
            if let hint = event.hint {
                errorHint = hint
            }
        }
    }

    private func cancelAndDismiss() {
        activeTask?.cancel()
        activeTask = nil
        isPresented = false
    }
}

#Preview {
    NewVMWizardView(
        appState: AppState(client: FixtureCLIClient()),
        isPresented: .constant(true)
    )
}
