import SwiftUI

@main
struct CallWebhookApp: App {
    @StateObject private var monitor = CallMonitor()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(monitor)
                .task {
                    connectSIPIfConfigured()
                }
                .onChange(of: scenePhase) { _, newPhase in
                    if newPhase == .active {
                        connectSIPIfConfigured()
                    }
                }
        }
    }

    @MainActor
    private func connectSIPIfConfigured() {
        let defaults = UserDefaults.standard
        guard defaults.bool(forKey: "sipEnabled") else { return }

        do {
            try SIPService.shared.configureAndStart()
        } catch {
            // SIPService publishes the concrete status for the UI.
        }
    }
}
