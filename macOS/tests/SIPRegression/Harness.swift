import AppKit
import Foundation
import Combine
import SwiftUI

struct MacConfiguration {
    let host: String
    let username = "callwebhook-mac-test"
    func validate() throws {}
}
enum MacSecrets { static func get(_ name: String) -> String? { "isolated-test-password" } }
enum MacError: LocalizedError {
    case message(String)
    var errorDescription: String? { if case let .message(s) = self { return s }; return nil }
}

@MainActor final class RingObservation {
    var appearedAt: Double?
}
struct RingTestView: View {
    @ObservedObject var client: MacSIPClient
    let observation: RingObservation
    var body: some View {
        VStack {
            if client.incoming {
                Text("SIP regression: incoming").onAppear {
                    client.ringViewAppeared()
                    observation.appearedAt = Date().timeIntervalSince1970
                }
            } else { Text("SIP regression") }
        }.frame(width: 240, height: 70)
    }
}

@main
struct SIPRegression {
    @MainActor static func main() throws {
        UserDefaults.standard.removeObject(forKey: "callwebhook.callHistory.v1")
        let client = MacSIPClient()
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        let ringObservation = RingObservation()
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 240, height: 70), styleMask: [.borderless], backing: .buffered, defer: false)
        window.contentView = NSHostingView(rootView: RingTestView(client: client, observation: ringObservation))
        window.orderFrontRegardless()
        var stoppedAt: Double?
        var incomingAt: Double?
        let observation = client.$incoming.sink {
            if $0 { incomingAt = Date().timeIntervalSince1970 }
            else if incomingAt != nil { stoppedAt = Date().timeIntervalSince1970 }
        }
        try client.start(MacConfiguration(host: "127.0.0.1:\(CommandLine.arguments[1])"))
        let deadline = Date().addingTimeInterval(12)
        while Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.005))
            if CommandLine.arguments.contains("decline"), client.incoming, ringObservation.appearedAt != nil {
                client.hangup()
            }
            if let entry = LocalCallHistory.shared.entries.first, entry.endedAt != nil {
                let result: [String: Any] = ["status": entry.status, "incoming_at": incomingAt ?? 0,
                    "ring_stopped": !client.incoming && !client.active, "stopped_at": stoppedAt ?? 0,
                    "view_at": ringObservation.appearedAt ?? 0]
                let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
                print("RESULT " + String(decoding: data, as: UTF8.self))
                client.stop()
                withExtendedLifetime(observation) {}
                return
            }
        }
        print("RESULT timeout registered=\(client.registered) incoming=\(client.incoming)")
        client.stop()
        exit(1)
    }
}
