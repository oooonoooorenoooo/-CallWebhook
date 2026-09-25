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
    @Published var haTriggerMode = UserDefaults.standard.string(forKey: "haTriggerMode") ?? "connected" {
        didSet { UserDefaults.standard.set(haTriggerMode, forKey: "haTriggerMode") }
    }
    @Published var haToken = "" {
        didSet { UserDefaults.standard.set(haToken, forKey: "haToken") }
    }

    private let observer = CXCallObserver()
    private let baseURL = "https://vjid3noccsptgcivfuw9dqz15dzvygte.ui.nabu.casa"
    private let entityID = "input_boolean.iphone_call_active"
    private let fritzEntityID = "sensor.fritz_box_5690_pro_anrufmonitor_telefonbuch"
    private let onURL = URL(string: "https://vjid3noccsptgcivfuw9dqz15dzvygte.ui.nabu.casa/api/webhook/iphone_call_on_4d7a21")!
    private let offURL = URL(string: "https://vjid3noccsptgcivfuw9dqz15dzvygte.ui.nabu.casa/api/webhook/iphone_call_off_8c3f62")!

    override init() {
        super.init()
        haToken = UserDefaults.standard.string(forKey: "haToken") ?? ""
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
            self.append("CallKit: \(state)")
            self.evaluateAndSend(call: call)
        }
    }

    func sendCurrentState() {
        evaluateAndSend(force: true)
    }

    func refreshHAState() {
        guard !haToken.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            haState = "Token fehlt"
            fritzCallState = "Token fehlt"
            return
        }
        guard let url = URL(string: "\(baseURL)/api/states/\(entityID)") else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.setValue("Bearer \(haToken.trimmingCharacters(in: .whitespacesAndNewlines))", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            let code = (response as? HTTPURLResponse)?.statusCode
            Task { @MainActor in
                guard let self else { return }
                if let error {
                    self.haState = "nicht erreichbar"
                    self.append("HA-Abfrage Fehler: \(error.localizedDescription)")
                    return
                }
                guard code == 200, let data,
                      let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let state = object["state"] as? String else {
                    self.haState = code == 401 ? "Token ungültig" : "Fehler HTTP \(code.map(String.init) ?? "?")"
                    self.append("HA-Abfrage: \(self.haState)")
                    return
                }
                self.haState = state.uppercased()
                self.append("HA-Status: \(self.haState)")
            }
        }.resume()
        refreshFritzCallState()
    }

    private func refreshFritzCallState() {
        guard !haToken.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              let url = URL(string: "\(baseURL)/api/states/\(fritzEntityID)") else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.setValue("Bearer \(haToken.trimmingCharacters(in: .whitespacesAndNewlines))", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            let code = (response as? HTTPURLResponse)?.statusCode
            Task { @MainActor in
                guard let self else { return }
                if error != nil {
                    self.fritzCallState = "nicht erreichbar"
                    return
                }
                guard code == 200, let data,
                      let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let state = object["state"] as? String else {
                    self.fritzCallState = code == 401 ? "Token ungültig" : "Fehler HTTP \(code.map(String.init) ?? "?")"
                    return
                }
                switch state {
                case "idle": self.fritzCallState = "Bereit"
                case "ringing": self.fritzCallState = "Klingelt"
                case "dialing": self.fritzCallState = "Wählt"
                case "talking": self.fritzCallState = "Gespräch verbunden"
                default: self.fritzCallState = state
                }
            }
        }.resume()
    }

    private func evaluateAndSend(force: Bool = false, call: CXCall? = nil) {
        let nowActive: Bool
        if haTriggerMode == "ringing" {
            nowActive = observer.calls.contains { !$0.hasEnded }
        } else {
            nowActive = observer.calls.contains { !$0.hasEnded && $0.hasConnected }
        }
        guard force || nowActive != active else { return }
        active = nowActive
        lastEvent = nowActive ? (haTriggerMode == "ringing" ? "Telefon aktiv" : "Gespräch verbunden") : "Kein Telefonat"
        post(nowActive ? onURL : offURL, label: nowActive ? "ON" : "OFF")
    }

    private func post(_ url: URL, label: String) {
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = Data("{}".utf8)
        URLSession.shared.dataTask(with: request) { [weak self] _, response, error in
            let code = (response as? HTTPURLResponse)?.statusCode
            Task { @MainActor in
                guard let self else { return }
                if let error {
                    self.append("\(label) Fehler: \(error.localizedDescription)")
                } else {
                    self.append("\(label) Webhook gesendet (HTTP \(code.map(String.init) ?? "?"))")
                    if let code, (200...299).contains(code) {
                        try? await Task.sleep(for: .milliseconds(500))
                        self.refreshHAState()
                    }
                }
            }
        }.resume()
    }

    private func append(_ text: String) {
        let time = Date().formatted(date: .omitted, time: .standard)
        log.insert("\(time)  \(text)", at: 0)
        if log.count > 50 { log.removeLast(log.count - 50) }
    }
}
