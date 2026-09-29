import SwiftUI

@main
struct CallWebhookApp: App {
    @UIApplicationDelegateAdaptor(CallWebhookAppDelegate.self) private var appDelegate
    @StateObject private var monitor = CallMonitor()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(monitor)
                .task {
                    connectSIPIfConfigured()
                    await synchronizePush()
                }
                .onChange(of: scenePhase) { _, newPhase in
                    if newPhase == .active {
                        connectSIPIfConfigured()
                        monitor.sendCurrentState()
                        Task { await synchronizePush() }
                    }
                }
        }
    }

    @MainActor
    private func synchronizePush() async {
        if UserDefaults.standard.bool(forKey: "setupCompleted") {
            await VoIPPushService.shared.completeSetup()
        } else {
            await VoIPPushService.shared.synchronize()
        }
    }

    @MainActor
    private func connectSIPIfConfigured() {
        let defaults = UserDefaults.standard
        guard defaults.bool(forKey: "sipEnabled") else { return }

        do {
            try SIPService.shared.ensureStarted()
        } catch {
            // SIPService publishes the concrete status for the UI.
        }
    }
}
