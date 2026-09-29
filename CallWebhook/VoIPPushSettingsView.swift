import SwiftUI
import UniformTypeIdentifiers

struct VoIPPushSettingsView: View {
    @ObservedObject private var push = VoIPPushService.shared
    @ObservedObject private var repair = IncomingRouteRepair.shared
    @ObservedObject private var sip = SIPService.shared
    @State private var teamID = Bundle.main.object(forInfoDictionaryKey: "CallWebhookTeamID") as? String ?? ""
    @State private var keyID = ""
    @State private var privateKey = ""
    @State private var changeKey = false
    @State private var importing = false
    @State private var working = false
    @State private var message = ""

    var body: some View {
        DisclosureGroup("Anrufe im Hintergrund / VoIP-Push") {
            Text(push.status).font(.caption)
            Text(push.backendStatus).font(.caption)
            Button("HA-Anruf-Push installieren / aktualisieren") {
                Task {
                    working = true
                    defer { working = false }
                    await push.updateBackend()
                }
            }.disabled(working || repair.running || sip.active)
            if push.configured {
                Button("Vorhandene Push-Einrichtung wieder aktivieren") {
                    Task {
                        working = true
                        defer { working = false }
                        do {
                            try await push.reuseExistingConfiguration()
                            await repair.run(requireVoIP: true)
                            message = repair.status
                        } catch { message = error.localizedDescription }
                    }
                }.disabled(working || repair.running || sip.active)
                Toggle("Apple-Push-Schlüssel ändern", isOn: $changeKey)
            }
            if !push.configured || changeKey {
                TextField("Apple Team-ID", text: $teamID)
                    .textInputAutocapitalization(.characters).autocorrectionDisabled()
                TextField("Apple Push Key-ID", text: $keyID)
                    .textInputAutocapitalization(.characters).autocorrectionDisabled()
                Button(privateKey.isEmpty ? "Apple-Push-Schlüssel (.p8) auswählen" : "Push-Schlüssel ausgewählt") { importing = true }
                Text("Der Schlüssel benötigt Apple Push Notifications (APNs). Ein App-Store-Connect-Schlüssel ist dafür nicht geeignet. Er wird auf deinem Home Assistant gespeichert.")
                    .font(.caption).foregroundStyle(.secondary)
                Button("Anruf-Push speichern und Asterisk aktivieren") {
                    Task {
                        working = true
                        defer { working = false }
                        do {
                            try await push.saveCredentials(key: privateKey, keyID: keyID.trimmingCharacters(in: .whitespacesAndNewlines), teamID: teamID.trimmingCharacters(in: .whitespacesAndNewlines))
                            privateKey = ""
                            message = "Push-Schlüssel gespeichert; Asterisk wird aktualisiert …"
                            await repair.run(requireVoIP: true)
                            message = repair.status
                        } catch { message = error.localizedDescription }
                    }
                }.disabled(privateKey.isEmpty || keyID.isEmpty || teamID.isEmpty || working || repair.running || sip.active)
            }
            Button("Push-Status prüfen") { Task { await push.synchronize(); await push.refreshStatus() } }
            if !message.isEmpty { Text(message).font(.caption) }
            if working { ProgressView() }
            Text("Für die Gesprächsverbindung muss Asterisk über Heimnetz oder VPN erreichbar sein.")
                .font(.caption).foregroundStyle(.secondary)
        }
        .task { await push.synchronize(); await push.refreshStatus() }
        .fileImporter(isPresented: $importing, allowedContentTypes: [.data], allowsMultipleSelection: false) { result in
            do {
                guard let url = try result.get().first else { return }
                let access = url.startAccessingSecurityScopedResource()
                defer { if access { url.stopAccessingSecurityScopedResource() } }
                let data = try Data(contentsOf: url)
                guard data.count < 4096, let key = String(data: data, encoding: .utf8), key.contains("BEGIN PRIVATE KEY") else {
                    message = "Bitte einen Apple-APNs-Schlüssel im .p8-Format auswählen."
                    return
                }
                privateKey = key
                let name = url.deletingPathExtension().lastPathComponent
                if name.hasPrefix("AuthKey_") { keyID = String(name.dropFirst(8)) }
                message = "Schlüssel ausgewählt"
            } catch { message = error.localizedDescription }
        }
    }
}
