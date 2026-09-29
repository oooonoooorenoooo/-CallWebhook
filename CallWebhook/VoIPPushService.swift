import Foundation
import PushKit
import UIKit

@MainActor
final class VoIPPushService: NSObject, ObservableObject, PKPushRegistryDelegate {
    static let shared = VoIPPushService()
    @Published private(set) var status = "Anruf-Push wird vorbereitet"
    @Published private(set) var backendStatus = ""
    @Published private(set) var configured = false
    private var registry: PKPushRegistry?
    private var token = ""
    private var wakeTasks: [UUID: Task<Void, Never>] = [:]

    var environment: String? {
        // CI checks this against the actual signed entitlement for each artifact.
        let value = Bundle.main.object(forInfoDictionaryKey: "CallWebhookPushEnvironment") as? String
        return ["development", "production"].contains(value ?? "") ? value : nil
    }

    func start() {
        guard registry == nil else { return }
        // Instantiate CallKit before PushKit can deliver a cold-start notification.
        _ = IncomingCallProvider.shared
        guard environment != nil else {
            status = "Apple-Push-Berechtigung fehlt im signierten App-Profil"
            return
        }
        let registry = PKPushRegistry(queue: .main)
        registry.delegate = self
        self.registry = registry
        registry.desiredPushTypes = [.voIP]
    }

    func synchronize() async {
        start()
        guard !token.isEmpty else { return }
        do {
            _ = try await request(path: "api/callwebhook/voip", method: "POST", body: [
                "action": "register", "token": token, "environment": environment ?? ""
            ])
            status = "iPhone für Anruf-Push registriert"
            await refreshStatus()
        } catch { status = error.localizedDescription }
    }

    func updateBackend() async {
        guard let base = HomeAssistantConnection.configuredBase else { return }
        backendStatus = "Suche installierten CallWebhook Bootstrap …"
        do {
            func supervisor(_ endpoint: String, method: String = "get") async throws -> [String: Any] {
                let result = try await HomeAssistantConnection.command(base: base, payload: [
                    "type": "supervisor/api", "endpoint": endpoint, "method": method, "timeout": 15
                ]) as? [String: Any] ?? [:]
                return result["data"] as? [String: Any] ?? result
            }
            let store = try await supervisor("/store")
            let repositories = store["repositories"] as? [[String: Any]] ?? []
            let ids = Set(repositories.compactMap { item -> String? in
                let source = (item["source"] as? String ?? "").trimmingCharacters(in: CharacterSet(charactersIn: "/"))
                guard ["https://github.com/oooonoooorenoooo/-CallWebhook", "https://github.com/oooonoooorenoooo/-CallWebhook.git"].contains(source) else { return nil }
                return item["slug"] as? String
            })
            let addons = (store["addons"] as? [[String: Any]]) ?? (store["apps"] as? [[String: Any]]) ?? []
            guard let slug = addons.compactMap({ $0["slug"] as? String }).first(where: { slug in ids.contains(where: { slug == $0 + "_callwebhook_bootstrap" }) }) else {
                throw failure("CallWebhook Bootstrap nicht gefunden")
            }
            let info = try await supervisor("/addons/\(slug)/info")
            guard info["state"] as? String != "started" else { throw failure("Bootstrap arbeitet noch. Bitte nach Abschluss erneut versuchen.") }
            _ = try await supervisor("/addons/\(slug)/start", method: "post")
            backendStatus = "Backend wird installiert; warte auf Home Assistant …"
            let deadline = Date().addingTimeInterval(600)
            while Date() < deadline {
                try await Task.sleep(for: .seconds(1))
                if let (data, code) = try? await HomeAssistantConnection.request(base: base, path: "api/callwebhook/setup/status"),
                   code == 200, let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                   (value["api_version"] as? Int ?? 0) >= 6, value["ready_for_asterisk"] as? Bool == true {
                    await synchronize()
                    await refreshStatus()
                    return
                }
            }
            throw failure("HA-Neustart noch nicht bestätigt. Push-Status später erneut prüfen.")
        } catch { backendStatus = error.localizedDescription }
    }

    func refreshStatus() async {
        do {
            let value = try await request(path: "api/callwebhook/voip")
            configured = value["configured"] as? Bool == true
            backendStatus = configured ? (value["message"] as? String ?? "Push-Schlüssel hinterlegt") : "Apple-Push-Schlüssel auf Home Assistant fehlt"
        } catch { backendStatus = error.localizedDescription }
    }

