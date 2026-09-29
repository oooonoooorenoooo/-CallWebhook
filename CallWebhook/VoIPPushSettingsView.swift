import SwiftUI
import UniformTypeIdentifiers

/// Only the app operator provisions the common service. Regular installations
/// use the relay URL shipped with the app and never receive its APNs key.
struct VoIPPushSettingsView: View {
    @Binding var working: Bool
    @ObservedObject private var push = VoIPPushService.shared
    @State private var operatorSetup = false
    @State private var teamID = Bundle.main.object(forInfoDictionaryKey: "CallWebhookTeamID") as? String ?? ""
    @State private var keyID = ""
    @State private var privateKey = ""
    @State private var importing = false
    @State private var message = ""
    @State private var progress = 0

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Toggle("Ich betreibe den gemeinsamen Push-Dienst auf diesem HA-Pi", isOn: $operatorSetup)
                .disabled(working)
            if operatorSetup {
                Text("Einmalige Einrichtung für den App-Betreiber. Der Assistent installiert und startet den Dienst auf diesem Pi. Spätere Nutzer melden sich automatisch dort an.")
                    .font(.caption).foregroundStyle(.secondary)
                TextField("Apple Team-ID", text: $teamID)
                    .textInputAutocapitalization(.characters).autocorrectionDisabled().disabled(working)
                TextField("APNs Key-ID", text: $keyID)
                    .textInputAutocapitalization(.characters).autocorrectionDisabled().disabled(working)
                Button(privateKey.isEmpty ? "APNs-Schlüssel (.p8) auswählen" : "APNs-Schlüssel ausgewählt") { importing = true }
                    .disabled(working)
                Text("Nur beim ersten Einrichten oder nach vollständigem Löschen nötig. Vorhandene Schlüssel werden wiederverwendet. Ein App-Store-Connect-Schlüssel genügt nicht. Der Schlüssel bleibt beim Betreiber und wird nicht an andere Nutzer verteilt.")
                    .font(.caption).foregroundStyle(.secondary)
                Button("Push-Dienst automatisch einrichten") { Task { await provision() } }
                    .disabled(working || push.settingUp || SIPService.shared.active)
                HStack(spacing: 4) {
                    ForEach(0..<5) { index in
                        Capsule().fill(index < progress ? Color.green : Color.secondary.opacity(0.2)).frame(height: 6)
                    }
                }
                if working { ProgressView() }
                if !message.isEmpty { Text(message).font(.caption) }
            }
        }
        .fileImporter(isPresented: $importing, allowedContentTypes: [.data], allowsMultipleSelection: false) { result in
            do {
                guard let url = try result.get().first else { return }
                let access = url.startAccessingSecurityScopedResource()
                defer { if access { url.stopAccessingSecurityScopedResource() } }
                let data = try Data(contentsOf: url)
                guard data.count < 4096, let key = String(data: data, encoding: .utf8), key.contains("BEGIN PRIVATE KEY") else {
                    message = "Bitte eine gültige APNs-.p8-Datei auswählen."
                    return
                }
                privateKey = key
                let name = url.deletingPathExtension().lastPathComponent
                if name.hasPrefix("AuthKey_") { keyID = String(name.dropFirst(8)) }
                message = "Schlüssel ausgewählt. Jetzt automatisch einrichten."
            } catch { message = error.localizedDescription }
        }
    }

    @MainActor
    private func provision() async {
        guard !working, let base = HomeAssistantConnection.configuredBase else { return }
        working = true
        defer { working = false }
        do {
            var payload: [String: Any] = ["operator": true]
            if !privateKey.isEmpty {
                payload["apns_private_key"] = privateKey
                payload["apns_key_id"] = keyID.trimmingCharacters(in: .whitespacesAndNewlines)
                payload["apns_team_id"] = teamID.trimmingCharacters(in: .whitespacesAndNewlines)
            }
            let (data, code) = try await HomeAssistantConnection.request(base: base,
                path: "api/callwebhook/push-relay/setup", method: "POST", body: payload, timeout: 30)
            let reply = try JSONSerialization.jsonObject(with: data) as? [String: Any] ?? [:]
            guard code == 200 else {
                throw failure(reply["message"] as? String ?? "Push-Einrichtung benötigt die aktuelle HA-Integration und einen HA-Administrator.")
            }
            privateKey = ""
            payload.removeAll()
            let deadline = Date().addingTimeInterval(1500)
            while Date() < deadline {
                try Task.checkCancellation()
                let (bytes, status) = try await HomeAssistantConnection.request(base: base,
                    path: "api/callwebhook/push-relay/setup", timeout: 15)
                guard status == 200, let current = try JSONSerialization.jsonObject(with: bytes) as? [String: Any] else {
                    throw failure("Status nicht erreichbar. Die Installation läuft auf HA weiter; anschließend erneut prüfen.")
                }
                progress = current["progress_step"] as? Int ?? 0
                message = current["message"] as? String ?? "Push-Dienst wird eingerichtet …"
                if current["state"] as? String == "error" { throw failure(message) }
                if current["state"] as? String == "completed" {
                    try await PushRelayRegistration.shared.discoverOperatorService()
                    await push.completeSetup()
                    guard push.configured && push.routeReady else { throw failure(push.backendStatus) }
                    progress = 5
                    message = "Dienst erreichbar, iPhone angemeldet und Asterisk-Push eingerichtet. Jetzt den Testanruf bei gesperrtem iPhone durchführen."
                    return
                }
                try await Task.sleep(for: .milliseconds(500))
            }
            throw failure("Installation dauert noch an. Später erneut prüfen; vorhandene Schritte werden wiederverwendet.")
        } catch { message = error.localizedDescription }
    }

    private func failure(_ message: String) -> NSError {
        NSError(domain: "CallWebhook.OperatorSetup", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
    }
}
