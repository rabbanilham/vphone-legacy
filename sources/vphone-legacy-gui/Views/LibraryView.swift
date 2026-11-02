import SwiftUI

public struct LibraryView: View {
    @Bindable public var appState: AppState

    @State private var showNewVMSheet = false
    @State private var showSetupSheet = false
    @State private var showInspector = true

    public init(appState: AppState) {
        self.appState = appState
    }

    public var body: some View {
        NavigationSplitView {
            // Left sidebar: VM list
            VStack(spacing: 0) {
                List(selection: $appState.selectedVMName) {
                    Section("Virtual Machines") {
                        ForEach(appState.vms) { vm in
                            HStack {
                                Image(systemName: "iphone.gen1")
                                    .foregroundStyle(vm.isRunning ? .green : .secondary)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(vm.name)
                                        .font(.body)
                                        .bold()
                                    Text("iOS \(vm.ios) • \(vm.profile)")
                                        .font(.caption2)
                                        .foregroundStyle(.secondary)
                                }
                                Spacer()
                                Circle()
                                    .fill(statusColor(for: vm.phase))
                                    .frame(width: 8, height: 8)
                            }
                            .tag(vm.name)
                        }
                    }
                }
                .listStyle(.sidebar)

                Divider()

                // Bottom toolbar inside sidebar
                HStack {
                    Button {
                        showNewVMSheet = true
                    } label: {
                        Label("New VM", systemImage: "plus")
                    }
                    .buttonStyle(.borderless)

                    Spacer()

                    Button {
                        showSetupSheet = true
                    } label: {
                        HStack(spacing: 4) {
                            Image(systemName: appState.isSetupValid ? "checkmark.seal" : "exclamationmark.triangle")
                                .foregroundStyle(appState.isSetupValid ? .green : .orange)
                            Text("Setup")
                        }
                    }
                    .buttonStyle(.borderless)

                    Button {
                        Task {
                            await appState.refreshVMs()
                        }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .buttonStyle(.borderless)
                    .help("Refresh VM list")
                }
                .padding(10)
                .background(Color(NSColor.controlBackgroundColor))
            }
            .frame(minWidth: 220)
        } detail: {
            // Main content
            if appState.vms.isEmpty {
                emptyLibraryView
            } else if let selected = appState.selectedVM {
                HSplitView {
                    // Card Area
                    ScrollView {
                        VStack(alignment: .leading, spacing: 16) {
                            Text("Virtual Machine")
                                .font(.title3)
                                .bold()
                            VMCardView(appState: appState, vm: selected)
                                .frame(maxWidth: 480)
                            Spacer()
                        }
                        .padding()
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .frame(minWidth: 340)

                    // Inspector / Detail Panel
                    VMDetailView(appState: appState, vm: selected)
                        .frame(minWidth: 320)
                }
            } else {
                Text("Select a virtual machine to view details.")
                    .font(.title3)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button {
                    showNewVMSheet = true
                } label: {
                    Label("New VM", systemImage: "plus")
                }
            }
            ToolbarItem(placement: .automatic) {
                Button {
                    showSetupSheet = true
                } label: {
                    Label("Setup", systemImage: "gearshape")
                }
            }
        }
        .onChange(of: appState.recreateRequest) { _, name in
            if name != nil { showNewVMSheet = true }
        }
        .sheet(isPresented: $showNewVMSheet) {
            NewVMWizardView(appState: appState, isPresented: $showNewVMSheet)
        }
        .sheet(isPresented: $showSetupSheet) {
            SetupWizardView(appState: appState)
                .toolbar {
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Done") {
                            showSetupSheet = false
                        }
                    }
                }
        }
    }

    private func statusColor(for phase: String) -> Color {
        switch phase {
        case "running": return .green
        case "starting", "stopping": return .orange
        case "failed": return .red
        default: return .secondary.opacity(0.5)
        }
    }

    private var emptyLibraryView: some View {
        VStack(spacing: 16) {
            Image(systemName: "iphone.gen1")
                .font(.system(size: 64))
                .foregroundStyle(.secondary)
            Text("No Virtual Machines")
                .font(.title2)
                .bold()
            Text("Create an iPhone 5s virtual machine from an iOS 7.1.2 (11D257) IPSW.")
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            Button {
                showNewVMSheet = true
            } label: {
                Label("Create New VM", systemImage: "plus")
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
        }
        .padding()
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

#Preview {
    LibraryView(appState: AppState(client: FixtureCLIClient()))
}
