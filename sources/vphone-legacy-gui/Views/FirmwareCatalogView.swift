import SwiftUI

public struct FirmwareCatalogView: View {
    let vm: VirtualMachineInfo

    public init(vm: VirtualMachineInfo) { self.vm = vm }

    private var manifest: String {
        let url = URL(fileURLWithPath: vm.path).appendingPathComponent("vm_manifest.json")
        guard let data = try? Data(contentsOf: url),
              let json = try? JSONSerialization.jsonObject(with: data),
              let formatted = try? JSONSerialization.data(withJSONObject: json, options: [.prettyPrinted, .sortedKeys]),
              let text = String(data: formatted, encoding: .utf8) else {
            return "Manifest unavailable or invalid."
        }
        return text
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Firmware Identity & Bundle Manifest").font(.title3).fontWeight(.bold)
            Text("Current bring-up target: iPhone 5s n53ap, iOS 7.1.2 (11D257). iOS 8–9 are deferred. A bundle manifest records artifacts; it does not prove that the guest can boot.")
                .font(.caption).foregroundStyle(.secondary)
            Text("Run vm doctor \(vm.name) to validate the selected bundle and inspect backend capabilities.")
                .font(.system(.caption, design: .monospaced))
            Text(manifest)
                .font(.system(size: 11, design: .monospaced))
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(14)
                .background(Color(NSColor.controlBackgroundColor))
                .cornerRadius(10)
        }
        .padding(20)
    }
}
