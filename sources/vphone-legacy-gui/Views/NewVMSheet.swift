import SwiftUI

public struct NewVMSheet: View {
    @ObservedObject var vmManager: VMManager
    @Binding var isPresented: Bool
    
    @State private var name: String = ""
    private let version = "7.1.2"
    @State private var ipswPath: String = ""
    
    @State private var isCreating: Bool = false
    @State private var statusMessage: String = ""
    
    public init(vmManager: VMManager, isPresented: Binding<Bool>) {
        self.vmManager = vmManager
        self._isPresented = isPresented
    }
    
    public var body: some View {
        VStack(spacing: 0) {
            // Header
            HStack {
                Text("New Legacy Virtual Machine")
                    .font(.headline)
                Spacer()
                Button("Cancel") {
                    isPresented = false
                }
                .disabled(isCreating)
            }
            .padding(18)
            
            Divider()
            
            // Form Content
            Form {
                Section {
                    TextField("VM Name (e.g. ios7_iphone5s):", text: $name)
                    
                    Text("Firmware target: iOS 7.1.2 (11D257)")
                    Text("Boot arguments: -v serial=3")
                        .font(.system(.caption, design: .monospaced))
                    
                    Text("Guest requirements: Apple A7 • n53ap • 1 CPU • 1024 MB RAM")
                        .font(.caption).foregroundStyle(.secondary)
                }
                
                Section(header: Text("Required Local Firmware IPSW")) {
                    HStack {
                        TextField("Path to local iPhone6,2 IPSW:", text: $ipswPath)
                        Button("Browse...") {
                            let panel = NSOpenPanel()
                            panel.allowedContentTypes = []
                            panel.allowsMultipleSelection = false
                            panel.canChooseDirectories = false
                            if panel.runModal() == .OK, let url = panel.url {
                                ipswPath = url.path
                            }
                        }
                    }
                }
                
            }
            .padding(20)
            
            if !statusMessage.isEmpty {
                Text(statusMessage)
                    .font(.caption)
                    .foregroundColor(statusMessage.contains("Error") ? .red : .green)
                    .padding(.horizontal, 20)
                    .padding(.bottom, 8)
            }
            
            Divider()
            
            // Footer Action
            HStack {
                Spacer()
                if isCreating {
                    ProgressView()
                        .scaleEffect(0.8)
                        .padding(.trailing, 8)
                }
                Button("Create Virtual Machine") {
                    createVMAction()
                }
                .buttonStyle(.borderedProminent)
                .disabled(name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || ipswPath.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || isCreating)
            }
            .padding(16)
        }
        .frame(width: 520, height: 420)
    }
    
    private func createVMAction() {
        isCreating = true
        statusMessage = "Staging firmware and creating bundle..."
        
        vmManager.createVM(
            name: name.trimmingCharacters(in: .whitespaces),
            version: version,
            ipswPath: ipswPath
        ) { success, output in
            isCreating = false
            if success {
                statusMessage = "Created successfully!"
                isPresented = false
            } else {
                statusMessage = "Error: \(output)"
            }
        }
    }
}
