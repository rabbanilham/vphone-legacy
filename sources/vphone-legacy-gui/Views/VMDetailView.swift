import SwiftUI

public struct VMDetailView: View {
    @Bindable public var appState: AppState
    public let vm: VMInfo

    @State private var selectedTab: Int = 0

    public init(appState: AppState, vm: VMInfo) {
        self.appState = appState
        self.vm = vm
    }

    private var status: VMStatus? {
        appState.vmStatuses[vm.name]
    }

    public var body: some View {
        VStack(spacing: 0) {
            // Header
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(vm.name)
                        .font(.title2)
                        .bold()
                    Text("iPhone 5s (iPhone6,2) • iOS \(vm.ios) (11D257)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()

                Picker("View", selection: $selectedTab) {
                    Text("Info").tag(0)
                    Text("Logs").tag(1)
                }
                .pickerStyle(.segmented)
                .frame(width: 160)
            }
            .padding()
            .background(Color(NSColor.controlBackgroundColor))

            Divider()

            // Content
            if selectedTab == 0 {
                infoTabContent
            } else {
                logsTabContent
            }
        }
        .task {
            await appState.tailLog(for: vm.name)
        }
    }

    // MARK: - Info Tab

    private var infoTabContent: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                // Status Box
                VStack(alignment: .leading, spacing: 10) {
                    Text("Runtime Status")
                        .font(.headline)
                    LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 12) {
                        infoField(label: "Phase", value: vm.phase.capitalized)
                        infoField(label: "Boot Stage", value: status?.boot_stage ?? vm.boot_stage)
                        infoField(label: "PID", value: status?.process?.pid.map(String.init) ?? (vm.pid.map(String.init) ?? "None"))
                        infoField(label: "Lock", value: status?.lock ?? "free")
                    }
                }
                .padding()
                .background(RoundedRectangle(cornerRadius: 8).fill(Color(NSColor.controlBackgroundColor)))

                // VM Properties
                VStack(alignment: .leading, spacing: 10) {
                    Text("Hardware & System")
                        .font(.headline)
                    LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 12) {
                        infoField(label: "Device", value: vm.device)
                        infoField(label: "Profile", value: vm.profile)
                        infoField(label: "Memory", value: "\(vm.memoryMB) MB")
                        infoField(label: "Disk Size", value: vm.formattedDiskSize)
                        infoField(label: "Bundle Path", value: vm.path)
                        infoField(label: "Session ID", value: status?.session_id ?? "—")
                    }
                }
                .padding()
                .background(RoundedRectangle(cornerRadius: 8).fill(Color(NSColor.controlBackgroundColor)))

                // Installed Applications
                VStack(alignment: .leading, spacing: 10) {
                    Text("Installed Applications (\(vm.apps.count))")
                        .font(.headline)

                    if vm.apps.isEmpty {
                        Text("No third-party apps installed. Drag and drop an .ipa file onto the VM card in Library to sideload.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    } else {
                        VStack(spacing: 6) {
                            ForEach(vm.apps, id: \.bundle_id) { app in
                                HStack {
                                    Image(systemName: "app.fill")
                                        .font(.title3)
                                        .foregroundStyle(.blue)
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(app.bundle_id)
                                            .font(.subheadline)
                                            .bold()
                                        Text(app.path)
                                            .font(.caption2)
                                            .foregroundStyle(.secondary)
                                    }
                                    Spacer()
                                }
                                .padding(8)
                                .background(RoundedRectangle(cornerRadius: 6).fill(Color(NSColor.textBackgroundColor)))
                            }
                        }
                    }
                }
                .padding()
                .background(RoundedRectangle(cornerRadius: 8).fill(Color(NSColor.controlBackgroundColor)))
            }
            .padding()
        }
    }

    private func infoField(label: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.subheadline)
                .bold()
                .textSelection(.enabled)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: - Logs Tab

    private var logsTabContent: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Serial / Console Log")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                Button {
                    Task {
                        await appState.tailLog(for: vm.name)
                    }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .controlSize(.small)
            }
            .padding(.horizontal)
            .padding(.vertical, 8)
            .background(Color(NSColor.controlBackgroundColor))

            Divider()

            let logText = appState.vmLogs[vm.name] ?? "No serial output observed yet."

            ScrollView {
                Text(logText)
                    .font(.system(size: 11, design: .monospaced))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding()
                    .textSelection(.enabled)
            }
            .background(Color(NSColor.textBackgroundColor))
        }
    }
}

#Preview {
    let vm = VMInfo(
        name: "ios712",
        path: "/Users/me/.vphone-legacy/VMs/ios712",
        ios: "7.1.2",
        build: "11D257",
        device: "iPhone6,2",
        profile: "ui",
        memory_mb: 1024,
        disk_bytes: 7300000000,
        phase: "stopped",
        boot_stage: "none",
        apps: [VMAppInfo(bundle_id: "com.vphone.hello", path: "/Applications/Hello.app")]
    )
    VMDetailView(appState: AppState(client: FixtureCLIClient()), vm: vm)
}
