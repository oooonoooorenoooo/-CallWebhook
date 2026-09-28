import SwiftUI
import UIKit
import LiveCommunicationKit

struct DefaultPhoneAppsView: View {
    let onContinue: () -> Void
    @Environment(\.scenePhase) private var scenePhase
    @State private var callingConfirmed = false
    @State private var dialingConfirmed = false
    @State private var availableSIMs = 0
    @State private var settingsError: String?
    @State private var emergencyError: String?

    var body: some View {
        NavigationStack {
            Form {
                Section("Vor der Einrichtung") {
                    Text("Wähle CallWebhook unter Standard-Apps für Anrufe und Wählen.")
                    Text("iOS gibt diese beiden Einstellungen nicht zur automatischen Prüfung frei. Bitte kontrolliere sie in den Einstellungen und bestätige sie hier.")
                        .font(.caption).foregroundStyle(.secondary)
                    Button("Standard-Apps öffnen") {
                        guard let url = URL(string: UIApplication.openDefaultApplicationsSettingsURLString) else { return }
                        UIApplication.shared.open(url) { opened in
                            if !opened { settingsError = "Bitte Einstellungen → Apps → Standard-Apps öffnen." }
                        }
                    }
                    Toggle("Anrufe: CallWebhook – von mir geprüft", isOn: $callingConfirmed)
                    Toggle("Wählen: CallWebhook – von mir geprüft", isOn: $dialingConfirmed)
                    if let settingsError { Text(settingsError).foregroundStyle(.red) }
                }
                Section("Mobilfunkzugriff") {
                    Text(availableSIMs > 0 ? "\(availableSIMs) SIM-Leitung(en) verfügbar" : "Noch keine SIM-Leitungen für CallWebhook verfügbar")
                    Text("Der SIM-Zugriff ersetzt keine Prüfung der Standard-Apps. Bei Dual-SIM ordnest du die SIM später der passenden Handynummer zu.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Button("Weiter") { onContinue() }
                    .disabled(!callingConfirmed || !dialingConfirmed)
                Button("Später einrichten") { onContinue() }
                Section("Notruf") {
                    Button("112 über Mobilfunk wählen") {
                        SystemCellularDialer.call("112") { opened in
                            if !opened { emergencyError = "Bitte die Notruffunktion des iPhones verwenden." }
                        }
                    }
                    if let emergencyError { Text(emergencyError).foregroundStyle(.red) }
                }
            }
            .navigationTitle("Standard-Apps")
        }
        .task { refresh() }
        .onChange(of: scenePhase) { _, phase in if phase == .active { refresh() } }
    }

    private func refresh() {
        availableSIMs = TelephonyConversationManager.sharedInstance.cellularServices.count
    }
}

private struct MobileProviderPicker: View {
    @Binding var selection: String
    @Environment(\.dismiss) private var dismiss
    @State private var query = ""
    @State private var custom = ""

    var body: some View {
        List {
            Section("Anbieter und Marken") {
                ForEach(MobileProvider.matching(query)) { provider in
                    Button {
                        selection = provider.name
                        dismiss()
                    } label: {
                        HStack {
                            Text(provider.name)
                            Spacer()
                            if selection == provider.name { Image(systemName: "checkmark") }
                        }
                    }
                }
            }
            Section("Weiterer Anbieter") {
                TextField("Anbietername", text: $custom)
                Button("Diesen Anbieter verwenden") {
                    selection = custom.trimmingCharacters(in: .whitespacesAndNewlines)
                    dismiss()
                }.disabled(custom.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .searchable(text: $query, prompt: "Mobilfunkanbieter suchen")
        .navigationTitle("Mobilfunkanbieter")
    }
}

struct MobileForwardingView: View {
    let line: Int
    @Binding var mobile: String
    @Binding var provider: String
    let destination: String
    @AppStorage("setupAreaCode") private var areaCode = ""
    private var fullDestination: String { MobileForwarding.destination(number: destination, areaCode: areaCode) }
    @Binding var serviceID: String
    @Binding var confirmedConfiguration: String
    @State private var services: [CellularService] = []
    @State private var status = ""
    @State private var sending = false
    @State private var showConfirmation = false
    @Environment(\.scenePhase) private var scenePhase

    private var code: String? { MobileForwarding.activationCode(mobile: mobile, destination: fullDestination, provider: provider) }
    private var configuration: String { [mobile, provider, fullDestination, serviceID].joined(separator: "|") }
    private var selectedService: CellularService? { services.first { $0.id.uuidString == serviceID } }

    var body: some View {
        Group {
            TextField("Handynummer dieser Leitung", text: $mobile).keyboardType(.phonePad)
            NavigationLink {
                MobileProviderPicker(selection: $provider)
            } label: {
                LabeledContent("Mobilfunkanbieter", value: provider.isEmpty ? "Bitte auswählen" : provider)
            }
            Picker("SIM für \(mobile.isEmpty ? "diese Leitung" : mobile)", selection: $serviceID) {
                Text("Bitte zuordnen").tag("")
                ForEach(services) { service in
                    Text(service.label).tag(service.id.uuidString)
                }
            }
            Button("SIM-Leitungen neu prüfen") { refreshServices() }
            if services.isEmpty {
                Text("Keine SIM verfügbar. Standard-Apps prüfen oder den Code auf dem Handy der angegebenen Rufnummer wählen.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if let code {
                LabeledContent("Rufumleitung", value: "\(mobile) → \(fullDestination)")
                Text(code).font(.system(.body, design: .monospaced)).textSelection(.enabled)
                Button("Rufumleitung über Mobilfunk aktivieren") { showConfirmation = true }
                    .disabled(sending || selectedService == nil)
                Button("Steuercode kopieren") {
                    UIPasteboard.general.string = code
                    status = "Code kopiert. Auf der SIM von \(mobile) wählen und die Netzbestätigung abwarten."
                }
                Toggle("Aktivierung vom Mobilfunknetz bestätigt", isOn: Binding(
                    get: { confirmedConfiguration == configuration },
                    set: { confirmedConfiguration = $0 ? configuration : "" }
                ))
                Text("Die gewählte SIM muss zu \(mobile) gehören. Alle Anrufe werden sofort an \(fullDestination) weitergeleitet. Freigabe und Kosten hängen vom Tarif ab; manche Prepaid-Tarife erlauben nur die Mailbox.")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                Text("Anbieter, deutsche Handynummer und vollständige Festnetznummer einschließlich Vorwahl auswählen bzw. eintragen.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if !status.isEmpty { Text(status).font(.caption).foregroundStyle(.secondary) }
        }
        .task { refreshServices() }
        .onChange(of: scenePhase) { _, phase in if phase == .active { refreshServices() } }
        .confirmationDialog("Rufumleitung für Leitung \(line) aktivieren?", isPresented: $showConfirmation, titleVisibility: .visible) {
            Button("Mit SIM „\(selectedService?.label ?? "")“ wählen") { Task { await activate() } }
            Button("Abbrechen", role: .cancel) {}
        } message: {
            Text("\(mobile) → \(fullDestination)\n\(code ?? "")\nDie ausgewählte SIM muss die Handynummer dieser Leitung sein.")
        }
    }

    private func refreshServices() {
        services = TelephonyConversationManager.sharedInstance.cellularServices
        // Never guess the SIM from array order, carrier brand, or phone prefix.
    }

    @MainActor
    private func activate() async {
        guard let code, let service = selectedService, !sending else { return }
        sending = true
        defer { sending = false }
        confirmedConfiguration = ""
        do {
            let action = StartCellularConversationAction(Handle(type: .phoneNumber, value: code), cellularService: service)
            try await TelephonyConversationManager.sharedInstance.startCellularConversation(action)
            status = "Steuercode an iOS übergeben. Erst nach der Bestätigung des Mobilfunknetzes oben als aktiviert markieren."
        } catch {
            status = "iOS konnte den Steuercode nicht wählen: \(error.localizedDescription). Code kopieren und in der Telefon-App mit der richtigen SIM wählen."
        }
    }
}
