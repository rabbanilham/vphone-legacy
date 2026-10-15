import SwiftUI

public struct AgentView: View {
    @ObservedObject var vmManager: VMManager
    @State private var commandInput = "uname -a"
    @State private var commandResult = ""
    @State private var verificationStatus = "Unverified"
    @State private var isChecking = false
    @State private var isExecuting = false

    public init(vmManager: VMManager) { self.vmManager = vmManager }

    public var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    Text("Guest Agent Identity").font(.headline)
                    Spacer()
                    Button("Verify Guest") {
                        isChecking = true
                        vmManager.verifyAgent { status in
                            isChecking = false
                            verificationStatus = status
                        }
                    }
                    .disabled(!vmManager.isRunning || isChecking)
                }
                Text("A runtime transport must be configured and verified. SystemVersion.plist must identify iPhone OS 7.1.2 (11D257) before commands are enabled.")
                    .font(.caption).foregroundStyle(.secondary)
                Text(vmManager.isAgentConnected ? "Guest identity verified" : verificationStatus)
                    .font(.system(.callout, design: .monospaced))
                if isChecking { ProgressView() }
            }
            VStack(alignment: .leading, spacing: 12) {
                Text("Guest Shell Execution").font(.headline)
                HStack {
                    TextField("Command to execute inside the verified guest", text: $commandInput)
                        .font(.system(.body, design: .monospaced))
                        .textFieldStyle(.roundedBorder)
                    Button("Execute") { executeCommand() }
                        .disabled(!vmManager.isAgentConnected || isExecuting || commandInput.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
                if !commandResult.isEmpty {
                    Text(commandResult).font(.system(size: 11, design: .monospaced))
                        .textSelection(.enabled).padding(12)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color.black.opacity(0.85)).foregroundColor(.green).cornerRadius(6)
                }
            }
            Text("GPS injection is unavailable until the guest exposes a supported control channel.")
                .font(.caption).foregroundStyle(.secondary)
        }
        .padding(20)
    }

    private func executeCommand() {
        isExecuting = true
        vmManager.agentSend(cmd: "exec", args: ["command": commandInput]) { response in
            isExecuting = false
            guard let response, response["status"] as? String == "ok",
                  let output = response["output"] as? String, let code = response["exit_code"] as? Int else {
                commandResult = "Error: no valid response from the verified guest agent."
                return
            }
            commandResult = "[Exit: \(code)]\n\(output)"
        }
    }
}
