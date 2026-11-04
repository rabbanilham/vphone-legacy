import SwiftUI

public struct VMCardView: View {
    @Bindable public var appState: AppState
    public let vm: VMInfo

    @State private var isTargetedForDrop = false
    @State private var showStopConfirmDialog = false
    @State private var pendingIpaPath: String? = nil
    @State private var isInstallingApp = false
    @State private var installProgressMessage = ""
    @State private var installFraction: Double? = nil
    @State private var showSettingsSheet = false

    public init(appState: AppState, vm: VMInfo) {
        self.appState = appState
        self.vm = vm
    }

    private var isSelected: Bool {
        appState.selectedVMName == vm.name
    }

    private var isRunning: Bool {
        vm.isRunning || appState.vmStatuses[vm.name]?.process?.phase == "running"
    }

    private var isOperating: Bool {
        appState.activeOperations[vm.name] != nil || isInstallingApp
    }

    private var activeOperationText: String? {
        appState.activeOperations[vm.name]
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            // Card Header
            HStack(alignment: .top) {
                // Device Icon
                ZStack {
                    RoundedRectangle(cornerRadius: 12)
                        .fill(LinearGradient(
                            colors: [Color.yellow.opacity(0.2), Color.orange.opacity(0.1)],
                            startPoint: .topLeading,
                            endPoint: .bottomTrailing
                        ))
                        .frame(width: 48, height: 48)
                    Image(systemName: "iphone.gen1")
                        .font(.system(size: 26))
                        .foregroundStyle(.primary)
                }

                VStack(alignment: .leading, spacing: 3) {
                    HStack {
                        Text(vm.name)
                            .font(.headline)
                        profileBadge
                        if vm.isLegacy { legacyBadge }
                    }
                    Text("iOS \(vm.ios) (\(vm.build)) • \(vm.device)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                Spacer()

                // Context Menu Button
                Menu {
                    contextMenuContent
                } label: {
                    Image(systemName: "ellipsis.circle")
                        .font(.title3)
                        .foregroundStyle(.secondary)
                }
                .menuStyle(.borderlessButton)
                .frame(width: 24, height: 24)
            }

            // Status & Specs Row
            HStack(spacing: 8) {
                phaseBadge
                bootStageBadge
                Spacer()
                Text(vm.formattedDiskSize)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                Text("•")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                Text("\(vm.apps.count) app\(vm.apps.count == 1 ? "" : "s")")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }

            Divider()

            // Large Start/Stop Button & Sideload Drop Zone
            HStack {
                if isOperating {
                    HStack(spacing: 8) {
                        ProgressView()
                            .controlSize(.small)
                        Text(activeOperationText ?? installProgressMessage)
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                    }
                } else {
                    if isRunning {
                        Button {
                            Task {
                                await appState.stopVM(name: vm.name, force: false)
                            }
                        } label: {
                            Label("Stop", systemImage: "stop.fill")
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.borderedProminent)
                        .tint(.red)
                        .controlSize(.large)
                    } else if vm.isLegacy {
                        VStack(alignment: .leading, spacing: 6) {
                            Text("This VM uses an unsupported legacy bundle format and cannot be started. Recreate it from the IPSW; its files stay untouched until you do.")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                            Button {
                                appState.recreateRequest = vm.name
                            } label: {
                                Label("Recreate…", systemImage: "arrow.triangle.2.circlepath")
                                    .frame(maxWidth: .infinity)
                            }
                            .buttonStyle(.borderedProminent)
                            .tint(.orange)
                            .controlSize(.large)
                        }
                    } else {
                        Button {
                            Task {
                                await appState.launchVM(name: vm.name)
                            }
                        } label: {
                            Label("Start", systemImage: "play.fill")
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.borderedProminent)
                        .tint(.green)
                        .controlSize(.large)
                    }
                }
            }

            // Sideload Drop hint
            HStack(spacing: 6) {
                Image(systemName: "arrow.down.doc")
                    .font(.caption)
                Text("Drop .ipa to install")
                    .font(.caption)
            }
            .foregroundStyle(isTargetedForDrop ? .blue : .secondary)
            .frame(maxWidth: .infinity, alignment: .center)
            .padding(.vertical, 4)
            .background(
                RoundedRectangle(cornerRadius: 6)
                    .strokeBorder(
                        isTargetedForDrop ? Color.blue : Color.secondary.opacity(0.2),
                        style: StrokeStyle(lineWidth: 1, dash: [4])
                    )
            )
        }
        .padding(16)
        .background(
            RoundedRectangle(cornerRadius: 14)
                .fill(Color(NSColor.controlBackgroundColor))
                .shadow(color: isSelected ? Color.accentColor.opacity(0.2) : Color.black.opacity(0.05), radius: 6, y: 2)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 14)
                .stroke(isSelected ? Color.accentColor : Color.secondary.opacity(0.15), lineWidth: isSelected ? 2 : 1)
        )
        .contentShape(Rectangle())
        .onTapGesture {
            appState.selectedVMName = vm.name
        }
        .contextMenu {
            contextMenuContent
        }
        .onDrop(of: [.fileURL], isTargeted: $isTargetedForDrop) { providers in
            handleDrop(providers: providers)
        }
        .confirmationDialog(
            "Hentikan VM untuk memasang?",
            isPresented: $showStopConfirmDialog,
            titleVisibility: .visible
        ) {
            Button("Hentikan & Pasang", role: .destructive) {
                if let path = pendingIpaPath {
                    pendingIpaPath = nil
                    performAppInstallWithReboot(ipaPath: path)
                }
            }
            Button("Batal", role: .cancel) {
                pendingIpaPath = nil
            }
        } message: {
            Text("Tidak ada shutdown bersih, tulisan terakhir bisa hilang. VM akan dimatikan paksa, aplikasi dipasang, lalu VM dijalankan ulang.")
        }
        .sheet(isPresented: $showSettingsSheet) {
            settingsSheetView
        }
    }

