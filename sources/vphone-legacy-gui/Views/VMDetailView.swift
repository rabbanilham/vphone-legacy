import SwiftUI

public struct VMDetailView: View {
    @ObservedObject var vmManager: VMManager
    let vm: VirtualMachineInfo
    @Binding var selectedTab: Int
    
    public init(vmManager: VMManager, vm: VirtualMachineInfo, selectedTab: Binding<Int>) {
        self.vmManager = vmManager
        self.vm = vm
        self._selectedTab = selectedTab
    }
    
    public var body: some View {
        VStack(spacing: 0) {
            // Top Toolbar Bar
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 8) {
                        Text(vm.name)
                            .font(.title2)
                            .fontWeight(.bold)
                        Text("iOS \(vm.version)")
                            .font(.caption)
                            .fontWeight(.semibold)
                            .padding(.horizontal, 8)
                            .padding(.vertical, 3)
                            .background(Color.blue.opacity(0.15))
                            .foregroundColor(.blue)
                            .cornerRadius(6)
                    }
                    Text("Guest firmware: \(vm.device) • Required \(vm.chip) / \(vm.cpu)")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
                
                Spacer()
                
                // Hardware controls are disabled until events can reach the guest.
                HStack(spacing: 6) {
                    Button("Home", systemImage: "circle.circle") {}.disabled(true)
                    Button("Power", systemImage: "power") {}.disabled(true)
                }
                .help("Unavailable: backend guest input is not implemented")

                // Start / Stop Button
                if vmManager.isRunning && vmManager.activeVMName == vm.name {
                    Button(action: {
                        vmManager.stopVM()
                    }) {
                        Label(vmManager.isStopping ? "Requesting Stop…" : "Shut Down", systemImage: "stop.fill")
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(.red)
                    .disabled(vmManager.isStopping)
                    Button("Force Stop") { vmManager.stopVM(force: true) }
                        .disabled(vmManager.isStopping)
                        .help("Terminate the backend without a clean guest shutdown")
                } else {
                    Button(action: {
                        vmManager.launchVM(vm: vm)
                    }) {
                        Label("Start VM", systemImage: "play.fill")
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(.green)
                    .disabled(vmManager.isRunning)
                }
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 14)
            .background(Color(NSColor.windowBackgroundColor))
            
            Divider()
            
            // Tab Picker
            Picker("", selection: $selectedTab) {
                Text("Runtime Status & Specs").tag(0)
                Text("Serial Console").tag(1)
                Text("Guest Agent (vphoned)").tag(2)
                Text("Firmware Info").tag(3)
            }
            .pickerStyle(SegmentedPickerStyle())
            .padding(.horizontal, 20)
            .padding(.vertical, 10)
            
            Divider()
            
            // Tab Content
            ScrollView {
                if selectedTab == 0 {
                    overviewTab
                } else if selectedTab == 1 {
                    ConsoleView(vmManager: vmManager)
                } else if selectedTab == 2 {
                    AgentView(vmManager: vmManager)
                } else {
                    FirmwareCatalogView(vm: vm)
                }
            }
        }
    }
    
    private var overviewTab: some View {
        HStack(alignment: .top, spacing: 30) {
            VMStatusView(vmManager: vmManager, vm: vm)
                .padding(.leading, 20)
            
            // Specification & Configuration Cards
            VStack(alignment: .leading, spacing: 20) {
                // Specs Card
                VStack(alignment: .leading, spacing: 12) {
                    Text("Machine Configuration")
                        .font(.headline)
                    
                    Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 8) {
                        GridRow {
                            Text("Device Identity:").foregroundColor(.secondary)
                            Text("\(vm.device) (\(vm.board))")
                                .fontWeight(.medium)
                        }
                        GridRow {
                            Text("Processor / SoC:").foregroundColor(.secondary)
                            Text("Apple A7 (\(vm.chip)) ARM64")
                                .fontWeight(.medium)
                        }
                        GridRow {
                            Text("Guest CPU Requirement:").foregroundColor(.secondary)
                            Text(vm.cpu)
                                .font(.system(.body, design: .monospaced))
                        }
                        GridRow {
                            Text("Backend CPU (experimental):").foregroundColor(.secondary)
                            Text(vmManager.backendCPUDescription)
                                .font(.system(.caption, design: .monospaced))
                        }
                        GridRow {
                            Text("Configured Memory:").foregroundColor(.secondary)
                            Text("\(vm.memoryMB) MB DRAM")
                        }
                        GridRow {
                            Text("Filesystem Type:").foregroundColor(.secondary)
                            Text("See validated bundle manifest")
                        }
                    }
                }
                .padding(16)
                .background(Color(NSColor.controlBackgroundColor))
                .cornerRadius(10)
                
                VStack(alignment: .leading, spacing: 8) {
                    Text("Firmware Validation").font(.headline)
                    Text("Run vm doctor \(vm.name) in the CLI to inspect firmware checksums, storage, and backend capabilities. Launch succeeds only when its required checks pass.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                .padding(16)
                .background(Color(NSColor.controlBackgroundColor))
                .cornerRadius(10)

                // Boot Arguments
                VStack(alignment: .leading, spacing: 8) {
                    Text("Boot Arguments")
                        .font(.headline)
                    Text(vm.bootArgs)
                        .font(.system(.caption, design: .monospaced))
                        .padding(10)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color.black.opacity(0.1))
                        .cornerRadius(6)
                }
                .padding(16)
                .background(Color(NSColor.controlBackgroundColor))
                .cornerRadius(10)
            }
            .padding(.trailing, 20)
        }
        .padding(.vertical, 20)
    }
}

extension Color {
    init(hex: String) {
        let hex = hex.trimmingCharacters(in: CharacterSet.alphanumerics.inverted)
        var int: UInt64 = 0
        Scanner(string: hex).scanHexInt64(&int)
        let a, r, g, b: UInt64
        switch hex.count {
        case 3: // RGB (12-bit)
            (a, r, g, b) = (255, (int >> 8) * 17, (int >> 4 & 0xF) * 17, (int & 0xF) * 17)
        case 6: // RGB (24-bit)
            (a, r, g, b) = (255, int >> 16, int >> 8 & 0xFF, int & 0xFF)
        case 8: // ARGB (32-bit)
            (a, r, g, b) = (int >> 24, int >> 16 & 0xFF, int >> 8 & 0xFF, int & 0xFF)
        default:
            (a, r, g, b) = (1, 1, 1, 0)
        }
        self.init(
            .sRGB,
            red: Double(r) / 255,
            green: Double(g) / 255,
            blue:  Double(b) / 255,
            opacity: Double(a) / 255
        )
    }
}
