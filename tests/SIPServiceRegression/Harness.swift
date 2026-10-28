import Foundation
import Combine

// Platform doubles isolate SIPService from cellular calls, Keychain and CallKit.
// The production SIPService and actual Linphone core are compiled unchanged.
enum SetupKeychain {
    static func get(account: String) -> String? { nil }
    static func set(_ value: String, account: String) throws {}
}
final class CallProximityController {
    static let shared = CallProximityController()
    var isBlockingTouches = false
    func setCallInProgress(_ value: Bool) {}
}
final class CallAudioRouteController {
    static let shared = CallAudioRouteController()
    func setCallInProgress(_ value: Bool) {}
}
final class IncomingRouteRepair {
    static let shared = IncomingRouteRepair()
    var running = false
}
enum SystemCellularDialer {
    static func call(_ number: String, completion: (Bool) -> Void) { completion(false) }
}
@MainActor final class IncomingCallProvider {
    static let shared = IncomingCallProvider()
    var currentCallID: UUID?
    var hasCall: Bool { currentCallID != nil }
    var endedAt: Double?
    var answeredElsewhere = false
    var deferredHistory = false
    func attachSIP(caller: String, id: UUID?, line: Int?) -> Bool {
        let id = id ?? UUID()
        currentCallID = id
        LocalCallHistory.shared.begin(id: id, number: caller, incoming: true, line: line)
        return true
    }
    func end(id: UUID, failed: Bool = false, answeredElsewhere: Bool = false, recordHistory: Bool = true) {
        guard currentCallID == id else { return }
        ended(failed: failed, answeredElsewhere: answeredElsewhere, recordHistory: recordHistory)
    }
    func ended(failed: Bool = false, answeredElsewhere: Bool = false, recordHistory: Bool = true) {
        guard let id = currentCallID else { return }
        self.answeredElsewhere = answeredElsewhere
        endedAt = Date().timeIntervalSince1970
        deferredHistory = !recordHistory && LocalCallHistory.shared.entries.first(where: { $0.id == id })?.endedAt == nil
        if recordHistory { LocalCallHistory.shared.end(id, reason: failed ? "failed" : nil) }
        currentCallID = nil
    }
}

@main struct SIPServiceRegression {
    @MainActor static func main() throws {
        UserDefaults.standard.removeObject(forKey: "callwebhook.callHistory.v1")
        let client = SIPService.shared
        var incomingAt: Double?
        let observation = client.$incoming.sink { if $0 { incomingAt = Date().timeIntervalSince1970 } }
        try client.configureAndStart(host: "127.0.0.1:\(CommandLine.arguments[1])", username: "callwebhook-iphone-test", password: "isolated-test-password")
        let deadline = Date().addingTimeInterval(12)
        while Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.005))
            if CommandLine.arguments.contains("decline"), client.incoming { client.hangup() }
            if let entry = LocalCallHistory.shared.entries.first, entry.endedAt != nil {
                let provider = IncomingCallProvider.shared
                let result: [String: Any] = ["status": entry.status, "incoming_at": incomingAt ?? 0,
                    "ring_stopped": !client.incoming && !client.active && !provider.hasCall,
                    "stopped_at": provider.endedAt ?? 0,
                    "provider_answered_elsewhere": provider.answeredElsewhere,
                    "deferred_history": provider.deferredHistory]
                assert(provider.answeredElsewhere == (entry.status == "answered_elsewhere"))
                assert(provider.deferredHistory)
                print("RESULT " + String(decoding: try JSONSerialization.data(withJSONObject: result), as: UTF8.self))
                withExtendedLifetime(observation) {}
                return
            }
        }
        print("RESULT timeout registered=\(client.registered) incoming=\(client.incoming)")
        exit(1)
    }
}
