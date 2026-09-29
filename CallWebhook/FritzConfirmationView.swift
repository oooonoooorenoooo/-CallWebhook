import SwiftUI

@MainActor
final class FritzConfirmationModel: ObservableObject {
    @Published var visible = false
    @Published var methods: [String] = []
    @Published var selection = "button"
    @Published var phoneCode = ""
    @Published var error = ""
    var id = ""
    var cancelled = false
    var backend = false

    func begin(id: String, methods: [String], phoneCode: String, backend: Bool) {
        guard self.id != id else { return }
        self.id = id
        self.methods = methods
        self.phoneCode = phoneCode
        self.backend = backend
        selection = methods.first ?? "button"
        error = ""
        cancelled = false
        visible = true
    }

    func finish() {
        visible = false
        id = ""
        methods = []
        phoneCode = ""
        error = ""
        cancelled = false
    }
}

struct FritzConfirmationView: View {
    @ObservedObject var model: FritzConfirmationModel
    let submit: (String) async throws -> Void
    @State private var code = ""
    @State private var submitting = false

    var body: some View {
        NavigationStack {
            Form {
                Section("Wie möchtest du bestätigen?") {
                    Picker("Bestätigungsweg", selection: $model.selection) {
                        if model.methods.contains("button") { Text("Taste an der FRITZ!Box").tag("button") }
                        if model.methods.contains("phone") { Text("Verbundenes Festnetztelefon").tag("phone") }
                        if model.methods.contains("otp") { Text("Authenticator-Code").tag("otp") }
                    }
                    .pickerStyle(.inline)
                }
                Section {
                    switch model.selection {
                    case "phone":
                        Text("Diesen Code an einem bereits mit der FRITZ!Box verbundenen DECT- oder kabelgebundenen Telefon wählen:")
                        Text(model.phoneCode).font(.title.monospaced()).textSelection(.enabled)
                        Text("Den Bestätigungston abwarten und auflegen. Die App erkennt die Bestätigung automatisch.")
                    case "otp":
                        SecureField("6-stelliger Code", text: $code)
                            .keyboardType(.numberPad)
                            .textContentType(.oneTimeCode)
                            .onChange(of: code) { _, value in
                                code = String(value.filter { "0123456789".contains($0) }.prefix(6))
                            }
                        Button(submitting ? "Wird geprüft …" : "Code bestätigen") {
                            let enteredCode = code
                            code = ""
                            submitting = true
                            Task {
                                defer { submitting = false }
                                do { try await submit(enteredCode) }
                                catch { model.error = error.localizedDescription }
                            }
                        }
                        .disabled(code.count != 6 || submitting)
                    default:
                        Text("Eine Taste an der FRITZ!Box kurz drücken. Die App wartet auf die Bestätigung der Box und setzt danach automatisch fort.")
                    }
                    if !model.error.isEmpty { Text(model.error).foregroundStyle(.red) }
                    ProgressView("Warte auf Bestätigung …")
                }
                Section {
                    Text(model.backend
                        ? "Angezeigt werden die von der FRITZ!Box für diesen Benutzer angebotenen Wege. Authenticator nur, wenn eingerichtet und für diesen Auftrag verfügbar."
                        : "Für diese SIP-Änderung bietet die TR-064-Schnittstelle Taste und/oder Telefoncode an. Authenticator-Codes werden über diese Schnittstelle nicht unterstützt.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            .navigationTitle("FRITZ!Box bestätigen")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Abbrechen") { code = ""; model.cancelled = true; model.visible = false }
                }
            }
        }
        .interactiveDismissDisabled()
    }
}