    func saveCredentials(key: String, keyID: String, teamID: String) async throws {
        guard !token.isEmpty, environment != nil else {
            throw failure("Apple hat noch keinen VoIP-Push-Token geliefert. Signiertes Push-Profil und Internetverbindung prüfen.")
        }
        _ = try await request(path: "api/callwebhook/voip", method: "POST", body: [
            "action": "register", "token": token, "environment": environment ?? ""
        ])
        _ = try await request(path: "api/callwebhook/voip", method: "POST", body: [
            "action": "credentials", "key": key, "key_id": keyID, "team_id": teamID
        ])
        await synchronize()
        await refreshStatus()
    }

    func callState(_ id: UUID, action: String? = nil) async throws -> [String: Any] {
        try await request(path: "api/callwebhook/voip/call/\(id.uuidString.lowercased())",
                          method: action == nil ? "GET" : "POST", body: action.map { ["action": $0] })
    }

    func finish(_ id: UUID) {
        wakeTasks.removeValue(forKey: id)?.cancel()
        Task { _ = try? await callState(id, action: "end") }
    }

    private func request(path: String, method: String = "GET", body: [String: Any]? = nil) async throws -> [String: Any] {
        guard let base = HomeAssistantConnection.configuredBase else { throw failure("Home Assistant noch nicht eingerichtet") }
        let (data, code) = try await HomeAssistantConnection.request(base: base, path: path, method: method, body: body)
        guard (200..<300).contains(code) else {
            throw failure(code == 404 ? "HA-Backend aktualisieren: CallWebhook Bootstrap starten und HA-Neustart abwarten" : "Anruf-Push: HA antwortet mit HTTP \(code)")
        }
        return try JSONSerialization.jsonObject(with: data) as? [String: Any] ?? [:]
    }

    private func failure(_ message: String) -> NSError {
        NSError(domain: "CallWebhook.Push", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
    }

    nonisolated func pushRegistry(_ registry: PKPushRegistry, didUpdate pushCredentials: PKPushCredentials, for type: PKPushType) {
        let value = pushCredentials.token.map { String(format: "%02x", $0) }.joined()
        Task { @MainActor in self.token = value; await self.synchronize() }
    }

    nonisolated func pushRegistry(_ registry: PKPushRegistry, didInvalidatePushTokenFor type: PKPushType) {
        Task { @MainActor in
            let old = self.token
            self.token = ""
            self.status = "Apple erneuert den Anruf-Push-Token"
            _ = try? await self.request(path: "api/callwebhook/voip", method: "POST", body: ["action": "unregister", "token": old])
        }
    }

    nonisolated func pushRegistry(_ registry: PKPushRegistry, didReceiveIncomingPushWith payload: PKPushPayload,
                                 for type: PKPushType, completion: @escaping () -> Void) {
        // Delegate queue is .main: report to CallKit synchronously, before any network work.
        MainActor.assumeIsolated {
            let data = payload.dictionaryPayload
            let id = (data["call_id"] as? String).flatMap(UUID.init(uuidString:)) ?? UUID()
            let caller = data["caller"] as? String ?? "Unbekannt"
            IncomingCallProvider.shared.report(caller: caller, id: id, completion: completion)
            guard wakeTasks[id] == nil else { return }
            wakeTasks[id] = Task { @MainActor in
                defer { self.wakeTasks[id] = nil }
                do {
                    guard let sent = data["sent_at"] as? TimeInterval, abs(Date().timeIntervalSince1970 - sent) < 60 else {
                        throw self.failure("Veralteter Anruf-Push")
                    }
                    try SIPService.shared.wakeForIncomingCall()
                    let registrationDeadline = Date().addingTimeInterval(7)
                    while !SIPService.shared.registered && Date() < registrationDeadline {
                        try await Task.sleep(for: .milliseconds(100))
                    }
                    guard SIPService.shared.registered else { throw self.failure("SIP nach Anruf-Push nicht erreichbar – Heimnetz/VPN prüfen") }
                    _ = try await self.callState(id, action: "ready")
                    let inviteDeadline = Date().addingTimeInterval(20)
                    while IncomingCallProvider.shared.isWaiting(for: id) && Date() < inviteDeadline {
                        try await Task.sleep(for: .seconds(1))
                        let state = try await self.callState(id)
                        if state["active"] as? Bool != true { throw self.failure("Anrufer hat aufgelegt") }
                    }
                    if IncomingCallProvider.shared.isWaiting(for: id) { throw self.failure("Kein SIP-Anruf nach Push empfangen") }
                } catch is CancellationError {
                    return
                } catch {
                    self.status = error.localizedDescription
                    IncomingCallProvider.shared.end(id: id, failed: true)
                }
            }
        }
    }
}

final class CallWebhookAppDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        VoIPPushService.shared.start()
        return true
    }
}
