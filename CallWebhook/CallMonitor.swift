import Foundation
import CallKit
import Combine

@MainActor
final class CallMonitor: NSObject, ObservableObject, CXCallObserverDelegate {
    @Published private(set) var active = false
    @Published private(set) var lastEvent = "App gestartet"
    @Published private(set) var log: [String] = []

    private let observer = CXCallObserver()
    private let onURL = URL(string: "https://vjid3noccsptgcivfuw9dqz15dzvygte.ui.nabu.casa/api/webhook/iphone_call_on_4d7a21")!
    private let offURL = URL(string: "https://vjid3noccsptgcivfuw9dqz15dzvygte.ui.nabu.casa/api/webhook/iphone_call_off_8c3f62")!

    override init() {
        super.init()
        observer.setDelegate(self, queue: .main)
        append("CXCallObserver aktiv")
        evaluateAndSend(force: true)
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
            self.evaluateAndSend()
        }
    }

    func sendCurrentState() {
        evaluateAndSend(force: true)
    }

    private func evaluateAndSend(force: Bool = false) {
        let nowActive = observer.calls.contains { !$0.hasEnded }
        guard force || nowActive != active else { return }
        active = nowActive
        lastEvent = nowActive ? "Telefon aktiv" : "Kein Telefonat"
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
                if let error {
                    self?.append("\(label) Fehler: \(error.localizedDescription)")
                } else {
                    self?.append("\(label) Webhook gesendet (HTTP \(code.map(String.init) ?? "?"))")
                }
            }
        }.resume()
    }

    private func append(_ text: String) {
        let time = Date().formatted(date: .omitted, time: .standard)
        log.insert("\(time)  \(text)", at: 0)
        if log.count > 50 {
            log.removeLast(log.count - 50)
        }
    }
}
