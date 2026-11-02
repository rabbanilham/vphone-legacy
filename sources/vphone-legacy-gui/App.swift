import SwiftUI

@main
public struct VPhoneLegacyApp: App {
    @State private var appState = AppState()

    public init() {}

    public var body: some Scene {
        WindowGroup("vphone-legacy — iPhone 5s Emulator") {
            Group {
                if !appState.repoLocator.isValidRepo {
                    SetupWizardView(appState: appState)
                } else {
                    LibraryView(appState: appState)
                }
            }
            .alert("vphone-legacy", isPresented: Binding(
                get: { appState.errorMessage != nil },
                set: { if !$0 { appState.errorMessage = nil } })) {
                Button("OK", role: .cancel) {}
            } message: {
                Text(appState.errorMessage ?? "")
            }
            .frame(minWidth: 900, minHeight: 620)
            .task {
                await appState.checkSetup()
                await appState.refreshVMs()
            }
        }
        .windowStyle(.titleBar)
        .windowToolbarStyle(.unified)
    }
}