    // MARK: - Badges

    private var legacyBadge: some View {
        Text("Legacy")
            .font(.caption2)
            .bold()
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(Capsule().fill(Color.orange.opacity(0.18)))
            .foregroundStyle(.orange)
    }

    private var profileBadge: some View {
        Text(vm.isUiProfile ? "UI Profile" : "Baseline")
            .font(.caption2)
            .bold()
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(Capsule().fill(vm.isUiProfile ? Color.purple.opacity(0.15) : Color.blue.opacity(0.15)))
            .foregroundStyle(vm.isUiProfile ? .purple : .blue)
    }

    private var phaseBadge: some View {
        HStack(spacing: 4) {
            Circle()
                .fill(phaseColor)
                .frame(width: 8, height: 8)
            Text(vm.phase.capitalized)
                .font(.caption)
                .bold()
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 3)
        .background(Capsule().fill(phaseColor.opacity(0.12)))
        .foregroundStyle(phaseColor)
    }

    private var phaseColor: Color {
        switch vm.phase {
        case "running": return .green
        case "starting": return .orange
        case "stopping": return .orange
        case "failed": return .red
        default: return .secondary
        }
    }

    private var bootStageBadge: some View {
        let stage = appState.vmStatuses[vm.name]?.boot_stage ?? vm.boot_stage
        return Group {
            if stage != "none" {
                Text(stage.capitalized)
                    .font(.caption2)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(Capsule().fill(Color.secondary.opacity(0.15)))
                    .foregroundStyle(.secondary)
            }
        }
    }

    // MARK: - Context Menu

    @ViewBuilder
    private var contextMenuContent: some View {
        if isRunning {
            Button("Stop VM") {
                Task { await appState.stopVM(name: vm.name, force: false) }
            }
            Button("Force Stop VM") {
                Task { await appState.stopVM(name: vm.name, force: true) }
            }
        } else {
            Button("Start VM") {
                Task { await appState.launchVM(name: vm.name) }
            }
        }

        Divider()

        Button("Open Log Folder") {
            let logDir = URL(fileURLWithPath: vm.path).appendingPathComponent("logs")
            NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: logDir.path)
        }

