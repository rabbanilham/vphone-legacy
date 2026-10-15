import SwiftUI

@main
public struct VPhoneLegacyApp: App {
    @StateObject private var vmManager = VMManager()
    @State private var showNewVMSheet: Bool = false
    @State private var selectedTab: Int = 0
    
    public init() {}
    
    public var body: some Scene {
        WindowGroup("vphone-legacy — Guest Manager") {
            HSplitView {
                SidebarView(
                    vmManager: vmManager,
                    showNewVMSheet: $showNewVMSheet,
                    selectedTab: $selectedTab
                )
                
                if let vm = vmManager.selectedVM {
                    VMDetailView(
                        vmManager: vmManager,
                        vm: vm,
                        selectedTab: $selectedTab
                    )
                } else {
                    VStack(spacing: 16) {
                        Image(systemName: "iphone.gen1")
                            .font(.system(size: 64))
                            .foregroundColor(.secondary)
                        Text("No Virtual Machine Selected")
                            .font(.title2)
                            .fontWeight(.medium)
                        Text("Select a virtual machine from the sidebar or click New VM to create one.")
                            .font(.body)
                            .foregroundColor(.secondary)
                        Button(action: { showNewVMSheet = true }) {
                            Label("Create New VM", systemImage: "plus")
                        }
                        .buttonStyle(.borderedProminent)
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
            .frame(minWidth: 880, minHeight: 600)
            .sheet(isPresented: $showNewVMSheet) {
                NewVMSheet(vmManager: vmManager, isPresented: $showNewVMSheet)
            }
        }
        .windowStyle(TitleBarWindowStyle())
        .windowToolbarStyle(UnifiedWindowToolbarStyle())
    }
}
