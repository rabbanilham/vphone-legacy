import SwiftUI

/// Manager status only. Guest pixels belong to the QEMU display window.
public struct VMStatusView: View {
    @ObservedObject var vmManager: VMManager
    let vm: VirtualMachineInfo

    public init(vmManager: VMManager, vm: VirtualMachineInfo) {
        self.vmManager = vmManager
        self.vm = vm
    }

    private var status: VMRuntimeStatus? {
        if vmManager.isRunning, let active = vmManager.activeVMName, active != vm.name { return nil }
        return vmManager.runtimeStatus
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Label("Guest Display", systemImage: "macwindow")
                .font(.headline)
            Text("The QEMU window displays pixels rendered by the guest. This manager does not draw an iOS screen.")
                .font(.callout)
                .foregroundStyle(.secondary)
            Divider()
            Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 12) {
                row("Launcher", vmManager.isRunning && vmManager.activeVMName == vm.name ? "active" : "inactive")
                row("QEMU process", status?.process.phase ?? "unverified")
                row("Kernel", status?.kernel.description ?? "unverified")
                row("Userspace", status?.userspace.description ?? "unverified")
                row("Guest display", status?.display.description ?? "unverified")
                row("Guest agent", vmManager.isAgentConnected && vmManager.activeVMName == vm.name ? "identity verified" : "unverified")
            }
            if let shutdown = status?.shutdown, let clean = shutdown.clean {
                Text(clean ? "Last shutdown: clean" : "Last shutdown: unclean")
                    .font(.caption)
                if let reason = shutdown.reason { Text(reason).font(.caption).foregroundStyle(.secondary) }
            }
            Text("Home, Power, volume, and touch input in the manager are unavailable until the backend exposes real guest input.")
                .font(.caption)
                .foregroundStyle(.secondary)
            Button("Refresh Runtime Status") { vmManager.refreshRuntimeStatus() }
        }
        .padding(18)
        .frame(width: 300, alignment: .leading)
        .background(Color(NSColor.controlBackgroundColor))
        .cornerRadius(10)
    }

    private func row(_ title: String, _ value: String) -> some View {
        GridRow {
            Text(title).foregroundStyle(.secondary)
            Text(value).font(.system(.callout, design: .monospaced))
        }
    }
}