        Button("Show in Finder") {
            NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: vm.path)
        }

        Button("Settings...") {
            showSettingsSheet = true
        }

        Divider()

        Button("Delete VM...", role: .destructive) {
            Task {
                await appState.deleteVM(name: vm.name)
            }
        }
    }

    // MARK: - Settings Sheet

    private var settingsSheetView: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Settings: \(vm.name)")
                .font(.headline)

            Toggle("Enable Host Wi-Fi Mirroring (--wifi)", isOn: Binding(
                get: { appState.vmWifiSettings[vm.name] ?? false },
                set: { appState.vmWifiSettings[vm.name] = $0 }
            ))
            .help("Enables modeled BCM43342 Wi-Fi dongle and slirp user networking.")

            Toggle("Relaxed AMFI / Unsigned Apps (--enable-unsigned)", isOn: Binding(
                get: { appState.vmAmfiSettings[vm.name] ?? false },
                set: { appState.vmAmfiSettings[vm.name] = $0 }
            ))
            .help("Appends relaxed-AMFI boot arguments to manifest for unsigned binaries.")

            Spacer()

            HStack {
                Spacer()
                Button("Done") {
                    showSettingsSheet = false
                }
                .buttonStyle(.borderedProminent)
            }
        }
        .padding()
        .frame(width: 420, height: 220)
    }

    // MARK: - App Sideloading

    private func handleDrop(providers: [NSItemProvider]) -> Bool {
        guard let provider = providers.first else { return false }
        _ = provider.loadObject(ofClass: URL.self) { url, _ in
            guard let url, url.pathExtension.lowercased() == "ipa" else { return }
            DispatchQueue.main.async {
                if isRunning {
                    self.pendingIpaPath = url.path
                    self.showStopConfirmDialog = true
                } else {
                    performAppInstall(ipaPath: url.path)
                }
            }
        }
        return true
    }

    private func performAppInstall(ipaPath: String) {
        isInstallingApp = true
        installProgressMessage = "Installing IPA..."
        let unsigned = appState.vmAmfiSettings[vm.name] ?? false

        Task {
            let stream = appState.client.appInstall(name: vm.name, ipaPath: ipaPath, enableUnsigned: unsigned)
            for await event in stream {
                if let msg = event.message {
                    installProgressMessage = msg
                }
                if let fraction = event.fraction {
                    installFraction = fraction
                }
            }
            isInstallingApp = false
            await appState.refreshVMs()
        }
    }

    private func performAppInstallWithReboot(ipaPath: String) {
        isInstallingApp = true
        installProgressMessage = "Stopping VM for install..."
        let unsigned = appState.vmAmfiSettings[vm.name] ?? false

        Task {
            await appState.stopVM(name: vm.name, force: true)

            // Install IPA
            installProgressMessage = "Installing IPA..."
            let stream = appState.client.appInstall(name: vm.name, ipaPath: ipaPath, enableUnsigned: unsigned)
            for await event in stream {
                if let msg = event.message {
                    installProgressMessage = msg
                }
            }

            // Restart VM
            installProgressMessage = "Restarting VM..."
            await appState.launchVM(name: vm.name)
            isInstallingApp = false
            await appState.refreshVMs()
        }
    }
}

#Preview {
    let vm = VMInfo(
        name: "ios712",
        path: "/tmp/ios712",
        ios: "7.1.2",
        build: "11D257",
        device: "iPhone6,2",
        profile: "ui",
        memory_mb: 1024,
        disk_bytes: 7300000000,
        phase: "stopped",
        boot_stage: "none"
    )
    VMCardView(appState: AppState(client: FixtureCLIClient()), vm: vm)
        .frame(width: 320)
        .padding()
}
