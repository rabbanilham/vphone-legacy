import SwiftUI

public struct SidebarView: View {
    @ObservedObject var vmManager: VMManager
    @Binding var showNewVMSheet: Bool
    @Binding var selectedTab: Int
    
    public init(vmManager: VMManager, showNewVMSheet: Binding<Bool>, selectedTab: Binding<Int>) {
        self.vmManager = vmManager
        self._showNewVMSheet = showNewVMSheet
        self._selectedTab = selectedTab
    }
    
    public var body: some View {
        VStack(spacing: 0) {
            // Header
            HStack(spacing: 10) {
                ZStack {
                    RoundedRectangle(cornerRadius: 8)
                        .fill(LinearGradient(colors: [Color.blue, Color.purple], startPoint: .topLeading, endPoint: .bottomTrailing))
                        .frame(width: 32, height: 32)
                    Image(systemName: "iphone.gen1")
                        .font(.system(size: 16, weight: .bold))
                        .foregroundColor(.white)
                }
                
                VStack(alignment: .leading, spacing: 2) {
                    Text("vphone-legacy")
                        .font(.headline)
                        .fontWeight(.bold)
                    Text("Legacy iOS Guest Manager")
                        .font(.caption2)
                        .foregroundColor(.secondary)
                }
                Spacer()
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 14)
            
            Divider()
            
            // VM List
            List(selection: $vmManager.selectedVM) {
                Section(header: Text("Virtual Machines").font(.caption).foregroundColor(.secondary)) {
                    if vmManager.vms.isEmpty {
                        Text("No VMs created yet")
                            .font(.caption)
                            .foregroundColor(.secondary)
                            .padding(.vertical, 8)
                    } else {
                        ForEach(vmManager.vms) { vm in
                            HStack(spacing: 10) {
                                Circle()
                                    .fill(vm.isRunning ? Color.green : Color.gray.opacity(0.5))
                                    .frame(width: 8, height: 8)
                                
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(vm.name)
                                        .font(.system(size: 13, weight: .semibold))
                                    Text(vm.device)
                                        .font(.caption2)
                                        .foregroundColor(.secondary)
                                }
                                
                                Spacer()
                                
                                Text("iOS \(vm.version)")
                                    .font(.system(size: 10, weight: .bold))
                                    .padding(.horizontal, 6)
                                    .padding(.vertical, 2)
                                    .background(Color.blue.opacity(0.15))
                                    .foregroundColor(.blue)
                                    .cornerRadius(4)
                            }
                            .padding(.vertical, 4)
                            .tag(vm)
                        }
                    }
                }
                
                Section(header: Text("Navigation").font(.caption).foregroundColor(.secondary)) {
                    Button(action: { selectedTab = 0 }) {
                        Label("Overview & Display", systemImage: "macwindow")
                    }
                    .foregroundColor(selectedTab == 0 ? .accentColor : .primary)
                    
                    Button(action: { selectedTab = 1 }) {
                        Label("Console Output", systemImage: "terminal")
                    }
                    .foregroundColor(selectedTab == 1 ? .accentColor : .primary)
                    
                    Button(action: { selectedTab = 2 }) {
                        Label("vphoned Agent", systemImage: "bolt.horizontal.circle")
                    }
                    .foregroundColor(selectedTab == 2 ? .accentColor : .primary)
                    
                    Button(action: { selectedTab = 3 }) {
                        Label("Firmware Manifest", systemImage: "shippingbox")
                    }
                    .foregroundColor(selectedTab == 3 ? .accentColor : .primary)
                }
            }
            .listStyle(SidebarListStyle())
            
            Divider()
            
            // Bottom Action Bar
            HStack {
                Button(action: {
                    showNewVMSheet = true
                }) {
                    Label("New VM", systemImage: "plus")
                        .font(.system(size: 12, weight: .medium))
                }
                .buttonStyle(.borderedProminent)
                
                Spacer()
                
                Button(action: {
                    vmManager.refreshVMs()
                }) {
                    Image(systemName: "arrow.clockwise")
                        .font(.system(size: 12))
                }
                .help("Refresh VM List")
                
                Button(action: {
                    NSWorkspace.shared.open(vmManager.vmsDirectory)
                }) {
                    Image(systemName: "folder")
                        .font(.system(size: 12))
                }
                .help("Reveal VMs in Finder")
            }
            .padding(12)
        }
        .frame(minWidth: 220, idealWidth: 240)
    }
}
