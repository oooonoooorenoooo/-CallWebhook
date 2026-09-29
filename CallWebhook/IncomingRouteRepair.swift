import Foundation

@MainActor
final class IncomingRouteRepair: ObservableObject {
    static let shared = IncomingRouteRepair()
    @Published private(set) var running = false
    @Published private(set) var status = "Aktualisiert die Rufweiterleitung zum iPhone und startet Asterisk neu."

    func run() async {
        guard !running, !SIPService.shared.active else { return }
        running = true
        defer { running = false }
        do {
            guard let base = HomeAssistantConnection.configuredBase,
                  let pjsip = SetupKeychain.get(account: "asterisk-pjsip-generated"),
                  let old = SetupKeychain.get(account: "asterisk-extensions-generated") else {
                throw failure("Gespeicherte Asterisk-Konfiguration oder HA-Adresse fehlt.")
            }
            let extensions = IncomingDialplan.addingIncomingRoute(to: old)
            let defaults = UserDefaults.standard
            var body: [String: Any] = ["pjsip": pjsip, "extensions": extensions]
            for line in 1...3 {
                guard let tam = defaults.object(forKey: "setupMailbox\(line)TAM") as? Int else {
                    throw failure("Anrufbeantworter-Zuordnung für Leitung \(line) fehlt. Bitte in den Einstellungen prüfen.")
                }
                body["mailbox_tam_\(line)"] = tam
            }
            status = "Übergebe Rufweiterleitung an Home Assistant …"
            let (data, code) = try await HomeAssistantConnection.request(base: base, path: "api/callwebhook/setup/asterisk", method: "POST", body: body)
            let started = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            guard (200..<300).contains(code), started?["state"] as? String == "started" else {
                throw failure("Asterisk-Auftrag nicht gestartet (HTTP \(code)). Möglicherweise läuft noch eine andere Aufgabe; bitte später erneut versuchen.")
            }
            let deadline = Date().addingTimeInterval(600)
            while Date() < deadline {
                try await Task.sleep(for: .milliseconds(700))
                let (data, code) = try await HomeAssistantConnection.request(base: base, path: "api/callwebhook/setup/asterisk/status")
                guard code == 200, let result = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                    throw failure("Asterisk-Status konnte nicht gelesen werden.")
                }
                status = result["message"] as? String ?? "Asterisk wird aktualisiert …"
                if result["state"] as? String == "error" { throw failure(status) }
                if result["state"] as? String == "done" {
                    guard (result["result"] as? [String: Any])?["config_verified"] as? Bool == true else {
                        throw failure("Neue Konfiguration wurde nicht bestätigt.")
                    }
                    try SetupKeychain.set(extensions, account: "asterisk-extensions-generated")
                    try SIPService.shared.configureAndStart()
                    status = "Rufweiterleitung installiert. SIP verbindet sich erneut – danach bei geöffneter App einen Testanruf durchführen."
                    return
                }
            }
            throw failure("Zeitlimit erreicht. Der HA-Auftrag kann noch laufen; Status dort prüfen.")
        } catch { status = "Reparatur nicht bestätigt: \(error.localizedDescription)" }
    }

    private func failure(_ message: String) -> NSError {
        NSError(domain: "CallWebhook.IncomingRoute", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
    }
}
