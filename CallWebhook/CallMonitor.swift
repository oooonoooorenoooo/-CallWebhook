import Foundation
import CallKit
import Combine

@MainActor
final class CallMonitor: NSObject, ObservableObject, CXCallObserverDelegate {
    @Published private(set) var active = false
    @Published private(set) var lastEvent = "App gestartet"
    @Published private(set) var log: [String] = []
    @Published private(set) var haState = "Token fehlt"
    @Published private(set) var fritzCallState = "Token fehlt"
    @Published private(set) var backgroundStatus = "Nicht gestartet"
    @Published private(set) var backgroundRemaining = "—"
    @Published private(set) var backgroundLastEvent = "—"
    @Published var haTriggerMode = UserDefaults.standard.string(forKey: "haTriggerMode") ?? "connected" {
        didSet { UserDefaults.standard.set(haTriggerMode, forKey: "haTriggerMode") }
    }
    @Published var haToken = "" {
        didSet {
            if !haToken.isEmpty, haToken != SetupKeychain.get(account: "home-assistant-token") {
                // A manually supplied long-lived token is not part of the OAuth session.
                do {
                    try SetupKeychain.set(haToken, account: "home-assistant-token")
                    SetupKeychain.delete(account: "home-assistant-refresh-token")
                    SetupKeychain.delete(account: "home-assistant-token-expiry")
                } catch { haState = "HA-Token konnte nicht gespeichert werden" }
            }
            UserDefaults.standard.removeObject(forKey: "haToken")
        }
    }

    private let observer = CXCallObserver()
    private var stateTask: Task<Void, Never>?
    private var baseURL: URL? { HomeAssistantConnection.configuredBase }
    private var entityID: String { UserDefaults.standard.string(forKey: "haCallEntityID") ?? "input_boolean.iphone_call_active" }
    private var fritzEntityID: String { UserDefaults.standard.string(forKey: "haFritzCallEntityID") ?? "sensor.fritz_box_5690_pro_anrufmonitor_telefonbuch" }

    override init() {
        super.init()
        haToken = SetupKeychain.get(account: "home-assistant-token") ?? UserDefaults.standard.string(forKey: "haToken") ?? ""
        observer.setDelegate(self, queue: .main)
        append("CXCallObserver aktiv")
        evaluateAndSend(force: true)
        if !haToken.isEmpty { refreshHAState() }
    }

    nonisolated func callObserver(_ callObserver: CXCallObserver, callChanged call: CXCall) {
        Task { @MainActor in
            let state: String
            if call.hasEnded {
                state = "beendet"
            } else if call.hasConnected {
                state = call.isOutgoing ? "ausgehend verbunden" : "eingehend verbunden"
            } else {
                state = call.isOutgoing ? "ausgehend" : "eingehend/klingelt"
            }
            self.backgroundLastEvent = "CallKit: \(state)"
            self.append("CallKit: \(state)")
            self.evaluateAndSend(call: call)
        }
    }

    func sendCurrentState() { evaluateAndSend(force: true) }

    func refreshHAState() {
        guard let base = baseURL else { haState = "HA noch nicht eingerichtet"; return }
        Task {
            do {
                let (data, code) = try await HomeAssistantConnection.request(base: base, path: "api/states/\(entityID)")
                guard code == 200, let object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let state = object["state"] as? String else { throw URLError(.badServerResponse) }
                haState = state.uppercased()
                let (fritzData, fritzCode) = try await HomeAssistantConnection.request(base: base, path: "api/states/\(fritzEntityID)")
                if fritzCode == 200, let object = try JSONSerialization.jsonObject(with: fritzData) as? [String: Any] {
                    fritzCallState = object["state"] as? String ?? "Unbekannt"
                } else { fritzCallState = "FRITZ!-Anrufmonitor nicht eingerichtet" }
            } catch {
                haState = "HA nicht erreichbar / Anmeldung prüfen"
                append("HA-Abfrage: \(error.localizedDescription)")
            }
        }
    }

    private func evaluateAndSend(force: Bool = false, call: CXCall? = nil) {
        let nowActive = haTriggerMode == "ringing" ? observer.calls.contains { !$0.hasEnded } : observer.calls.contains { !$0.hasEnded && $0.hasConnected }
        guard force || nowActive != active else { return }
        active = nowActive
        lastEvent = nowActive ? (haTriggerMode == "ringing" ? "Telefon aktiv" : "Gespräch verbunden") : "Kein Telefonat"
        guard let base = baseURL else { haState = "HA noch nicht eingerichtet"; return }
        // Serialize state changes so a slow ON cannot overwrite a later OFF.
        let previous = stateTask
        let entity = entityID
        stateTask = Task {
            await previous?.value
            do {
                let (_, code) = try await HomeAssistantConnection.request(base: base,
                    path: "api/services/input_boolean/\(nowActive ? "turn_on" : "turn_off")",
                    method: "POST", body: ["entity_id": entity])
                guard (200..<300).contains(code) else { throw URLError(.badServerResponse) }
                backgroundLastEvent = "Anrufstatus \(nowActive ? "ON" : "OFF") übertragen"
                haState = nowActive ? "ON" : "OFF"
                append(backgroundLastEvent)
            } catch {
                haState = "Anrufstatus nicht übertragen"
                append("HA-Anrufstatus: \(error.localizedDescription)")
            }
        }
    }

    private func append(_ text: String) {
        let time = Date().formatted(date: .omitted, time: .standard)
        log.insert("\(time)  \(text)", at: 0)
        if log.count > 50 { log.removeLast(log.count - 50) }
    }
}
