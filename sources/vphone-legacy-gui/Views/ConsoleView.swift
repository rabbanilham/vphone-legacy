import SwiftUI

public struct ConsoleView: View {
    @ObservedObject var vmManager: VMManager

    public init(vmManager: VMManager) { self.vmManager = vmManager }

    public var body: some View {
        VStack(spacing: 10) {
            HStack {
                Text("Launcher Output & Guest Serial Log")
                    .font(.caption).fontWeight(.semibold).foregroundStyle(.secondary)
                Spacer()
                Text(vmManager.isRunning ? "Launcher active" : "Launcher inactive")
                    .font(.caption2).foregroundStyle(.secondary)
                Button("Clear", systemImage: "trash") { vmManager.consoleOutput = "" }
            }
            Text("Output is read from the launcher. Log text does not establish that iOS has booted. Interactive serial input is unavailable; verified guest commands are available in the Agent tab when configured.")
                .font(.caption).foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
            ScrollViewReader { proxy in
                ScrollView {
                    Text(vmManager.consoleOutput.isEmpty ? "Waiting for launcher output..." : vmManager.consoleOutput)
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundColor(.green)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(14).id("bottom")
                }
                .background(Color.black.opacity(0.9)).cornerRadius(8)
                .onChange(of: vmManager.consoleOutput) { proxy.scrollTo("bottom", anchor: .bottom) }
            }
        }
        .padding(20).frame(minHeight: 450)
    }
}
