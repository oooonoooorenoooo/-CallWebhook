import SwiftUI

@main
struct CallWebhookApp: App {
    @StateObject private var monitor = CallMonitor()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(monitor)
        }
    }
}
