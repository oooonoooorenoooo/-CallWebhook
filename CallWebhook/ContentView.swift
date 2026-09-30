import SwiftUI
import UIKit
import Contacts
import ContactsUI
import LiveCommunicationKit
import AVKit
import Security
import Intents

private final class FritzAuthDelegate: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    let username: String
    let password: String

    init(username: String, password: String) {
        self.username = username
        self.password = password
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        let method = challenge.protectionSpace.authenticationMethod
        if (method == NSURLAuthenticationMethodHTTPDigest || method == NSURLAuthenticationMethodHTTPBasic),
           challenge.previousFailureCount == 0 {
            completionHandler(.useCredential, URLCredential(user: username, password: password, persistence: .forSession))
        } else {
            completionHandler(.performDefaultHandling, nil)
        }
    }
}

enum SetupKeychain {
    private static let service = "de.reno.CallWebhook.setup"

    static func set(_ value: String, account: String) throws {
        guard let data = value.data(using: .utf8) else {
            throw NSError(domain: "CallWebhook.Keychain", code: -1)
        }
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account
        ]
        SecItemDelete(query as CFDictionary)
        var item = query
        item[kSecValueData as String] = data
        item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let status = SecItemAdd(item as CFDictionary, nil)
        guard status == errSecSuccess else {
            throw NSError(domain: NSOSStatusErrorDomain, code: Int(status))
        }
    }

    static func get(account: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne
        ]
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }
    static func delete(account: String) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account
        ]
        SecItemDelete(query as CFDictionary)
    }

}

struct ContentView: View {
    @AppStorage("setupCompleted") private var setupCompleted = false
    @AppStorage("primaryPhoneNumber") private var primaryPhoneNumber = ""
    @AppStorage("secondaryPhoneNumber") private var secondaryPhoneNumber = ""
    @AppStorage("sipLine2Enabled") private var sipLine2Enabled = false
    @AppStorage("sipLine3Enabled") private var sipLine3Enabled = false
    @EnvironmentObject var monitor: CallMonitor
    @StateObject private var dialer = DialerModel()
    @ObservedObject private var callSIP = SIPService.shared
    @State private var selectedTab = 0
    @AppStorage("phoneDefaultsReviewed") private var phoneDefaultsReviewed = false
    @State private var showNetworkCode = false
    @State private var revisitingSetup = false

    var body: some View {
        Group {
            // A completed installation always starts in the normal app, including
            // upgrades from builds that did not persist the default-app review.
            if revisitingSetup {
                SetupWizardView(
                    onFinished: { revisitingSetup = false },
                    onCancel: { revisitingSetup = false }
                )
            } else if setupCompleted {
                mainTabs
            } else if !phoneDefaultsReviewed {
                DefaultPhoneAppsView { phoneDefaultsReviewed = true }
            } else {
                SetupWizardView {
                    setupCompleted = true
                }
            }
        }
        .sheet(isPresented: $showNetworkCode) {
            NavigationStack {
                Form {
                    Text(dialer.number).font(.system(.title3, design: .monospaced)).textSelection(.enabled)
                    Text(CellularRouting.cellularOnlyNumber(dialer.number) != nil
                        ? "Diese Nummer wird über die Mobilfunktelefonie des iPhones gewählt. Home Assistant und Asterisk werden dafür nicht benötigt."
                        : "Mobilfunk-Steuercode: Die SIM muss zur Handynummer der einzurichtenden Leitung gehören. Bitte die SIM im Systemdialog prüfen.")
                    Button("Über Mobilfunk wählen") { dialer.call(dialer.number) }
                    Button("Code kopieren") { UIPasteboard.general.string = dialer.number }
                    Text(dialer.status).font(.caption)
                }
                .navigationTitle(CellularRouting.cellularOnlyNumber(dialer.number) != nil ? "Mobilfunkanruf" : "Mobilfunk-Steuercode")
                .toolbar { Button("Zurück") { showNetworkCode = false } }
            }
        }
        .onOpenURL { url in
            guard url.scheme?.lowercased() == "tel" else { return }
            let raw = String(url.absoluteString.dropFirst(4))
            showIncomingNumber(raw.removingPercentEncoding ?? raw)
        }
        .onContinueUserActivity("INStartCallIntent") { activity in
            if let intent = activity.interaction?.intent as? INStartCallIntent,
               let number = intent.contacts?.first?.personHandle?.value {
                showIncomingNumber(number)
            }
        }
        .onContinueUserActivity("INStartAudioCallIntent") { activity in
            if let intent = activity.interaction?.intent as? INStartAudioCallIntent,
               let number = intent.contacts?.first?.personHandle?.value {
                showIncomingNumber(number)
            }
        }
    }

    private func showIncomingNumber(_ value: String) {
        let number = CellularRouting.cellularOnlyNumber(value) ?? value.filter { "+*#0123456789,;".contains($0) }
        guard !number.isEmpty else { return }
        dialer.number = number
        if CellularRouting.cellularOnlyNumber(number) != nil || MobileForwarding.isNetworkCode(number) { showNetworkCode = true }
        selectedTab = 3
    }

    private var mainTabs: some View {
        TabView(selection: $selectedTab) {
            ContactsView(dialer: dialer)
                .tabItem { Label("Kontakte", systemImage: "person.crop.circle.fill") }
                .tag(0)

            CallsView(dialer: dialer)
                .tabItem { Label("Anrufe", systemImage: "clock.fill") }
                .tag(1)

            MailboxView(dialer: dialer)
                .tabItem { Label("Mailbox", systemImage: "recordingtape") }
                .tag(2)

            DialPadView(dialer: dialer, primaryPhoneNumber: primaryPhoneNumber, secondaryPhoneNumber: secondaryPhoneNumber, sipLine2Enabled: sipLine2Enabled, sipLine3Enabled: sipLine3Enabled)
                .environmentObject(monitor)
                .tabItem { Label("Zifferblatt", systemImage: "circle.grid.3x3.fill") }
                .tag(3)

            ExtrasView(onRestartSetup: { revisitingSetup = true })
                .tabItem { Label("Extras", systemImage: "ellipsis.circle.fill") }
                .tag(4)
        }
        .tint(.blue)
        .onChange(of: dialer.isDialing) { _, dialing in
            if dialing { selectedTab = 3 }
        }
        .onChange(of: callSIP.incoming) { _, incoming in
            if incoming { selectedTab = 3 }
        }
    }

}


private struct SetupWizardView: View {
    let onFinished: () -> Void
    var onCancel: (() -> Void)? = nil

    @StateObject private var fritzConfirmation = FritzConfirmationModel()
    @State private var step = 0
    @State private var functionTestRunning = false
    @ObservedObject private var setupPush = VoIPPushService.shared
    @State private var operatorPushWorking = false
    @State private var functionTestResults: [String] = []
    @State private var mailboxNumbersVerified = false
    @State private var lineAssignmentStatus = ""
    @State private var fritzNumberAssignments: [String: String] = [:]
    @State private var fritzHost = UserDefaults.standard.string(forKey: "setupFritzHost") ?? "192.168.178.1"
    @State private var fritzUser = UserDefaults.standard.string(forKey: "setupFritzUser") ?? ""
    @State private var fritzPassword = ""
    @State private var fritzUserChoice = true
    @State private var isPreparingDevices = false
    @State private var automationRunning = false
    @State private var automationFailed = false
    @State private var confirmedAssignments: [Int: String] = [:]
    @State private var homeAssistantURL = UserDefaults.standard.string(forKey: "setupHomeAssistantURL") ?? "26"
    @State private var easybellEnabled = UserDefaults.standard.bool(forKey: "setupEasybellEnabled")
    @AppStorage("primaryPhoneNumber") private var primaryMobileNumber = ""
    @AppStorage("secondaryPhoneNumber") private var secondaryMobileNumber = ""
    @AppStorage("setupAreaCode") private var setupAreaCode = ""
    @AppStorage("line1MobileProvider") private var line1MobileProvider = ""
    @AppStorage("line2MobileProvider") private var line2MobileProvider = ""
    @AppStorage("line1CellularServiceID") private var line1CellularServiceID = ""
    @AppStorage("line2CellularServiceID") private var line2CellularServiceID = ""
    @AppStorage("line1ForwardingConfirmed") private var line1ForwardingConfirmed = ""
    @AppStorage("line2ForwardingConfirmed") private var line2ForwardingConfirmed = ""
    @State private var callHelperReady = false
    @State private var isCreatingCallHelper = false
    @State private var callHelperStatus = "Anrufstatus-Schalter wird im letzten HA-Schritt angelegt"
    @State private var line1Label = UserDefaults.standard.string(forKey: "sipLine1Label") ?? "Mobil 1"
    @State private var line2Label = UserDefaults.standard.string(forKey: "sipLine2Label") ?? "Mobil 2"
    @State private var line3Label = UserDefaults.standard.string(forKey: "sipLine3Label") ?? "Festnetz"
    @State private var line1Number = UserDefaults.standard.string(forKey: "sipLine1Number") ?? ""
    @State private var line2Number = UserDefaults.standard.string(forKey: "sipLine2Number") ?? ""
    @State private var line3Number = UserDefaults.standard.string(forKey: "sipLine3Number") ?? ""
    @State private var line3ManualNumber = false
    @State private var fritzReachable = false
    @State private var fritzStatus = "Noch nicht geprüft"
    @State private var fritzVoIPAvailable = false
    @State private var fritzTAMAvailable = false
    @State private var fritzServiceCount = 0
    @State private var fritzAuthenticated = false
    @State private var fritzVoIPNumbers: [String] = []
    @State private var fritzTAMCount = 0
    @State private var isProvisioningTAM = false
    @State private var tamProvisionStatus = "Anrufbeantworter noch nicht eingerichtet"
    @State private var fritzTAMs: [FritzTAM] = []
    @State private var isSavingMailboxes = false
    @State private var mailboxSaveError: String?
    @State private var mailbox1TAM = UserDefaults.standard.object(forKey: "setupMailbox1TAM") as? Int ?? -1
    @State private var mailbox2TAM = UserDefaults.standard.object(forKey: "setupMailbox2TAM") as? Int ?? -1
    @State private var mailbox3TAM = UserDefaults.standard.object(forKey: "setupMailbox3TAM") as? Int ?? -1
    @State private var fritzSIPClients: [FritzSIPClient] = []
    @State private var sipClient1Plan = "Noch nicht geprüft"
    @State private var sipClient2Plan = "Noch nicht geprüft"
    @State private var sipClient1Index: Int?
    @State private var sipClient2Index: Int?
    @State private var sipClient3Plan = "Noch nicht geprüft"
    @State private var sipClient3Index: Int?
    @State private var fritzSIPWriteAction = ""
    @State private var fritzSIPWriteStatus = "Schreibschnittstelle noch nicht geprüft"
    @State private var fritzSIPWriteArguments: [String] = []
    @State private var sipProvisionStatus = "Vorhandene SIP-Nebenstellen werden beim FRITZ!Box-Test geprüft"
    @State private var isProvisioningSIP = false
    @State private var homeAssistantReachable = false
    @State private var homeAssistantStatus = "Noch nicht geprüft"
    @State private var callWebhookHAReady = false
    @State private var callWebhookHAStatus = "CallWebhook-Integration noch nicht geprüft"
    @State private var asteriskConfigStatus = "Noch nicht vorbereitet"
    @State private var asteriskConfigReady = false
    @State private var asteriskInstalled = false
    @State private var asteriskInstallFailed = false
    @State private var isInstallingAsterisk = false
    @State private var isContinuingHASetup = false
    @State private var bootstrapProgressStep = 0
    private let bootstrapProgressTotal = 5
    @State private var asteriskProgressStep = 0
    private let asteriskProgressTotal = 7
    @State private var setupHAToken = ""
    @State private var isAuthenticatingHA = false
    @State private var isBootstrappingHA = false
    @State private var bootstrapInstallationNeeded = false
    @State private var bootstrapAttemptFailed = false
    @ScaledMetric(relativeTo: .body) private var setupStatusIconSize: CGFloat = 18
    @State private var bootstrapRestartPending = false
    @State private var isCheckingBackend = false
    @State private var bootstrapPreviousBootID: String?
    @State private var isWaitingForHARestart = false
    @State private var haAuthenticated = false
    @State private var isChecking = false
    @State private var easybellUsername = ""
    @State private var easybellPassword = ""
    @State private var easybellContactUser = ""
    @ObservedObject private var setupSIP = SIPService.shared
    @AppStorage("sipLine2Enabled") private var sipLine2Enabled = false
    @AppStorage("sipLine3Enabled") private var sipLine3Enabled = false
    @AppStorage("sipLine2Prefix") private var sipLine2Prefix = ""
    @AppStorage("sipLine3Prefix") private var sipLine3Prefix = ""

    private struct FritzTAM: Identifiable, Hashable {
        let index: Int
        let name: String
        let enabled: Bool

        var id: Int { index }
        var displayName: String {
            "\(name.isEmpty ? "Anrufbeantworter \(index + 1)" : name)\(enabled ? "" : " (aus)")"
        }
    }

    // One continuous form. The stage reveals sections; it never replaces the page.
    private let titles = ["FRITZ!Box", "Home Assistant", "Telefoniegeräte",
                          "Leitungszuordnung", "Automatische Einrichtung", "Prüfung"]

    private var setupBusy: Bool {
        isChecking || isAuthenticatingHA || isPreparingDevices || automationRunning
            || isSavingMailboxes || isProvisioningSIP || isProvisioningTAM
            || isBootstrappingHA || isWaitingForHARestart || isContinuingHASetup
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                ProgressView(value: Double(step + 1), total: Double(titles.count))
                    .tint(.blue).padding()
                ScrollViewReader { proxy in
                    Form {
                        fritz.id(0)
                        if step >= 1 { homeAssistantLogin.id(1) }
                        if step >= 2 { devices.id(2) }
                        if step >= 3 {
                            Text("Leitungszuordnung").font(.headline).id(3)
                            lines.disabled(step != 3 || setupBusy)
                        }
                        if step >= 4 {
                            Text("Automatische Einrichtung").font(.headline).id(4)
                            homeAssistant
                        }
                        if let mailboxSaveError {
                            Section { Text(mailboxSaveError).foregroundStyle(.red) }
                        }
                        if isSavingMailboxes {
                            Section {
                                ProgressView("Rufnummernzuordnung wird übertragen …")
                                Text(lineAssignmentStatus)
                            }
                        }
                        if step == 5 {
                            Text("Prüfung").font(.headline).id(5)
                            verification
                            Section {
                                Button("Einrichtung abschließen") {
                                    persistSetup()
                                    onFinished()
                                }
                                .buttonStyle(.borderedProminent)
                                .disabled(!canContinue || setupBusy || functionTestRunning
                                    || setupPush.settingUp || operatorPushWorking)
                            }
                        }
                    }
                    .onChange(of: step) { _, value in
                        withAnimation { proxy.scrollTo(value, anchor: .top) }
                    }
                    .onChange(of: confirmedAssignments) { _, _ in
                        guard step == 3 else { return }
                        if let next = activeLines.first(where: {
                            confirmedAssignments[$0] != assignmentFingerprint(line: $0)
                        }) {
                            withAnimation { proxy.scrollTo(100 + next, anchor: .top) }
                        }
                    }
                }
            }
            .toolbar {
                if let onCancel {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Schließen", action: onCancel)
                            .disabled(setupBusy || functionTestRunning || setupPush.settingUp || operatorPushWorking)
                    }
                }
            }
            .navigationTitle("CallWebhook einrichten")
            .navigationBarTitleDisplayMode(.inline)
        }
        .onChange(of: readyForAutomation) { _, ready in
            if ready { Task { await startAutomaticSetup() } }
        }
        .sheet(isPresented: $fritzConfirmation.visible) {
            FritzConfirmationView(model: fritzConfirmation, submit: submitFritzConfirmationCode)
        }
    }

    @MainActor
    private func submitFritzConfirmationCode(_ code: String) async throws {
        guard fritzConfirmation.backend, let base = HomeAssistantConnection.configuredBase else {
            throw URLError(.badURL)
        }
        let (data, status) = try await HomeAssistantConnection.request(base: base,
            path: "api/callwebhook/setup/mailboxes", method: "POST",
            body: ["confirmation": ["id": fritzConfirmation.id, "action": "otp", "code": code]])
        guard status == 200 else {
            let result = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            throw NSError(domain: "CallWebhook.2FA", code: status,
                userInfo: [NSLocalizedDescriptionKey: result?["error"] as? String ?? "Code konnte nicht übermittelt werden"])
        }
        fritzConfirmation.error = ""
    }

    private var fritz: some View {
        Section("FRITZ!Box") {
            Toggle("FRITZ!Box-Benutzer vorhanden", isOn: $fritzUserChoice)
                .disabled(step > 0 || setupBusy)
            if !fritzUserChoice {
                Text("Unter System → FRITZ!Box-Benutzer einen Benutzer anlegen und „FRITZ!Box Einstellungen“ erlauben. Anschließend die Zugangsdaten hier eintragen.")
                Button("FRITZ!Box öffnen") {
                    if let url = URL(string: "http://\(fritzHost)") { UIApplication.shared.open(url) }
                }
            }
            HStack(alignment: .top, spacing: 8) {
                TextField("IP-Adresse", text: $fritzHost)
                    .keyboardType(.decimalPad)
                    .accessibilityLabel("FRITZ!Box IP-Adresse")
                TextField("Benutzername", text: $fritzUser)
                    .textContentType(.username)
                    .accessibilityLabel("FRITZ!Box Benutzername")
                SecureField("Passwort", text: $fritzPassword)
                    .textContentType(.password)
                    .accessibilityLabel("FRITZ!Box Passwort")
            }
            .textInputAutocapitalization(.never)
            .autocorrectionDisabled()
            .disabled(step > 0 || setupBusy)
            if step == 0 {
                Button("Verbinden") {
                    Task {
                        await checkFritzBox()
                        if fritzAuthenticated && fritzVoIPAvailable { step = 1 }
                    }
                }.disabled(setupBusy || fritzHost.isEmpty || fritzUser.isEmpty || fritzPassword.isEmpty)
                if isChecking { ProgressView("FRITZ!Box wird geprüft …") }
            }
            if fritzAuthenticated {
                foundRow("FRITZ!Box verbunden")
                if fritzVoIPAvailable { foundRow("Telefonie verfügbar") }
                ForEach(fritzVoIPNumbers, id: \.self) { number in
                    foundRow("Rufnummer \(number)")
                }
            } else if fritzStatus != "Noch nicht geprüft" {
                Text(fritzStatus).foregroundStyle(.secondary)
            }
        }
    }

    private func foundRow(_ title: String) -> some View {
        Label(title, systemImage: "checkmark.circle.fill")
            .font(.body).foregroundStyle(.green)
    }

    private var homeAssistantLogin: some View {
        Section("Home Assistant anmelden") {
            HStack(spacing: 0) {
                Text("192.168.178.").foregroundStyle(.secondary)
                TextField("26", text: $homeAssistantURL)
                    .keyboardType(.numberPad)
                    .onChange(of: homeAssistantURL) { _, value in
                        let digits = String(value.filter(\.isNumber).prefix(3))
                        if digits != value { homeAssistantURL = digits }
                    }
            }.disabled(step > 1 || setupBusy)
            if step == 1 {
                Button("Mit Home Assistant verbinden") {
                    Task {
                        await checkHomeAssistant()
                        if homeAssistantReachable && !haAuthenticated {
                            await authenticateHomeAssistant()
                        }
                    }
                }.disabled(setupBusy || homeAssistantURL.isEmpty)
            }
            if haAuthenticated {
                foundRow("Home Assistant verbunden")
            } else {
                Text(homeAssistantStatus).foregroundStyle(.secondary)
            }
            if isAuthenticatingHA || (step == 1 && isChecking) {
                ProgressView("Anmeldung wird geprüft …")
            }
        }
    }

    private var devices: some View {
        Section("Telefoniegeräte und Anrufbeantworter") {
            Picker("Wie viele Geräte möchtest du einrichten?", selection: Binding(
                get: { sipLine3Enabled ? 3 : (sipLine2Enabled ? 2 : 1) },
                set: { count in
                    sipLine2Enabled = count >= 2
                    sipLine3Enabled = count >= 3
                })) {
                Text("1 · SIM 1").tag(1)
                Text("2 · SIM 1 und SIM 2").tag(2)
                Text("3 · SIM 1, SIM 2 und Festnetz").tag(3)
            }.disabled(step != 2 || setupBusy)
            if step == 2 {
                Button(isPreparingDevices ? "Wird eingerichtet …" : "Fertig") {
                    Task { await prepareDevices() }
                }.disabled(setupBusy)
            }
            if isPreparingDevices {
                ProgressView(isProvisioningTAM ? tamProvisionStatus : "Telefoniegeräte werden geprüft und eingerichtet …")
            }
            ForEach(fritzSIPClients.filter { client in
                (1...(sipLine3Enabled ? 3 : (sipLine2Enabled ? 2 : 1))).contains { index in
                    client.username == "callwhapp\(index)" || client.phoneName == "callwhapp\(index)"
                }
            }) { client in foundRow(client.displayName) }
            ForEach(fritzTAMs) { tam in foundRow(tam.displayName) }
            if step == 2 && !isPreparingDevices {
                if !fritzAuthenticated { Text(fritzStatus).foregroundStyle(.red) }
                else if !fritzSIPVerified || !deviceCredentialsReady {
                    Text(sipProvisionStatus).foregroundStyle(.red)
                }
            }
        }
    }

    @MainActor
    private func prepareDevices() async {
        guard step == 2, haAuthenticated, !isPreparingDevices else { return }
        isPreparingDevices = true
        mailboxSaveError = nil
        defer { isPreparingDevices = false }
        await checkFritzBox()
        guard fritzAuthenticated && fritzVoIPAvailable else { return }
        await provisionMissingSIPClients()
        guard fritzSIPVerified,
              deviceCredentialsReady else { return }
        guard await provisionMissingTAMs() else { return }
        confirmedAssignments = [:]
        step = 3
    }

    private var homeAssistant: some View {
        Group {
            Section("Automatische Einrichtung") {
                HStack(spacing: 8) {
                    ZStack {
                        ForEach(0..<bootstrapProgressTotal, id: \.self) { index in
                            Circle()
                                .trim(from: CGFloat(index) / CGFloat(bootstrapProgressTotal) + 0.025,
                                      to: CGFloat(index + 1) / CGFloat(bootstrapProgressTotal) - 0.025)
                                .stroke(index < bootstrapProgressStep ? Color.green : Color.secondary.opacity(0.22),
                                        style: StrokeStyle(lineWidth: 2, lineCap: .round))
                                .rotationEffect(.degrees(-90))
                        }
                        if callWebhookHAReady && !bootstrapRestartPending {
                            Image(systemName: "checkmark")
                                .font(.system(size: setupStatusIconSize * 0.48, weight: .bold))
                                .foregroundStyle(.green)
                        }
                    }
                    .padding(1)
                    .frame(width: setupStatusIconSize, height: setupStatusIconSize)
                    Text(bootstrapRestartPending ? "Home Assistant startet neu … Warte auf vollständige Bereitschaft." : callWebhookHAStatus)
                        .foregroundStyle(callWebhookHAReady && !bootstrapRestartPending ? .green : .secondary)
                }
                Label(
                    asteriskInstalled ? "Asterisk automatisch eingerichtet" : (isInstallingAsterisk ? "Asterisk wird automatisch eingerichtet …" : (asteriskInstallFailed ? "Asterisk-Einrichtung fehlgeschlagen" : "Asterisk wird automatisch eingerichtet")),
                    systemImage: asteriskInstalled ? "checkmark.circle.fill" : (asteriskInstallFailed ? "xmark.circle.fill" : "arrow.trianglehead.2.clockwise.rotate.90")
                )
                .foregroundStyle(asteriskInstalled ? .green : (asteriskInstallFailed ? .red : .secondary))
                HStack(spacing: 8) {
                    ZStack {
                        ForEach(0..<asteriskProgressTotal, id: \.self) { index in
                            Circle()
                                .trim(from: CGFloat(index) / CGFloat(asteriskProgressTotal) + 0.025,
                                      to: CGFloat(index + 1) / CGFloat(asteriskProgressTotal) - 0.025)
                                .stroke(index < asteriskProgressStep ? Color.green : Color.secondary.opacity(0.22),
                                        style: StrokeStyle(lineWidth: 2, lineCap: .round))
                                .rotationEffect(.degrees(-90))
                        }
                        if asteriskInstalled {
                            Image(systemName: "checkmark")
                                .font(.system(size: setupStatusIconSize * 0.48, weight: .bold))
                                .foregroundStyle(.green)
                        } else if asteriskInstallFailed {
                            Image(systemName: "xmark")
                                .font(.system(size: setupStatusIconSize * 0.48, weight: .bold))
                                .foregroundStyle(.red)
                        }
                    }
                    .padding(1)
                    .frame(width: setupStatusIconSize, height: setupStatusIconSize)
                    Text(asteriskConfigStatus)
                        .foregroundStyle(asteriskInstallFailed ? .red : (asteriskInstalled ? .green : .secondary))
                }
                Label("Der Home-Assistant-Token wird ausschließlich sicher im iOS-Keychain gespeichert.", systemImage: "lock.shield")
                    .foregroundStyle(.secondary)
            }
            Section("Anrufstatus-Schalter") {
                setupCheck("Home-Assistant-Helfer", detail: callHelperStatus, ready: callHelperReady)
                if isCreatingCallHelper { ProgressView() }

            }
            if automationFailed {
                Section {
                    Button("Einrichtung erneut versuchen") {
                        Task { await startAutomaticSetup() }
                    }.disabled(setupBusy)
                }
            }
        }
    }

    private var lines: some View {
        Group {
            Section("Hinterlegte FRITZ!Box-Rufnummern") {
                Text("Leitungen zuordnen und jeweils bestätigen. Sind alle Zuordnungen und Mobilfunk-Rufumleitungen bestätigt, startet die weitere Einrichtung automatisch.")
                TextField("Ortsvorwahl, falls Rufnummern ohne Vorwahl", text: $setupAreaCode).keyboardType(.phonePad)
                ForEach(Array(fritzVoIPNumbers.enumerated()), id: \.element) { index, number in
                    foundRow("Rufnummer \(number)")
                }
                if fritzVoIPNumbers.isEmpty {
                    Text("Keine Rufnummern ausgelesen. Du kannst die tatsächliche Nummer manuell eintragen.")
                        .foregroundStyle(.secondary)
                }
            }
            Section("Rufnummer bei ausgehenden Anrufen") {
                Toggle("Eigene Mobilfunknummer anzeigen", isOn: $easybellEnabled)
                Text(easybellEnabled
                     ? "Dafür wird ein geeigneter easybell-Telefonie-/SIP-Tarif benötigt. CLIP no screening selbst ist bei easybell kostenlos."
                     : "Ohne diese Option wird kein easybell-Zugang für die Mobilfunknummer benötigt.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if easybellEnabled {
                    Text("Es darf nur eine Rufnummer übertragen werden, die dir zugeteilt ist bzw. deren Zuteilungsnehmer du bist.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    TextField("easybell SIP-Benutzername", text: $easybellUsername)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    SecureField("easybell SIP-Passwort", text: $easybellPassword)
                    TextField("Contact User / Stammrufnummer", text: $easybellContactUser)
                        .keyboardType(.phonePad)
                    Text("Registrar: voip.easybell.de. Das SIP-Kennwort wird ausschließlich sicher im iOS-Keychain gespeichert.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            Section("Leitung 1") {
                TextField("Bezeichnung", text: $line1Label)
                fritzNumberPicker("Absenderrufnummer", selection: $line1Number)
                fritzMailboxPicker(selection: $mailbox1TAM)
                MobileForwardingView(line: 1, mobile: $primaryMobileNumber, provider: $line1MobileProvider,
                    destination: line1Number, serviceID: $line1CellularServiceID, confirmedConfiguration: $line1ForwardingConfirmed)
                Text(easybellEnabled ? "easybell / CLIP no screening" : "FRITZ!Box")
                    .foregroundStyle(.secondary)
                assignmentConfirmation(line: 1)
            }.id(101)
            Section("Leitung 2") {
                Text(sipLine2Enabled ? "SIM 2" : "Nicht ausgewählt")
                if sipLine2Enabled {
                    TextField("Bezeichnung", text: $line2Label)
                    fritzNumberPicker("Absenderrufnummer", selection: $line2Number)
                    fritzMailboxPicker(selection: $mailbox2TAM)
                    MobileForwardingView(line: 2, mobile: $secondaryMobileNumber, provider: $line2MobileProvider,
                        destination: line2Number, serviceID: $line2CellularServiceID, confirmedConfiguration: $line2ForwardingConfirmed)
                    TextField("Asterisk-Präfix", text: $sipLine2Prefix)
                        .keyboardType(.numbersAndPunctuation)
                }
                if sipLine2Enabled { assignmentConfirmation(line: 2) }
            }.id(102)
            Section("Leitung 3") {
                Text(sipLine3Enabled ? "Festnetz" : "Nicht ausgewählt")
                if sipLine3Enabled {
                    TextField("Bezeichnung", text: $line3Label)
                    if line3ManualNumber || fritzVoIPNumbers.isEmpty {
                        TextField("FRITZ!-Festnetzrufnummer", text: $line3Number)
                            .keyboardType(.phonePad)
                        if !fritzVoIPNumbers.isEmpty {
                            Button("Erkannte Rufnummer auswählen") {
                                line3ManualNumber = false
                                if !fritzVoIPNumbers.contains(line3Number) {
                                    line3Number = fritzVoIPNumbers.first ?? ""
                                }
                            }
                        }
                    } else {
                        Picker("FRITZ!-Festnetzrufnummer", selection: $line3Number) {
                            Text("Bitte wählen").tag("")
                            ForEach(Array(fritzVoIPNumbers.enumerated()), id: \.element) { index, number in
                                Text("\(index + 1) · \(number)").tag(number)
                            }
                        }
                        Button("Andere Festnetzrufnummer eingeben") {
                            line3ManualNumber = true
                            line3Number = ""
                        }
                    }
                    fritzMailboxPicker(selection: $mailbox3TAM)
                    TextField("Asterisk-Präfix", text: $sipLine3Prefix)
                        .keyboardType(.numbersAndPunctuation)
                    Text("Reine Festnetzleitung – keine Mobilfunk-Rufumleitung. Direkter FRITZ!Box-Pfad ohne CLIP no screening.")
                        .foregroundStyle(.secondary)
                    assignmentConfirmation(line: 3)
                }
            }.id(103)

        }
        .onAppear {
            if sipLine2Prefix.isEmpty { sipLine2Prefix = "*82" }
            if sipLine3Prefix.isEmpty { sipLine3Prefix = "*83" }
        }
    }

    private var activeLines: [Int] {
        Array(1...(sipLine3Enabled ? 3 : (sipLine2Enabled ? 2 : 1)))
    }

    private var deviceCredentialsReady: Bool {
        activeLines.allSatisfy { SetupKeychain.get(account: "fritz-sip-callwhapp\($0)") != nil }
    }

    private func assignmentFingerprint(line: Int) -> String {
        let number = [line1Number, line2Number, line3Number][line - 1]
        let tam = [mailbox1TAM, mailbox2TAM, mailbox3TAM][line - 1]
        return SetupAssignments.fingerprint(number: number, mailbox: tam)
    }

    private func assignmentConfirmation(line: Int) -> some View {
        Toggle("Zuordnung bestätigt", isOn: Binding(
            get: { confirmedAssignments[line] == assignmentFingerprint(line: line) },
            set: { confirmed in
                confirmedAssignments[line] = confirmed ? assignmentFingerprint(line: line) : nil
            }))
        .disabled([line1Number, line2Number, line3Number][line - 1].isEmpty || !mailboxSelectionVerified)
    }

    private var readyForAutomation: Bool {
        step == 3 && canContinue && mailboxSelectionVerified
            && SetupAssignments.allConfirmed(
                numbers: Array([line1Number, line2Number, line3Number].prefix(activeLines.count)),
                mailboxes: Array([mailbox1TAM, mailbox2TAM, mailbox3TAM].prefix(activeLines.count)),
                confirmations: confirmedAssignments)
    }

    @MainActor
    private func startAutomaticSetup() async {
        guard !automationRunning, readyForAutomation || (step == 4 && automationFailed) else { return }
        automationRunning = true
        automationFailed = false
        mailboxSaveError = nil
        persistSetup(completed: false)
        step = 4
        defer {
            automationRunning = false
            automationFailed = step != 5
        }
        let input = homeAssistantURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let base = URL(string: "http://192.168.178.\(input):8123") else { return }
        await prepareConnectedHomeAssistant(base: base)
        // An installed backend may still be starting. Poll its real readiness,
        // without treating a transient network error as a missing installation.
        if !callWebhookHAReady && haAuthenticated && !bootstrapAttemptFailed {
            await waitForCallWebhookAfterRestart()
        }
    }

    @ViewBuilder
    private func fritzNumberPicker(_ title: String, selection: Binding<String>) -> some View {
        if fritzVoIPNumbers.isEmpty {
            TextField(title, text: selection)
                .keyboardType(.phonePad)
        } else {
            Picker(title, selection: selection) {
                Text("Bitte wählen").tag("")
                ForEach(Array(fritzVoIPNumbers.enumerated()), id: \.element) { index, number in
                    Text("\(index + 1) · \(number)").tag(number)
                }
            }
        }
    }

    private var missingFritzSIPClients: [String] {
        FritzSIPReadiness.missingClients(in: fritzSIPClients, secondLineEnabled: sipLine2Enabled, thirdLineEnabled: sipLine3Enabled)
    }

    private var fritzSIPVerified: Bool {
        fritzAuthenticated && missingFritzSIPClients.isEmpty
    }

    private var fritzSIPSummary: String {
        if fritzSIPVerified {
            return "Alle benötigten SIP-Nebenstellen sind vorhanden und werden verwendet. Keine Neuanlage erforderlich."
        }
        if !fritzAuthenticated { return "FRITZ!Box-Anmeldung noch nicht bestätigt" }
        return "Noch fehlend: " + missingFritzSIPClients.joined(separator: ", ")
    }

    @ViewBuilder
    private func fritzMailboxPicker(selection: Binding<Int>) -> some View {
        Picker("Anrufbeantworter", selection: selection) {
            if selection.wrappedValue == -2 {
                Text("Zuerst im FRITZ!Box-Schritt einrichten").tag(-2)
            }
            Text("Nicht verwenden").tag(-1)
            ForEach(fritzTAMs) { tam in
                Text("\(tam.index + 1) · \(tam.displayName)").tag(tam.index)
            }
            if selection.wrappedValue >= 0 && !fritzTAMs.contains(where: { $0.index == selection.wrappedValue }) {
                Text("Bisheriger Anrufbeantworter nicht verfügbar").tag(selection.wrappedValue)
            }
        }
        if fritzTAMs.isEmpty {
            Text("Noch kein aktiver Anrufbeantworter vorhanden. Bitte im FRITZ!Box-Schritt die fehlenden Anrufbeantworter einrichten.")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    private var mailboxSelectionVerified: Bool {
        let selected = [mailbox1TAM, sipLine2Enabled ? mailbox2TAM : -1, sipLine3Enabled ? mailbox3TAM : -1]
        return selected.allSatisfy { value in
            value == -1 || fritzTAMs.contains { $0.index == value }
        }
    }

    @MainActor
    private func saveMailboxSelection() async -> Bool {
        guard !isSavingMailboxes, mailboxSelectionVerified else { return false }
        isSavingMailboxes = true
        mailboxNumbersVerified = false
        functionTestResults = []
        mailboxSaveError = nil
        defer { isSavingMailboxes = false; fritzConfirmation.finish() }
        do {
            guard let ha = HomeAssistantConnection.configuredBase else { throw URLError(.badURL) }
            func backendVersion() async throws -> Int {
                let (data, status) = try await HomeAssistantConnection.request(base: ha, path: "api/callwebhook/setup/status")
                guard status == 200 else { return 0 }
                return (try JSONSerialization.jsonObject(with: data) as? [String: Any])?["api_version"] as? Int ?? 0
            }
            if try await backendVersion() < 18 {
                lineAssignmentStatus = "HA-Komponente für FRITZ!-Anrufbeantworter wird aktualisiert …"
                await setupPush.updateBackend()
            }
            guard try await backendVersion() >= 18 else {
                throw NSError(domain: "CallWebhook.TAM", code: 1, userInfo: [NSLocalizedDescriptionKey: "Die HA-Aktualisierung für das Schreiben der Anrufbeantworter ist noch nicht abgeschlossen."])
            }
            try await synchronizeFritzCredentials()
            try await synchronizeFritzLineNumbers()
            let input = homeAssistantURL.trimmingCharacters(in: .whitespacesAndNewlines)
            guard let base = URL(string: "http://192.168.178.\(input):8123"),
                  let url = URL(string: "/api/callwebhook/setup/mailboxes", relativeTo: base)?.absoluteURL,
                  var token = SetupKeychain.get(account: "home-assistant-token"), !token.isEmpty else {
                throw URLError(.userAuthenticationRequired)
            }
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.timeoutInterval = 15
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            let assignments = ["mailbox_tam_1": mailbox1TAM,
                               "mailbox_tam_2": sipLine2Enabled ? mailbox2TAM : -1,
                               "mailbox_tam_3": sipLine3Enabled ? mailbox3TAM : -1]
            request.httpBody = try JSONSerialization.data(withJSONObject: [
                "mailbox_tam_1": assignments["mailbox_tam_1"]!,
                "mailbox_tam_2": assignments["mailbox_tam_2"]!,
                "mailbox_tam_3": assignments["mailbox_tam_3"]!,
                "line_numbers": ["1": line1Number, "2": line2Number, "3": line3Number]
            ])
            var (data, response) = try await URLSession.shared.data(for: request)
            if (response as? HTTPURLResponse)?.statusCode == 401 {
                token = try await HomeAssistantAuth.shared.refresh(instance: base)
                setupHAToken = token
                request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
                (data, response) = try await URLSession.shared.data(for: request)
            }
            if (response as? HTTPURLResponse)?.statusCode == 202 {
                let deadline = Date().addingTimeInterval(600)
                var completed = false
                while Date() < deadline {
                    let (bytes, status) = try await HomeAssistantConnection.request(base: base,
                        path: "api/callwebhook/setup/mailboxes", timeout: 15)
                    guard status == 200, let state = try JSONSerialization.jsonObject(with: bytes) as? [String: Any] else {
                        throw URLError(.badServerResponse)
                    }
                    lineAssignmentStatus = state["message"] as? String ?? "FRITZ!-Anrufbeantworter werden gespeichert …"
                    if state["state"] as? String == "error" {
                        throw NSError(domain: "CallWebhook.TAM", code: 1, userInfo: [NSLocalizedDescriptionKey: lineAssignmentStatus])
                    }
                    if state["state"] as? String == "completed" { data = bytes; completed = true; break }
                    if fritzConfirmation.cancelled {
                        let (_, cancelStatus) = try await HomeAssistantConnection.request(base: base,
                            path: "api/callwebhook/setup/mailboxes", method: "POST",
                            body: ["confirmation": ["id": fritzConfirmation.id, "action": "cancel"]])
                        guard cancelStatus == 200 || cancelStatus == 409 else { throw URLError(.badServerResponse) }
                        throw NSError(domain: "CallWebhook.2FA", code: 1,
                            userInfo: [NSLocalizedDescriptionKey: "FRITZ!Box-Bestätigung abgebrochen"])
                    }
                    if let confirmation = state["confirmation"] as? [String: Any],
                       let id = confirmation["id"] as? String,
                       let methods = confirmation["methods"] as? [String], !methods.isEmpty {
                        fritzConfirmation.begin(id: id, methods: methods,
                            phoneCode: confirmation["phone_code"] as? String ?? "", backend: true)
                        fritzConfirmation.error = confirmation["error"] as? String ?? ""
                    } else if fritzConfirmation.backend && !fritzConfirmation.id.isEmpty {
                        fritzConfirmation.finish()
                    }
                    try await Task.sleep(for: .milliseconds(500))
                }
                guard completed else { throw URLError(.timedOut) }
            }
            let result = try JSONSerialization.jsonObject(with: data) as? [String: Any] ?? [:]
            guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode),
                  result["ok"] as? Bool == true, result["state"] as? String == "completed",
                  let confirmed = result["assignments"] as? [String: Int],
                  assignments.allSatisfy({ key, value in
                      guard let actual = confirmed[key] else { return false }
                      return value == -2 ? (0...4).contains(actual) : actual == value
                  }) else {
                throw NSError(domain: "CallWebhook.TAM", code: 1, userInfo: [NSLocalizedDescriptionKey:
                    result["error"] as? String ?? "FRITZ!-Anrufbeantworter-Zuordnung nicht bestätigt"])
            }
            mailbox1TAM = confirmed["mailbox_tam_1"] ?? -1
            mailbox2TAM = confirmed["mailbox_tam_2"] ?? -1
            mailbox3TAM = confirmed["mailbox_tam_3"] ?? -1
            for (key, value) in confirmed where value >= 0 && !fritzTAMs.contains(where: { $0.index == value }) {
                let line = key.suffix(1)
                fritzTAMs.append(FritzTAM(index: value, name: "CallWebhook Leitung \(line)", enabled: true))
            }
            fritzTAMCount = fritzTAMs.count
            mailboxNumbersVerified = true
            for line in 1...3 {
                UserDefaults.standard.set(confirmed["mailbox_tam_\(line)"], forKey: "setupMailbox\(line)TAM")
            }
            return true
        } catch {
            mailboxSaveError = "Leitungs-/Anrufbeantworter-Zuordnung nicht gespeichert: \(error.localizedDescription). Bitte die Einrichtung erneut versuchen."
            return false
        }
    }

    @MainActor
    private func synchronizeFritzLineNumbers() async throws {
        func failure(_ text: String) -> NSError {
            NSError(domain: "CallWebhook.FritzLines", code: 1, userInfo: [NSLocalizedDescriptionKey: text])
        }
        guard !setupSIP.active else { throw failure("Bitte zuerst das laufende Gespräch beenden.") }
        guard !fritzSIPWriteAction.isEmpty else { throw failure("FRITZ!Box-Schreibschnittstelle zuerst im FRITZ!Box-Schritt prüfen.") }
        let rawHost = fritzHost.trimmingCharacters(in: .whitespacesAndNewlines)
        let base = rawHost.contains("://") ? rawHost : "http://\(rawHost):49000"
        guard let url = URL(string: base + "/tr64desc.xml") else { throw URLError(.badURL) }
        let (data, _) = try await URLSession.shared.data(from: url)
        let description = String(decoding: data, as: UTF8.self)
        guard let service = extractTR064Services(from: description).first(where: { $0.type.contains("X_VoIP") }) else { throw URLError(.unsupportedURL) }
        let session = URLSession(configuration: .ephemeral, delegate: FritzAuthDelegate(username: fritzUser, password: fritzPassword), delegateQueue: nil)
        defer { session.finishTasksAndInvalidate() }
        var secondFactorToken: String?
        // The second base client is still used by the generated Asterisk config.
        var lines = [(1, line1Number)]
        if sipLine2Enabled { lines.append((2, line2Number)) }
        if sipLine3Enabled { lines.append((3, line3Number)) }
        for (line, number) in lines {
            let username = "callwhapp\(line)"
            guard let client = fritzSIPClients.first(where: { $0.username == username || $0.phoneName == username }),
                  let password = SetupKeychain.get(account: "fritz-sip-\(username)") else {
                throw failure("Nebenstelle \(username) oder ihre gespeicherten Zugangsdaten fehlen. Im FRITZ!Box-Schritt einrichten.")
            }
            let args = try setClientArguments(index: client.index, username: username, password: password, outgoing: number)
            let readArgs = [("NewX_AVM-DE_ClientIndex", String(client.index))]
            let before = try await soapCall(session: session, base: base, serviceType: service.type, controlURL: service.controlURL, action: "X_AVM-DE_GetClient3", arguments: readArgs)
            func matches(_ xml: String) -> Bool {
                FritzPhoneNumbers.clientMatches(number: number,
                    outgoing: extractSOAPValue("NewX_AVM-DE_OutGoingNumber", from: xml),
                    incoming: xml)
            }
            if matches(before) { continue }
            lineAssignmentStatus = "Leitung \(line): Rufnummer \(number) wird auf der FRITZ!Box gespeichert …"
            do {
                _ = try await soapCall(session: session, base: base, serviceType: service.type, controlURL: service.controlURL, action: fritzSIPWriteAction, arguments: args, secondFactorToken: secondFactorToken)
            } catch {
                guard isSecondFactorRequired(error) else { throw error }
                lineAssignmentStatus = "Bitte den Bestätigungsweg im FRITZ!Box-Dialog wählen …"
                secondFactorToken = try await beginFritzSecondFactor(session: session, base: base, descriptionXML: description)
                _ = try await soapCall(session: session, base: base, serviceType: service.type, controlURL: service.controlURL, action: fritzSIPWriteAction, arguments: args, secondFactorToken: secondFactorToken)
            }
            let verified = try await soapCall(session: session, base: base, serviceType: service.type, controlURL: service.controlURL, action: "X_AVM-DE_GetClient3", arguments: readArgs)
            guard matches(verified) else { throw failure("FRITZ!Box hat die Rufnummer für Leitung \(line) beim Zurücklesen nicht bestätigt.") }
        }
        lineAssignmentStatus = "FRITZ!Box-Rufnummern gespeichert und zurückgelesen"
    }

    private var forwardingReady: Bool {
        let target1 = MobileForwarding.destination(number: line1Number, areaCode: setupAreaCode)
        let target2 = MobileForwarding.destination(number: line2Number, areaCode: setupAreaCode)
        let first = MobileForwarding.activationCode(mobile: primaryMobileNumber, destination: target1, provider: line1MobileProvider) != nil
            && line1ForwardingConfirmed == [primaryMobileNumber, line1MobileProvider, target1, line1CellularServiceID].joined(separator: "|")
        let second = !sipLine2Enabled || (MobileForwarding.activationCode(mobile: secondaryMobileNumber, destination: target2, provider: line2MobileProvider) != nil
            && line2ForwardingConfirmed == [secondaryMobileNumber, line2MobileProvider, target2, line2CellularServiceID].joined(separator: "|"))
        return first && second
    }

    @MainActor
    private func ensureCallHelper() async {
        guard !isCreatingCallHelper else { return }
        isCreatingCallHelper = true
        callHelperReady = false
        callHelperStatus = "Anrufstatus-Schalter wird angelegt und geprüft …"
        defer { isCreatingCallHelper = false }
        do {
            let input = homeAssistantURL.trimmingCharacters(in: .whitespacesAndNewlines)
            guard let base = URL(string: "http://192.168.178.\(input):8123") else { throw URLError(.badURL) }
            let entity = try await HomeAssistantConnection.ensureCallHelper(base: base)
            UserDefaults.standard.set(homeAssistantURL, forKey: "setupHomeAssistantURL")
            callHelperReady = true
            callHelperStatus = "\(entity) ist bereit"
        } catch {
            callHelperStatus = "Schalter konnte nicht angelegt werden: \(error.localizedDescription). HA-Administratorrechte erforderlich."
        }
    }

    private var verification: some View {
        Group {
            setupCheck("FRITZ!Box", detail: fritzStatus, ready: fritzReachable && fritzAuthenticated && fritzVoIPAvailable)
            setupCheck("Home Assistant", detail: callWebhookHAStatus, ready: homeAssistantReachable && haAuthenticated && callWebhookHAReady)
            setupCheck("Leitung 1", detail: "\(line1Label) – \(line1Number)", ready: !line1Number.isEmpty)
            setupCheck("Leitung 2", detail: sipLine2Enabled ? "\(line2Label) – \(line2Number)" : "Deaktiviert", ready: !sipLine2Enabled || !line2Number.isEmpty)
            setupCheck("Leitung 3", detail: sipLine3Enabled ? "\(line3Label) – \(line3Number)" : "Deaktiviert", ready: !sipLine3Enabled || !line3Number.isEmpty)
            setupCheck(
                "FRITZ-SIP-Nebenstellen",
                detail: fritzSIPSummary,
                ready: fritzSIPVerified
            )
            setupCheck("Anrufstatus-Schalter", detail: callHelperStatus, ready: callHelperReady)
            setupCheck("Mobilfunk-Rufumleitungen", detail: forwardingReady ? "Aktivierung vom Benutzer nach Netzbestätigung bestätigt" : "Aktivierung für die Mobilfunkleitungen noch bestätigen", ready: forwardingReady)
            setupCheck("Mailbox-Auswahl in der App", detail: mailboxSummary, ready: mailboxSelectionVerified)
            setupCheck("Reagierende Rufnummern der FRITZ!-Mailboxen", detail: mailboxNumbersVerified ? "Aus der FRITZ!Box gelesen und abgeglichen" : "Noch nicht geprüft – Funktionstest starten", ready: mailboxNumbersVerified)
            Section("Eingehende Anrufe bei gesperrtem iPhone") {
                if PushRelayRegistration.shared.baseURL == nil || operatorPushWorking {
                    VoIPPushSettingsView(working: $operatorPushWorking)
                }
                Text(setupPush.backendStatus).font(.caption)
                if let relayURL = PushRelayRegistration.shared.baseURL {
                    Text(relayURL.absoluteString).font(.caption2).textSelection(.enabled)
                }
                if setupPush.settingUp { ProgressView("Anruf-Push wird eingerichtet …") }
                Button("Anruf-Push automatisch einrichten / erneut prüfen") {
                    Task { await setupPush.completeSetup() }
                }.disabled(setupPush.settingUp || operatorPushWorking || setupSIP.active)
                Text("Nach Bereitstellung des gemeinsamen Dienstes erfolgt die Anmeldung automatisch. Nutzer benötigen kein Apple-Konto und keine Schlüsseldatei. Für das Gespräch muss Asterisk weiterhin über Heimnetz oder VPN erreichbar sein.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Funktionstest") {
                Button("Verbindungen und Mailbox-Zuordnung prüfen") {
                    Task { await runFunctionTest() }
                }.disabled(functionTestRunning || setupSIP.active)
                if functionTestRunning { ProgressView("Prüfung läuft …") }
                ForEach(Array(functionTestResults.enumerated()), id: \.offset) { _, result in
                    Text(result).font(.caption)
                }
                ForEach(mailboxAssignmentInstructions, id: \.self) { instruction in
                    Text(instruction).font(.caption).textSelection(.enabled)
                }
                Button("Anrufbeantworter-Zuordnung auf FRITZ!Box speichern") {
                    Task { if await saveMailboxSelection() { await runFunctionTest() } }
                }.disabled(isSavingMailboxes || functionTestRunning || setupSIP.active)
                Button("Anrufbeantworter in der FRITZ!Box öffnen") {
                    if let url = URL(string: "http://\(fritzHost)/?lp=tam") { UIApplication.shared.open(url) }
                }
                Text("Beim Speichern überträgt CallWebhook die gewählten Rufnummern auf die FRITZ!Box und liest sie zur Kontrolle zurück. Falls die FRITZ!Box es verlangt, im Dialog einen angebotenen Bestätigungsweg wählen.")
                    .font(.caption).foregroundStyle(.secondary)
                Text("Anschließend einen ausgehenden Anruf testen, das iPhone sperren und von einem zweiten Telefon anrufen. Für den Mailbox-Test nicht annehmen und eine Nachricht hinterlassen.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            setupCheck("Asterisk + iPhone-SIP", detail: asteriskConfigStatus, ready: asteriskInstalled && setupSIP.registered)
        }
    }

    @MainActor
    private func runFunctionTest() async {
        guard !functionTestRunning else { return }
        functionTestRunning = true
        functionTestResults = []
        mailboxNumbersVerified = false
        defer { functionTestRunning = false }
        do {
            let base = URL(string: "http://192.168.178.\(homeAssistantURL):8123")!
            let (_, status) = try await HomeAssistantConnection.request(base: base, path: "api/")
            functionTestResults.append(status == 200 ? "✓ Home Assistant authentifiziert erreichbar" : "✗ Home Assistant: HTTP \(status)")
        } catch { functionTestResults.append("✗ Home Assistant: \(error.localizedDescription)") }
        functionTestResults.append(setupSIP.registered ? "✓ iPhone bei Asterisk registriert" : "✗ iPhone nicht bei Asterisk registriert")
        await VoIPPushService.shared.synchronize()
        functionTestResults.append("Push: \(VoIPPushService.shared.backendStatus)")
        do {
            let rawHost = fritzHost.trimmingCharacters(in: .whitespacesAndNewlines)
            let base = rawHost.contains("://") ? rawHost : "http://\(rawHost):49000"
            guard let url = URL(string: base + "/tr64desc.xml") else { throw URLError(.badURL) }
            let (data, _) = try await URLSession.shared.data(from: url)
            let description = String(decoding: data, as: UTF8.self)
            guard let service = extractTR064Services(from: description).first(where: { $0.type.contains("X_AVM-DE_TAM") }) else { throw URLError(.unsupportedURL) }
            let session = URLSession(configuration: .ephemeral, delegate: FritzAuthDelegate(username: fritzUser, password: fritzPassword), delegateQueue: nil)
            defer { session.finishTasksAndInvalidate() }
            let lines = [(1, line1Number, mailbox1TAM), (2, line2Number, sipLine2Enabled ? mailbox2TAM : -1), (3, line3Number, sipLine3Enabled ? mailbox3TAM : -1)]
            var verified = true
            for (line, number, tam) in lines where tam >= 0 {
                let response = try await soapCall(session: session, base: base, serviceType: service.type, controlURL: service.controlURL, action: "GetInfo", arguments: [("NewIndex", String(tam))])
                // An absent field is not the documented empty value (all numbers).
                let supported = response.contains("NewPhoneNumbers")
                let actual = extractSOAPValue("NewPhoneNumbers", from: response)
                let enabled = ["1", "true"].contains(extractSOAPValue("NewEnable", from: response).lowercased())
                let expectedNumbers = lines.filter { $0.2 == tam }.map { $0.1 }
                let matches = supported && enabled && FritzPhoneNumbers.tamMatches(numbers: expectedNumbers, configured: actual)
                verified = verified && matches
                functionTestResults.append("\(matches ? "✓" : "✗") Leitung \(line), AB \(tam + 1): Soll \(number); FRITZ!Box: \(supported ? (actual.isEmpty ? "alle Rufnummern – Zuordnung fehlt" : actual) : "nicht lesbar")\(enabled ? "" : " (deaktiviert)")")
            }
            mailboxNumbersVerified = verified
        } catch { functionTestResults.append("✗ FRITZ!-Mailbox-Prüfung: \(error.localizedDescription)") }
    }

    private var fritzSIPProvisioned: Bool { fritzSIPVerified }

    private var mailboxAssignmentInstructions: [String] {
        let lines = [(line1Number, mailbox1TAM), (line2Number, sipLine2Enabled ? mailbox2TAM : -1),
                     (line3Number, sipLine3Enabled ? mailbox3TAM : -1)]
        let selected = Dictionary(grouping: lines.filter { $0.1 >= 0 }, by: { $0.1 })
        return selected.keys.sorted().map { tam in
            let numbers = selected[tam, default: []].map { $0.0 }.joined(separator: ", ")
            return "Anrufbeantworter \(tam + 1) → nur \(numbers)"
        }
    }

    private var mailboxSummary: String {
        if fritzTAMs.isEmpty { return "Keine FRITZ!-Mailbox erkannt" }
        var names: [String] = []
        if let tam = fritzTAMs.first(where: { $0.index == mailbox1TAM }) { names.append("\(line1Label): \(tam.displayName)") }
        if sipLine2Enabled, let tam = fritzTAMs.first(where: { $0.index == mailbox2TAM }) { names.append("\(line2Label): \(tam.displayName)") }
        if sipLine3Enabled, let tam = fritzTAMs.first(where: { $0.index == mailbox3TAM }) { names.append("\(line3Label): \(tam.displayName)") }
        return names.isEmpty ? "Nicht verwendet" : names.joined(separator: " · ")
    }

    @MainActor
    private func installAsteriskConfiguration() async {
        guard let pjsip = SetupKeychain.get(account: "asterisk-pjsip-generated"),
              let extensions = SetupKeychain.get(account: "asterisk-extensions-generated") else {
            asteriskInstalled = false
            asteriskConfigStatus = "Vorbereitete Asterisk-Konfiguration fehlt"
            return
        }

        let input = homeAssistantURL.trimmingCharacters(in: .whitespacesAndNewlines)
        let raw = "http://192.168.178.\(input):8123"
        guard let base = URL(string: raw),
              let url = URL(string: "/api/callwebhook/setup/asterisk", relativeTo: base)?.absoluteURL else {
            asteriskInstalled = false
            asteriskConfigStatus = "Ungültige Home-Assistant-Adresse"
            return
        }

        isInstallingAsterisk = true
        asteriskInstallFailed = false
        defer { isInstallingAsterisk = false }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 15
        guard var haToken = SetupKeychain.get(account: "home-assistant-token"), !haToken.isEmpty else {
            asteriskInstalled = false
            asteriskConfigStatus = "Home Assistant noch nicht autorisiert"
            return
        }
        request.setValue("Bearer \(haToken)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        let body: [String: Any] = [
            "pjsip": pjsip,
            "extensions": extensions,
            "addon": "b35499aa_asterisk",
            "custom_path": "/addon_configs/b35499aa_asterisk/asterisk/custom",
            "mailbox_tam_1": mailbox1TAM,
            "mailbox_tam_2": mailbox2TAM,
            "mailbox_tam_3": mailbox3TAM
        ]

        do {
            asteriskProgressStep = 0
            asteriskConfigStatus = "Installiere und konfiguriere Asterisk über Home Assistant …"
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
            var (data, response) = try await URLSession.shared.data(for: request)
            var http = response as? HTTPURLResponse
            if http?.statusCode == 401 {
                haToken = try await HomeAssistantAuth.shared.refresh(instance: base)
                setupHAToken = haToken
                request.setValue("Bearer \(haToken)", forHTTPHeaderField: "Authorization")
                (data, response) = try await URLSession.shared.data(for: request)
                http = response as? HTTPURLResponse
            }
            guard let http else { throw URLError(.badServerResponse) }
            guard (200..<300).contains(http.statusCode) else {
                let message = String(data: data, encoding: .utf8) ?? "HTTP \(http.statusCode)"
                asteriskInstalled = false
                asteriskInstallFailed = true
                asteriskConfigStatus = "HA-Provisionierung fehlgeschlagen: \(message)"
                return
            }
            guard let statusURL = URL(string: "/api/callwebhook/setup/asterisk/status", relativeTo: base)?.absoluteURL else {
                throw URLError(.badURL)
            }
            var configVerified = false
            let provisioningDeadline = Date().addingTimeInterval(900)
            while Date() < provisioningDeadline {
                try await Task.sleep(nanoseconds: 250_000_000)
                var statusRequest = URLRequest(url: statusURL)
                statusRequest.timeoutInterval = 8
                statusRequest.setValue("Bearer \(haToken)", forHTTPHeaderField: "Authorization")
                do {
                    let (statusData, statusResponse) = try await URLSession.shared.data(for: statusRequest)
                    guard let statusHTTP = statusResponse as? HTTPURLResponse, (200..<300).contains(statusHTTP.statusCode),
                          let statusJSON = (try? JSONSerialization.jsonObject(with: statusData)) as? [String: Any] else { continue }
                    let state = statusJSON["state"] as? String ?? "running"
                    asteriskProgressStep = min(statusJSON["progress_step"] as? Int ?? asteriskProgressStep, asteriskProgressTotal - 1)
                    asteriskConfigStatus = statusJSON["message"] as? String ?? "Asterisk wird eingerichtet …"
                    if state == "error" {
                        asteriskInstalled = false
                        asteriskInstallFailed = true
                        return
                    }
                    if state == "done" {
                        let result = statusJSON["result"] as? [String: Any]
                        configVerified = (result?["config_verified"] as? Bool) ?? false
                        break
                    }
                } catch {
                    continue
                }
            }
            guard configVerified else {
                asteriskInstalled = false
                asteriskInstallFailed = true
                asteriskConfigStatus = "Asterisk-Einrichtung wurde nicht vollständig verifiziert"
                return
            }
            guard let iosPassword = SetupKeychain.get(account: "asterisk-sip-callwebhook-ios"),
                  let host = base.host else {
                asteriskInstalled = false
                asteriskConfigStatus = "iPhone-SIP-Zugang fehlt nach Installation"
                return
            }
            let defaults = UserDefaults.standard
            defaults.set(true, forKey: "sipEnabled")
            defaults.set("callwebhook-ios", forKey: "sipUsername")
            defaults.removeObject(forKey: "sipPassword")
            defaults.set(host, forKey: "sipHost")

            asteriskInstalled = false
            asteriskConfigStatus = "Asterisk bereit – prüfe iPhone-SIP-Registrierung …"
            try setupSIP.configureAndStart(host: host, username: "callwebhook-ios", password: iosPassword)

            for _ in 0..<15 {
                if setupSIP.registered {
                    asteriskInstalled = true
                    asteriskProgressStep = asteriskProgressTotal
                    asteriskConfigStatus = "Asterisk bereit – iPhone-SIP erfolgreich registriert"
                    break
                }
                try await Task.sleep(nanoseconds: 1_000_000_000)
            }
            if !asteriskInstalled {
                asteriskConfigStatus = "Asterisk läuft, aber iPhone-SIP wurde nicht registriert: \(setupSIP.status)"
            } else {
                await ensureCallHelper()
            }
        } catch {
            asteriskInstalled = false
            asteriskInstallFailed = true
            asteriskConfigStatus = "Asterisk-Installation fehlgeschlagen: \(error.localizedDescription)"
        }
    }

    private func prepareAsteriskConfiguration() {
        if easybellEnabled {
            do {
                try SetupKeychain.set(easybellUsername.trimmingCharacters(in: .whitespacesAndNewlines), account: "easybell-sip-username")
                try SetupKeychain.set(easybellPassword, account: "easybell-sip-password")
                try SetupKeychain.set(easybellContactUser.trimmingCharacters(in: .whitespacesAndNewlines), account: "easybell-contact-user")
            } catch {
                asteriskConfigReady = false
                asteriskConfigStatus = "easybell-Zugangsdaten konnten nicht sicher gespeichert werden"
                return
            }
        }
        easybellPassword = ""

        let password2 = SetupKeychain.get(account: "fritz-sip-callwhapp2")
        guard let password1 = SetupKeychain.get(account: "fritz-sip-callwhapp1"),
              !sipLine2Enabled || password2 != nil else {
            asteriskConfigReady = false
            asteriskConfigStatus = "FRITZ-SIP-Zugangsdaten fehlen noch"
            return
        }

        let host = fritzHost.trimmingCharacters(in: .whitespacesAndNewlines)
            .replacingOccurrences(of: "http://", with: "")
            .replacingOccurrences(of: "https://", with: "")
            .components(separatedBy: ":").first ?? "fritz.box"

        let iosPassword: String
        if let saved = SetupKeychain.get(account: "asterisk-sip-callwebhook-ios"), !saved.isEmpty {
            iosPassword = saved
        } else {
            let generated = randomSIPPassword()
            do {
                try SetupKeychain.set(generated, account: "asterisk-sip-callwebhook-ios")
                iosPassword = generated
            } catch {
                asteriskConfigReady = false
                asteriskConfigStatus = "iPhone-SIP-Zugang konnte nicht sicher gespeichert werden"
                return
            }
        }

        let password3 = sipLine3Enabled ? SetupKeychain.get(account: "fritz-sip-callwhapp3") : nil
        if sipLine3Enabled && password3 == nil {
            asteriskConfigReady = false
            asteriskConfigStatus = "FRITZ-SIP-Zugangsdaten für Leitung 3 fehlen noch"
            return
        }

        let easybellPJSIP: String
        if easybellEnabled,
           let ebUser = SetupKeychain.get(account: "easybell-sip-username"),
           let ebPassword = SetupKeychain.get(account: "easybell-sip-password"),
           let ebContact = SetupKeychain.get(account: "easybell-contact-user") {
            easybellPJSIP = """
            
            [easybell-auth]
            type=auth
            auth_type=userpass
            username=\(ebUser)
            password=\(ebPassword)

            [easybell-aor]
            type=aor
            contact=sip:voip.easybell.de

            [easybell-endpoint]
            type=endpoint
            transport=transport-udp
            context=from-easybell
            disallow=all
            allow=alaw,ulaw,g722
            outbound_auth=easybell-auth
            aors=easybell-aor
            from_user=\(ebUser)
            from_domain=voip.easybell.de
            send_pai=yes
            direct_media=no
            force_rport=yes
            rtp_symmetric=yes

            [easybell-registration]
            type=registration
            transport=transport-udp
            outbound_auth=easybell-auth
            server_uri=sip:voip.easybell.de
            client_uri=sip:\(ebUser)@voip.easybell.de
            contact_user=\(ebContact)
            retry_interval=60
            line=yes
            endpoint=easybell-endpoint
            """
        } else {
            easybellPJSIP = ""
        }

        let fritz3 = sipLine3Enabled ? """

        [fritz3-auth]
        type=auth
        auth_type=userpass
        username=callwhapp3
        password=\(password3 ?? "")

        [fritz3-aor]
        type=aor
        contact=sip:\(host)

        [fritz3-endpoint]
        type=endpoint
        transport=transport-udp
        context=from-fritz
        disallow=all
        allow=alaw,ulaw
        outbound_auth=fritz3-auth
        aors=fritz3-aor
        from_user=callwhapp3
        from_domain=\(host)
        direct_media=no

        [fritz3-registration]
        type=registration
        transport=transport-udp
        outbound_auth=fritz3-auth
        server_uri=sip:\(host)
        client_uri=sip:callwhapp3@\(host)
        contact_user=callwhapp3
        retry_interval=60
        line=yes
        endpoint=fritz3-endpoint
        """ : ""

        let fritz2 = sipLine2Enabled ? """
        [fritz2-auth]
        type=auth
        auth_type=userpass
        username=callwhapp2
        password=\(password2 ?? "")

        [fritz2-aor]
        type=aor
        contact=sip:\(host)

        [fritz2-endpoint]
        type=endpoint
        transport=transport-udp
        context=from-fritz
        disallow=all
        allow=alaw,ulaw
        outbound_auth=fritz2-auth
        aors=fritz2-aor
        from_user=callwhapp2
        from_domain=\(host)
        direct_media=no

        [fritz2-registration]
        type=registration
        transport=transport-udp
        outbound_auth=fritz2-auth
        server_uri=sip:\(host)
        client_uri=sip:callwhapp2@\(host)
        contact_user=callwhapp2
        retry_interval=60
        line=yes
        endpoint=fritz2-endpoint
        """ : ""

        let pjsip = """
        [global]
        type=global

        [transport-udp]
        type=transport
        protocol=udp
        bind=0.0.0.0:5060

        [callwebhook-ios-auth]
        type=auth
        auth_type=userpass
        username=callwebhook-ios
        password=\(iosPassword)

        [callwebhook-ios]
        type=aor
        max_contacts=1
        remove_existing=yes

        [callwebhook-ios]
        type=endpoint
        transport=transport-udp
        context=from-callwebhook-ios
        disallow=all
        allow=alaw,ulaw
        auth=callwebhook-ios-auth
        aors=callwebhook-ios
        direct_media=no
        force_rport=yes
        rewrite_contact=yes
        rtp_symmetric=yes
        \(easybellPJSIP)

        [fritz1-auth]
        type=auth
        auth_type=userpass
        username=callwhapp1
        password=\(password1)

        [fritz1-aor]
        type=aor
        contact=sip:\(host)

        [fritz1-endpoint]
        type=endpoint
        transport=transport-udp
        context=from-fritz
        disallow=all
        allow=alaw,ulaw
        outbound_auth=fritz1-auth
        aors=fritz1-aor
        from_user=callwhapp1
        from_domain=\(host)
        direct_media=no

        [fritz1-registration]
        type=registration
        transport=transport-udp
        outbound_auth=fritz1-auth
        server_uri=sip:\(host)
        client_uri=sip:callwhapp1@\(host)
        contact_user=callwhapp1
        retry_interval=60
        line=yes
        endpoint=fritz1-endpoint

        \(fritz2)
        \(fritz3)
        """

        let prefix2 = sipLine2Prefix.isEmpty ? "*82" : sipLine2Prefix
        let prefix3 = sipLine3Prefix.isEmpty ? "*83" : sipLine3Prefix
        let publicLine1Endpoint = easybellEnabled ? "easybell-endpoint" : "fritz1-endpoint"
        let publicLine2Endpoint = easybellEnabled ? "easybell-endpoint" : "fritz2-endpoint"
        let line1CallerID = easybellEnabled ? " same => n,Set(CALLERID(name)=\\(line1Number))\\n" : ""
        let line2CallerID = easybellEnabled ? " same => n,Set(CALLERID(name)=\\(line2Number))\\n" : ""
        let dialplan = """
        [from-callwebhook-ios]
        exten => _\(prefix2)**X.,1,NoOp(CallWebhook Leitung 2 internal FRITZ call to ${EXTEN:\(prefix2.count)})
        \(line2CallerID) same => n,Dial(PJSIP/${EXTEN:\(prefix2.count)}@\(publicLine2Endpoint),60)
         same => n,Hangup()

        exten => _\(prefix2)X.,1,NoOp(CallWebhook Leitung 2 to ${EXTEN:\(prefix2.count)})
         same => n,Dial(PJSIP/${EXTEN:\(prefix2.count)}@fritz2-endpoint,60)
         same => n,Hangup()

        exten => _\(prefix3)**X.,1,NoOp(CallWebhook Leitung 3 internal FRITZ call to ${EXTEN:\(prefix3.count)})
         same => n,Dial(PJSIP/${EXTEN:\(prefix3.count)}@fritz3-endpoint,60)
         same => n,Hangup()

        exten => _\(prefix3)X.,1,NoOp(CallWebhook Leitung 3 to ${EXTEN:\(prefix3.count)})
         same => n,Dial(PJSIP/${EXTEN:\(prefix3.count)}@fritz3-endpoint,60)
         same => n,Hangup()

        exten => _**X.,1,NoOp(CallWebhook internal FRITZ call to ${EXTEN})
        \(line1CallerID) same => n,Dial(PJSIP/${EXTEN}@\(publicLine1Endpoint),60)
         same => n,Hangup()

        exten => _X.,1,NoOp(CallWebhook Leitung 1 to ${EXTEN})
         same => n,Dial(PJSIP/${EXTEN}@fritz1-endpoint,60)
         same => n,Hangup()
        """

        do {
            try SetupKeychain.set(pjsip, account: "asterisk-pjsip-generated")
            try SetupKeychain.set(IncomingDialplan.addingIncomingRoute(to: dialplan), account: "asterisk-extensions-generated")
            asteriskConfigReady = true
            asteriskConfigStatus = "Konfiguration sicher vorbereitet – Übergabe an Home Assistant folgt"
        } catch {
            asteriskConfigReady = false
            asteriskConfigStatus = "Asterisk-Konfiguration konnte nicht sicher gespeichert werden"
        }
    }

    private func setupCheck(_ title: String, detail: String, ready: Bool) -> some View {
        HStack {
            Image(systemName: ready ? "checkmark.circle.fill" : "circle.dashed")
                .foregroundStyle(ready ? .green : .secondary)
            VStack(alignment: .leading) {
                Text(title)
                Text(detail).font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    private var canContinue: Bool {
        switch step {
        case 3:
            let linesReady = !line1Number.isEmpty && (!sipLine2Enabled || !line2Number.isEmpty) && (!sipLine3Enabled || !line3Number.isEmpty)
            let easybellReady = !easybellEnabled || (
                !easybellUsername.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                    && !easybellPassword.isEmpty
                    && !easybellContactUser.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            )
            return linesReady && easybellReady && forwardingReady
        case 5:
            return mailboxNumbersVerified && fritzReachable
                && fritzAuthenticated
                && fritzVoIPAvailable
                && fritzSIPVerified
                && homeAssistantReachable
                && haAuthenticated
                && callWebhookHAReady
                && mailboxSelectionVerified
                && callHelperReady
                && forwardingReady
                && asteriskInstalled
                && setupSIP.registered
                && setupPush.configured
                && setupPush.routeReady
        default: return true
        }
    }

    @MainActor
    private func checkFritzBox() async {
        isChecking = true
        defer { isChecking = false }
        fritzReachable = false
        fritzVoIPAvailable = false
        fritzTAMAvailable = false
        fritzServiceCount = 0
        fritzAuthenticated = false
        fritzVoIPNumbers = []
        fritzTAMCount = 0
        fritzTAMs = []
        fritzSIPClients = []
        sipClient1Index = nil
        sipClient2Index = nil
        sipClient1Plan = "Noch nicht geprüft"
        sipClient2Plan = "Noch nicht geprüft"
        sipClient3Index = nil
        sipClient3Plan = "Noch nicht geprüft"
        fritzStatus = "Prüfe FRITZ!Box …"

        let rawHost = fritzHost.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !rawHost.isEmpty else {
            fritzStatus = "Adresse fehlt"
            return
        }
        let base = rawHost.contains("://") ? rawHost : "http://\(rawHost):49000"
        guard let url = URL(string: base + "/tr64desc.xml") else {
            fritzStatus = "Ungültige FRITZ!Box-Adresse"
            return
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 5
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse,
                  (200..<400).contains(http.statusCode),
                  let xml = String(data: data, encoding: .utf8),
                  xml.localizedCaseInsensitiveContains("device") else {
                fritzStatus = "Keine TR-064-Beschreibung gefunden"
                return
            }

            let serviceTypes = extractTR064ServiceTypes(from: xml)
            fritzServiceCount = serviceTypes.count
            fritzVoIPAvailable = serviceTypes.contains { $0.localizedCaseInsensitiveContains("X_VoIP") || $0.localizedCaseInsensitiveContains("VoIP") }
            fritzTAMAvailable = serviceTypes.contains { $0.localizedCaseInsensitiveContains("X_AVM-DE_TAM") || $0.localizedCaseInsensitiveContains(":TAM:") }
            fritzReachable = true

            guard fritzVoIPAvailable else {
                fritzStatus = "FRITZ!Box erreichbar – Telefoniedienst nicht erkannt"
                return
            }
            guard !fritzUser.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                  !fritzPassword.isEmpty else {
                fritzStatus = "FRITZ!Box erreichbar – Benutzer und Kennwort fehlen"
                return
            }

            let services = extractTR064Services(from: xml)
            guard let voipService = services.first(where: { $0.type.localizedCaseInsensitiveContains("X_VoIP") || $0.type.localizedCaseInsensitiveContains("VoIP") }) else {
                fritzStatus = "X_VoIP-Steuerung nicht gefunden"
                return
            }

            let auth = FritzAuthDelegate(username: fritzUser, password: fritzPassword)
            let session = URLSession(configuration: .ephemeral, delegate: auth, delegateQueue: nil)
            defer { session.finishTasksAndInvalidate() }
            var secondFactorToken: String? = nil


            do {
                var resolvedNumbers: [String] = []
                // GetNumbers returns the complete configured telephone-number list.
                // GetExistingVoIPNumbers returns a COUNT, not telephone numbers.
                for action in ["X_AVM-DE_GetNumbers", "X_AVM-DE_GetVoIPAccounts"] {
                    if let response = try? await soapCall(
                        session: session, base: base, serviceType: voipService.type,
                        controlURL: voipService.controlURL, action: action, arguments: []
                    ) {
                        fritzNumberAssignments.merge(FritzPhoneNumbers.incomingAssignments(from: response)) { _, new in new }
                        resolvedNumbers = FritzPhoneNumbers.parse(response)
                        if !resolvedNumbers.isEmpty { break }
                    }
                }
                if resolvedNumbers.isEmpty {
                    let countXML = try await soapCall(
                        session: session, base: base, serviceType: voipService.type,
                        controlURL: voipService.controlURL, action: "GetExistingVoIPNumbers", arguments: []
                    )
                    let count = Int(extractSOAPValue("NewExistingVoIPNumbers", from: countXML)) ?? 0
                    if count > 0 {
                        var slots = count
                        if let maxXML = try? await soapCall(
                            session: session, base: base, serviceType: voipService.type,
                            controlURL: voipService.controlURL, action: "GetMaxVoIPNumbers", arguments: []
                        ), let maximum = Int(extractSOAPValue("NewMaxVoIPNumbers", from: maxXML)) {
                            slots = max(count, maximum)
                        }
                        for index in 0..<slots {
                            if let account = try? await soapCall(
                                session: session, base: base, serviceType: voipService.type,
                                controlURL: voipService.controlURL, action: "X_AVM-DE_GetVoIPAccount",
                                arguments: [("NewVoIPAccountIndex", String(index))]
                            ) {
                                resolvedNumbers.append(contentsOf: FritzPhoneNumbers.parse(account))
                                if resolvedNumbers.count >= count { break }
                            }
                        }
                    }
                }
                var seenNumbers = Set<String>()
                fritzVoIPNumbers = resolvedNumbers.filter { seenNumbers.insert($0).inserted }
                // Clear the old count-as-number selection without replacing manual numbers.
                let oldCount = String(fritzVoIPNumbers.count)
                if !fritzVoIPNumbers.contains(oldCount) {
                    if line1Number == oldCount { line1Number = "" }
                    if line2Number == oldCount { line2Number = "" }
                    if line3Number == oldCount { line3Number = "" }
                }
                fritzAuthenticated = true
                do {
                    if !voipService.scpdURL.isEmpty {
                        let path = voipService.scpdURL.hasPrefix("/") ? voipService.scpdURL : "/" + voipService.scpdURL
                        if let scpdURL = URL(string: base + path) {
                            let (data, response) = try await session.data(from: scpdURL)
                            if let http = response as? HTTPURLResponse,
                               (200..<300).contains(http.statusCode),
                               let scpd = String(data: data, encoding: .utf8) {
                                if scpd.contains("<name>X_AVM-DE_SetClient4</name>") {
                                    fritzSIPWriteAction = "X_AVM-DE_SetClient4"
                                } else if scpd.contains("<name>X_AVM-DE_SetClient3</name>") {
                                    fritzSIPWriteAction = "X_AVM-DE_SetClient3"
                                } else if scpd.contains("<name>X_AVM-DE_SetClient2</name>") {
                                    fritzSIPWriteAction = "X_AVM-DE_SetClient2"
                                }
                                if !fritzSIPWriteAction.isEmpty {
                                    fritzSIPWriteArguments = actionArgumentNames(fritzSIPWriteAction, in: scpd)
                                } else {
                                    fritzSIPWriteArguments = []
                                }
                                fritzSIPWriteStatus = fritzSIPWriteAction.isEmpty
                                    ? "Keine SIP-Schreibaktion angeboten"
                                    : "SIP-Schreibaktion: \(fritzSIPWriteAction) · \(fritzSIPWriteArguments.count) Argumente"

                            }
                        }
                    }
                } catch {
                    fritzSIPWriteAction = ""
                    fritzSIPWriteArguments = []
                    fritzSIPWriteStatus = "SIP-Schreibschnittstelle nicht lesbar"
                }
                if line1Number.isEmpty, let first = fritzVoIPNumbers.first { line1Number = first }
                if line2Number.isEmpty, fritzVoIPNumbers.count > 1 { line2Number = fritzVoIPNumbers[1] }
                if line3Number.isEmpty {
                    line3Number = fritzVoIPNumbers.first(where: { $0 != line1Number && (!sipLine2Enabled || $0 != line2Number) }) ?? ""
                }

                var clients: [FritzSIPClient] = []
                for index in 0..<20 {
                    do {
                        let clientResponse = try await soapCall(
                            session: session,
                            base: base,
                            serviceType: voipService.type,
                            controlURL: voipService.controlURL,
                            action: "X_AVM-DE_GetClient3",
                            arguments: [("NewX_AVM-DE_ClientIndex", String(index))]
                        )
                        let username = extractSOAPValue("NewX_AVM-DE_ClientUsername", from: clientResponse)
                        let phoneName = extractSOAPValue("NewX_AVM-DE_PhoneName", from: clientResponse)
                        let outgoing = extractSOAPValue("NewX_AVM-DE_OutGoingNumber", from: clientResponse)
                        let internalNumber = extractSOAPValue("NewX_AVM-DE_InternalNumber", from: clientResponse)
                        if !username.isEmpty || !phoneName.isEmpty || !internalNumber.isEmpty {
                            clients.append(FritzSIPClient(
                                index: index,
                                username: username,
                                phoneName: phoneName,
                                outgoingNumber: outgoing,
                                internalNumber: internalNumber
                            ))
                        } else {
                        }
                    } catch {
                        do {
                            let clientResponse = try await soapCall(
                                session: session,
                                base: base,
                                serviceType: voipService.type,
                                controlURL: voipService.controlURL,
                                action: "X_AVM-DE_GetClient2",
                                arguments: [("NewX_AVM-DE_ClientIndex", String(index))]
                            )
                            let username = extractSOAPValue("NewX_AVM-DE_ClientUsername", from: clientResponse)
                            let phoneName = extractSOAPValue("NewX_AVM-DE_PhoneName", from: clientResponse)
                            let outgoing = extractSOAPValue("NewX_AVM-DE_OutGoingNumber", from: clientResponse)
                            let internalNumber = extractSOAPValue("NewX_AVM-DE_InternalNumber", from: clientResponse)
                            if !username.isEmpty || !phoneName.isEmpty || !internalNumber.isEmpty {
                                clients.append(FritzSIPClient(
                                    index: index,
                                    username: username,
                                    phoneName: phoneName,
                                    outgoingNumber: outgoing,
                                    internalNumber: internalNumber
                                ))
                            } else {
                            }
                        } catch {
                        }
                    }
                }
                fritzSIPClients = clients
                var reserved = Set(clients.map(\.index))
                if let existing = clients.first(where: { $0.username == "callwhapp1" || $0.phoneName == "callwhapp1" }) {
                    sipClient1Index = existing.index
                    sipClient1Plan = "vorhanden, Index \(existing.index)"
                } else if let free = (0..<20).first(where: { !reserved.contains($0) }) {
                    sipClient1Index = free
                    reserved.insert(free)
                    sipClient1Plan = "freier Index \(free) reserviert"
                } else {
                    sipClient1Plan = "kein freier Clientplatz"
                }
                if let existing = clients.first(where: { $0.username == "callwhapp2" || $0.phoneName == "callwhapp2" }) {
                    sipClient2Index = existing.index
                    sipClient2Plan = "vorhanden, Index \(existing.index)"
                } else if let free = (0..<20).first(where: { !reserved.contains($0) }) {
                    sipClient2Index = free
                    reserved.insert(free)
                    sipClient2Plan = "freier Index \(free) reserviert"
                } else {
                    sipClient2Plan = "kein freier Clientplatz"
                }

                if sipLine3Enabled {
                    if let existing = clients.first(where: { $0.username == "callwhapp3" || $0.phoneName == "callwhapp3" }) {
                        sipClient3Index = existing.index
                        sipClient3Plan = "vorhanden, Index \(existing.index)"
                    } else if let free = (0..<20).first(where: { !reserved.contains($0) }) {
                        sipClient3Index = free
                        reserved.insert(free)
                        sipClient3Plan = "freier Index \(free) reserviert"
                    } else {
                        sipClient3Plan = "kein freier Clientplatz"
                    }
                } else {
                    sipClient3Index = nil
                    sipClient3Plan = "Leitung 3 deaktiviert"
                }

                if let tamService = services.first(where: { $0.type.localizedCaseInsensitiveContains("X_AVM-DE_TAM") || $0.type.localizedCaseInsensitiveContains(":TAM:") }) {
                    var discovered: [FritzTAM] = []
                    for index in 0..<10 {
                        do {
                            let tamResponse = try await soapCall(
                                session: session,
                                base: base,
                                serviceType: tamService.type,
                                controlURL: tamService.controlURL,
                                action: "GetInfo",
                                arguments: [("NewIndex", String(index))]
                            )
                            let name = extractSOAPValue("NewName", from: tamResponse)
                            let enableValue = extractSOAPValue("NewEnable", from: tamResponse).lowercased()
                            let enabled = enableValue == "1" || enableValue == "true"
                            if enabled {
                                discovered.append(FritzTAM(index: index, name: name, enabled: true))
                            }
                        } catch {
                            if index > 1 { break }
                        }
                    }
                    fritzTAMs = discovered
                    fritzTAMCount = discovered.count
                    if discovered.isEmpty {
                        mailbox1TAM = -2
                        mailbox2TAM = -2
                        mailbox3TAM = -2
                    }
                }

                fritzStatus = "FRITZ-Anmeldung erfolgreich – Telefonie ausgelesen"
            } catch {
                fritzAuthenticated = false
                fritzStatus = "FRITZ-Anmeldung fehlgeschlagen: \(error.localizedDescription)"
            }
        } catch {
            fritzStatus = "Nicht erreichbar: \(error.localizedDescription)"
        }
    }

    private struct TR064Service {
        let type: String
        let controlURL: String
        let scpdURL: String
    }

    private func extractTR064Services(from xml: String) -> [TR064Service] {
        let blockPattern = "<service>\\s*([\\s\\S]*?)\\s*</service>"
        guard let blockRegex = try? NSRegularExpression(pattern: blockPattern, options: [.caseInsensitive]) else { return [] }
        let range = NSRange(xml.startIndex..<xml.endIndex, in: xml)
        return blockRegex.matches(in: xml, range: range).compactMap { match in
            guard match.numberOfRanges > 1,
                  let blockRange = Range(match.range(at: 1), in: xml) else { return nil }
            let block = String(xml[blockRange])
            guard let type = firstXMLValue("serviceType", in: block),
                  let controlURL = firstXMLValue("controlURL", in: block) else { return nil }
            let scpdURL = firstXMLValue("SCPDURL", in: block) ?? ""
            return TR064Service(type: type, controlURL: controlURL, scpdURL: scpdURL)
        }
    }

    private func firstXMLValue(_ tag: String, in xml: String) -> String? {
        let pattern = "<\(NSRegularExpression.escapedPattern(for: tag))>\\s*([^<]+)\\s*</\(NSRegularExpression.escapedPattern(for: tag))>"
        guard let regex = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]) else { return nil }
        let range = NSRange(xml.startIndex..<xml.endIndex, in: xml)
        guard let match = regex.firstMatch(in: xml, range: range),
              match.numberOfRanges > 1,
              let valueRange = Range(match.range(at: 1), in: xml) else { return nil }
        return String(xml[valueRange]).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    @MainActor
    private func provisionMissingTAMs() async -> Bool {
        guard fritzAuthenticated, !isProvisioningTAM else { return false }
        isProvisioningTAM = true
        mailboxSaveError = nil
        defer { isProvisioningTAM = false; fritzConfirmation.finish() }
        let required = 1 + (sipLine2Enabled ? 1 : 0) + (sipLine3Enabled ? 1 : 0)
        let rawHost = fritzHost.trimmingCharacters(in: .whitespacesAndNewlines)
        let base = rawHost.contains("://") ? rawHost : "http://\(rawHost):49000"
        do {
            guard let url = URL(string: base + "/tr64desc.xml") else { throw URLError(.badURL) }
            let (data, _) = try await URLSession.shared.data(from: url)
            guard let xml = String(data: data, encoding: .utf8),
                  let service = extractTR064Services(from: xml).first(where: {
                      $0.type.localizedCaseInsensitiveContains("X_AVM-DE_TAM") || $0.type.localizedCaseInsensitiveContains(":TAM:")
                  }) else { throw URLError(.cannotParseResponse) }
            let auth = FritzAuthDelegate(username: fritzUser, password: fritzPassword)
            let session = URLSession(configuration: .ephemeral, delegate: auth, delegateQueue: nil)
            defer { session.finishTasksAndInvalidate() }
            var token: String?
            var active: [FritzTAM] = []
            var inactive: [Int] = []
            // Read all five slots before choosing any target. A failed read is
            // not evidence of a free slot and must never be written blindly.
            for index in 0..<5 {
                let info = try await soapCall(session: session, base: base, serviceType: service.type,
                    controlURL: service.controlURL, action: "GetInfo", arguments: [("NewIndex", String(index))])
                let enabled = ["1", "true"].contains(extractSOAPValue("NewEnable", from: info).lowercased())
                if enabled {
                    active.append(FritzTAM(index: index, name: extractSOAPValue("NewName", from: info), enabled: true))
                } else { inactive.append(index) }
            }
            let missing = max(0, required - active.count)
            for index in inactive.prefix(missing) {
                tamProvisionStatus = "Anrufbeantworter \(index + 1) wird eingerichtet …"
                do {
                    _ = try await soapCall(session: session, base: base, serviceType: service.type,
                        controlURL: service.controlURL, action: "SetEnable",
                        arguments: [("NewIndex", String(index)), ("NewEnable", "1")], secondFactorToken: token)
                } catch {
                    guard isSecondFactorRequired(error) else { throw error }
                    token = try await beginFritzSecondFactor(session: session, base: base, descriptionXML: xml)
                    _ = try await soapCall(session: session, base: base, serviceType: service.type,
                        controlURL: service.controlURL, action: "SetEnable",
                        arguments: [("NewIndex", String(index)), ("NewEnable", "1")], secondFactorToken: token)
                }
                let verified = try await soapCall(session: session, base: base, serviceType: service.type,
                    controlURL: service.controlURL, action: "GetInfo", arguments: [("NewIndex", String(index))])
                guard ["1", "true"].contains(extractSOAPValue("NewEnable", from: verified).lowercased()) else {
                    throw NSError(domain: "CallWebhook.TAM", code: 1, userInfo: [NSLocalizedDescriptionKey: "FRITZ!Box bestätigt Anrufbeantworter \(index + 1) nicht als aktiv"])
                }
                active.append(FritzTAM(index: index, name: extractSOAPValue("NewName", from: verified), enabled: true))
                // Retain verified partial success so a retry never repeats it.
                fritzTAMs = active.sorted { $0.index < $1.index }
                fritzTAMCount = active.count
            }
            guard active.count >= required else { throw URLError(.cannotParseResponse) }
            fritzTAMs = active.sorted { $0.index < $1.index }
            fritzTAMCount = active.count
            let selections = FritzTAMSelection.resolved(
                selections: [mailbox1TAM, sipLine2Enabled ? mailbox2TAM : -1, sipLine3Enabled ? mailbox3TAM : -1],
                available: active.map(\.index))
            mailbox1TAM = selections[0]
            mailbox2TAM = selections[1]
            mailbox3TAM = selections[2]
            mailboxNumbersVerified = false
            tamProvisionStatus = "\(active.count) Anrufbeantworter aus der FRITZ!Box zurückbestätigt – Rufnummernzuordnung folgt"
            return true
        } catch {
            tamProvisionStatus = "Anrufbeantworter-Einrichtung fehlgeschlagen: \(error.localizedDescription)"
            mailboxSaveError = tamProvisionStatus
            return false
        }
    }

    @MainActor
    private func provisionMissingSIPClients() async {
        guard fritzAuthenticated,
              !fritzSIPWriteAction.isEmpty,
              let index1 = sipClient1Index,
              (!sipLine2Enabled || sipClient2Index != nil) else {
            sipProvisionStatus = "FRITZ-SIP ist noch nicht vollständig geprüft"
            return
        }

        let client1 = fritzSIPClients.first { $0.username == "callwhapp1" || $0.phoneName == "callwhapp1" }
        let client2 = fritzSIPClients.first { $0.username == "callwhapp2" || $0.phoneName == "callwhapp2" }
        let client3 = fritzSIPClients.first { $0.username == "callwhapp3" || $0.phoneName == "callwhapp3" }
        let secret1Missing = SetupKeychain.get(account: "fritz-sip-callwhapp1") == nil
        let secret2Missing = sipLine2Enabled && SetupKeychain.get(account: "fritz-sip-callwhapp2") == nil
        let secret3Missing = sipLine3Enabled && SetupKeychain.get(account: "fritz-sip-callwhapp3") == nil
        if client1 != nil && (!sipLine2Enabled || client2 != nil) && (!sipLine3Enabled || client3 != nil)
            && !secret1Missing && !secret2Missing && !secret3Missing {
            sipProvisionStatus = "CallWebhook-SIP-Nebenstellen und sichere Zugangsdaten sind vollständig"
            return
        }

        isProvisioningSIP = true
        defer { isProvisioningSIP = false }

        let rawHost = fritzHost.trimmingCharacters(in: .whitespacesAndNewlines)
        let base = rawHost.contains("://") ? rawHost : "http://\(rawHost):49000"

        do {
            guard let descriptionURL = URL(string: base + "/tr64desc.xml") else { throw URLError(.badURL) }
            let (descriptionData, _) = try await URLSession.shared.data(from: descriptionURL)
            guard let descriptionXML = String(data: descriptionData, encoding: .utf8),
                  let voipService = extractTR064Services(from: descriptionXML).first(where: {
                      $0.type.localizedCaseInsensitiveContains("X_VoIP") || $0.type.localizedCaseInsensitiveContains("VoIP")
                  }) else {
                throw URLError(.cannotParseResponse)
            }

            let auth = FritzAuthDelegate(username: fritzUser, password: fritzPassword)
            let session = URLSession(configuration: .ephemeral, delegate: auth, delegateQueue: nil)
            defer { session.finishTasksAndInvalidate() }
            var secondFactorToken: String? = nil

            if client1 == nil || secret1Missing {
                let password = randomSIPPassword()
                try SetupKeychain.set(password, account: "fritz-sip-callwhapp1")
                do {
                    let targetIndex = client1?.index ?? index1
                    let args = try setClientArguments(index: targetIndex, username: "callwhapp1", password: password, outgoing: line1Number)
                    do {
                        _ = try await soapCall(session: session, base: base, serviceType: voipService.type, controlURL: voipService.controlURL, action: fritzSIPWriteAction, arguments: args, secondFactorToken: secondFactorToken)
                    } catch {
                        guard isSecondFactorRequired(error) else { throw error }
                        secondFactorToken = try await beginFritzSecondFactor(session: session, base: base, descriptionXML: descriptionXML)
                        _ = try await soapCall(session: session, base: base, serviceType: voipService.type, controlURL: voipService.controlURL, action: fritzSIPWriteAction, arguments: args, secondFactorToken: secondFactorToken)
                    }
                } catch {
                    SetupKeychain.delete(account: "fritz-sip-callwhapp1")
                    throw error
                }
            }

            if sipLine2Enabled && (client2 == nil || secret2Missing) {
                let password = randomSIPPassword()
                try SetupKeychain.set(password, account: "fritz-sip-callwhapp2")
                do {
                    let targetIndex = client2?.index ?? sipClient2Index!
                    let number2 = line2Number.isEmpty ? line1Number : line2Number
                    let args = try setClientArguments(index: targetIndex, username: "callwhapp2", password: password, outgoing: number2)
                    do {
                        _ = try await soapCall(session: session, base: base, serviceType: voipService.type, controlURL: voipService.controlURL, action: fritzSIPWriteAction, arguments: args, secondFactorToken: secondFactorToken)
                    } catch {
                        guard isSecondFactorRequired(error) else { throw error }
                        secondFactorToken = try await beginFritzSecondFactor(session: session, base: base, descriptionXML: descriptionXML)
                        _ = try await soapCall(session: session, base: base, serviceType: voipService.type, controlURL: voipService.controlURL, action: fritzSIPWriteAction, arguments: args, secondFactorToken: secondFactorToken)
                    }
                } catch {
                    SetupKeychain.delete(account: "fritz-sip-callwhapp2")
                    throw error
                }
            }

            if sipLine3Enabled && (client3 == nil || secret3Missing) {
                guard let plannedIndex3 = sipClient3Index else {
                    throw NSError(domain: "CallWebhook.Setup", code: 3, userInfo: [NSLocalizedDescriptionKey: "Kein sicherer Clientplatz für Leitung 3"])
                }
                let password = randomSIPPassword()
                try SetupKeychain.set(password, account: "fritz-sip-callwhapp3")
                do {
                    let targetIndex = client3?.index ?? plannedIndex3
                    let args = try setClientArguments(index: targetIndex, username: "callwhapp3", password: password, outgoing: line3Number)
                    do {
                        _ = try await soapCall(session: session, base: base, serviceType: voipService.type, controlURL: voipService.controlURL, action: fritzSIPWriteAction, arguments: args, secondFactorToken: secondFactorToken)
                    } catch {
                        guard isSecondFactorRequired(error) else { throw error }
                        secondFactorToken = try await beginFritzSecondFactor(session: session, base: base, descriptionXML: descriptionXML)
                        _ = try await soapCall(session: session, base: base, serviceType: voipService.type, controlURL: voipService.controlURL, action: fritzSIPWriteAction, arguments: args, secondFactorToken: secondFactorToken)
                    }
                } catch {
                    SetupKeychain.delete(account: "fritz-sip-callwhapp3")
                    throw error
                }
            }

            // TAM provisioning has its own step-one path, including when SIP
            // clients already exist. Exact number assignments are saved later.

            await checkFritzBox()

            guard fritzSIPVerified else {
                sipProvisionStatus = "FRITZ-SIP wurde geschrieben. " + fritzSIPSummary
                return
            }
            sipProvisionStatus = fritzSIPSummary
        } catch {
            sipProvisionStatus = "Provisionierung abgebrochen: \(error.localizedDescription)"
        }
    }

    @MainActor
    private func beginFritzSecondFactor(session: URLSession, base: String, descriptionXML: String) async throws -> String {
        guard let authService = extractTR064Services(from: descriptionXML).first(where: {
            $0.type.localizedCaseInsensitiveContains("X_AVM-DE_Auth")
        }) else {
            throw NSError(domain: "CallWebhook.TR064", code: 866, userInfo: [NSLocalizedDescriptionKey: "FRITZ!Box verlangt eine Bestätigung, bietet aber X_AVM-DE_Auth nicht an"])
        }

        let start = try await soapCall(session: session, base: base, serviceType: authService.type, controlURL: authService.controlURL, action: "SetConfig", arguments: [("NewAction", "start")])
        let token = extractSOAPValue("NewToken", from: start)
        let methods = extractSOAPValue("NewMethods", from: start)
        guard !token.isEmpty else {
            throw NSError(domain: "CallWebhook.TR064", code: 866, userInfo: [NSLocalizedDescriptionKey: "FRITZ!Box hat keinen 2FA-Token geliefert"])
        }

        let startState = extractSOAPValue("NewState", from: start).lowercased()
        if startState == "authenticated" { return token }
        guard startState == "waitingforauth" else {
            throw NSError(domain: "CallWebhook.TR064", code: 866,
                userInfo: [NSLocalizedDescriptionKey: "FRITZ!Box kann die Bestätigung gerade nicht starten: \(startState)"])
        }
        let choices = FritzConfirmationMethods.tr064(methods)
        guard !choices.methods.isEmpty else {
            throw NSError(domain: "CallWebhook.TR064", code: 866,
                userInfo: [NSLocalizedDescriptionKey: "FRITZ!Box bietet für diese SIP-Änderung keinen unterstützten Bestätigungsweg an"])
        }
        fritzConfirmation.begin(id: token, methods: choices.methods, phoneCode: choices.phoneCode, backend: false)
        defer { fritzConfirmation.finish() }
        sipProvisionStatus = "FRITZ!Box-Bestätigung: Bestätigungsweg im Dialog wählen …"

        for _ in 0..<240 {
            try await Task.sleep(for: .milliseconds(500))
            if fritzConfirmation.cancelled {
                _ = try await soapCall(session: session, base: base, serviceType: authService.type,
                    controlURL: authService.controlURL, action: "SetConfig", arguments: [("NewAction", "stop")], secondFactorToken: token)
                throw NSError(domain: "CallWebhook.TR064", code: 866,
                    userInfo: [NSLocalizedDescriptionKey: "FRITZ!Box-Bestätigung abgebrochen"])
            }
            let stateXML = try await soapCall(session: session, base: base, serviceType: authService.type, controlURL: authService.controlURL, action: "GetState", arguments: [], secondFactorToken: token)
            let state = extractSOAPValue("NewState", from: stateXML).lowercased()
            if state == "authenticated" {
                sipProvisionStatus = "FRITZ!Box bestätigt – SIP-Nebenstellen werden eingerichtet …"
                return token
            }
            if !state.isEmpty && state != "waitingforauth" {
                throw NSError(domain: "CallWebhook.TR064", code: 866, userInfo: [NSLocalizedDescriptionKey: "FRITZ!Box-Bestätigung beendet: \(state)"])
            }
        }
        throw NSError(domain: "CallWebhook.TR064", code: 866, userInfo: [NSLocalizedDescriptionKey: "Zeitüberschreitung bei der FRITZ!Box-Bestätigung"])
    }

    private func isSecondFactorRequired(_ error: Error) -> Bool {
        error.localizedDescription.contains("AVM 866")
    }

    private func setClientArguments(index: Int, username: String, password: String, outgoing: String) throws -> [(String, String)] {
        guard outgoing.filter({ $0.isNumber }).count >= 3 else {
            throw NSError(domain: "CallWebhook.Setup", code: 4, userInfo: [NSLocalizedDescriptionKey: "Bitte eine echte Festnetzrufnummer auswählen. Ein Leitungsindex ist keine Rufnummer."])
        }
        guard let incomingXML = fritzNumberAssignments[outgoing] else {
            throw NSError(domain: "CallWebhook.Setup", code: 5, userInfo: [NSLocalizedDescriptionKey: "FRITZ!Box-Rufnummer mit Typ und Index fehlt. Bitte die Rufnummernliste erneut auslesen und eine erkannte Nummer auswählen."])
        }
        let effectiveOutgoing = outgoing
        var values: [String: String] = [
            "NewX_AVM-DE_ClientIndex": String(index),
            "NewX_AVM-DE_ClientUsername": username,
            "NewX_AVM-DE_ClientPassword": password,
            "NewX_AVM-DE_PhoneName": username,
            "NewX_AVM-DE_OutGoingNumber": effectiveOutgoing,
            "NewX_AVM-DE_InComingNumbers": incomingXML,
            "NewX_AVM-DE_ClientId": ""
        ]
        values["NewX_AVM-DE_ClientID"] = ""
        values["NewX_AVM-DE_ExternalRegistration"] = "0"

        var result: [(String, String)] = []
        for argument in fritzSIPWriteArguments {
            guard let value = values[argument] else {
                throw NSError(domain: "CallWebhook.Setup", code: 1, userInfo: [NSLocalizedDescriptionKey: "Unbekanntes FRITZ-Argument \(argument)"])
            }
            result.append((argument, value))
        }
        guard !result.isEmpty else {
            throw NSError(domain: "CallWebhook.Setup", code: 2, userInfo: [NSLocalizedDescriptionKey: "Keine SetClient-Argumente ausgelesen"])
        }
        return result
    }

    private func randomSIPPassword() -> String {
        let alphabet = Array("ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789")
        var generator = SystemRandomNumberGenerator()
        return String((0..<24).compactMap { _ in alphabet.randomElement(using: &generator) })
    }

    private func actionArgumentNames(_ action: String, in scpd: String) -> [String] {
        let escaped = NSRegularExpression.escapedPattern(for: action)
        let pattern = "<action>\\s*<name>\\s*\(escaped)\\s*</name>([\\s\\S]*?)</action>"
        guard let regex = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]),
              let match = regex.firstMatch(in: scpd, range: NSRange(scpd.startIndex..<scpd.endIndex, in: scpd)),
              match.numberOfRanges > 1,
              let bodyRange = Range(match.range(at: 1), in: scpd) else { return [] }
        let body = String(scpd[bodyRange])
        let argumentPattern = "<argument>[\\s\\S]*?<name>\\s*([^<]+)\\s*</name>[\\s\\S]*?<direction>\\s*in\\s*</direction>[\\s\\S]*?</argument>"
        guard let argumentRegex = try? NSRegularExpression(pattern: argumentPattern, options: [.caseInsensitive]) else { return [] }
        let range = NSRange(body.startIndex..<body.endIndex, in: body)
        return argumentRegex.matches(in: body, range: range).compactMap { item in
            guard item.numberOfRanges > 1,
                  let nameRange = Range(item.range(at: 1), in: body) else { return nil }
            return String(body[nameRange]).trimmingCharacters(in: .whitespacesAndNewlines)
        }
    }

    private func extractSOAPValue(_ tag: String, from xml: String) -> String {
        firstXMLValue(tag, in: xml) ?? ""
    }

    private func soapCall(
        session: URLSession,
        base: String,
        serviceType: String,
        controlURL: String,
        action: String,
        arguments: [(String, String)],
        secondFactorToken: String? = nil
    ) async throws -> String {
        let normalizedBase = base.hasSuffix("/") ? String(base.dropLast()) : base
        let path = controlURL.hasPrefix("/") ? controlURL : "/" + controlURL
        guard let url = URL(string: normalizedBase + path) else {
            throw URLError(.badURL)
        }

        let argsXML = arguments.map { "<\($0.0)>\(xmlEscaped($0.1))</\($0.0)>" }.joined()
        let tokenHeader = secondFactorToken.map {
            "<s:Header><avm:token xmlns:avm=\"avm.de\" s:mustUnderstand=\"1\">\(xmlEscaped($0))</avm:token></s:Header>"
        } ?? ""
        let envelope = """
        <?xml version="1.0" encoding="utf-8"?>
        <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
          \(tokenHeader)
          <s:Body>
            <u:\(action) xmlns:u="\(serviceType)">\(argsXML)</u:\(action)>
          </s:Body>
        </s:Envelope>
        """

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 8
        request.httpBody = envelope.data(using: .utf8)
        request.setValue("text/xml; charset=utf-8", forHTTPHeaderField: "Content-Type")
        request.setValue("\(serviceType)#\(action)", forHTTPHeaderField: "SOAPAction")

        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw URLError(.badServerResponse) }
        guard (200..<300).contains(http.statusCode) else {
            let responseText = String(data: data, encoding: .utf8) ?? ""
            let errorCode = firstXMLValue("errorCode", in: responseText) ?? "?"
            let errorDescription = firstXMLValue("errorDescription", in: responseText) ?? "keine Beschreibung"
            let argumentNames = arguments.map(\.0).joined(separator: ", ")
            throw NSError(
                domain: "CallWebhook.TR064",
                code: http.statusCode,
                userInfo: [
                    NSLocalizedDescriptionKey:
                        "TR-064 HTTP \(http.statusCode) · AVM \(errorCode): \(errorDescription) · \(action) [\(argumentNames)]"
                ]
            )
        }
        guard let text = String(data: data, encoding: .utf8) else { throw URLError(.cannotDecodeContentData) }
        return text
    }

    private func xmlEscaped(_ value: String) -> String {
        value
            .replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")
            .replacingOccurrences(of: "\"", with: "&quot;")
            .replacingOccurrences(of: "'", with: "&apos;")
    }

    private func extractTR064ServiceTypes(from xml: String) -> [String] {
        let pattern = "<serviceType>\\s*([^<]+)\\s*</serviceType>"
        guard let regex = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]) else { return [] }
        let range = NSRange(xml.startIndex..<xml.endIndex, in: xml)
        return regex.matches(in: xml, range: range).compactMap { match in
            guard match.numberOfRanges > 1,
                  let valueRange = Range(match.range(at: 1), in: xml) else { return nil }
            return String(xml[valueRange]).trimmingCharacters(in: .whitespacesAndNewlines)
        }
    }

    @MainActor
    private func authenticateHomeAssistant() async {
        let input = homeAssistantURL.trimmingCharacters(in: .whitespacesAndNewlines)
        let raw = "http://192.168.178.\(input):8123"
        guard let base = URL(string: raw) else {
            homeAssistantStatus = "Ungültige Home-Assistant-Adresse"
            return
        }
        isAuthenticatingHA = true
        defer { isAuthenticatingHA = false }
        do {
            let token = try await HomeAssistantAuth.shared.authenticate(instance: base)
            setupHAToken = token
            haAuthenticated = true
            homeAssistantStatus = "Home Assistant autorisiert"
            await prepareConnectedHomeAssistant(base: base)
        } catch {
            haAuthenticated = false
            callWebhookHAReady = false
            homeAssistantStatus = "Anmeldung fehlgeschlagen: \(error.localizedDescription)"
        }
    }

    @MainActor
    private func checkHomeAssistant() async {
        isChecking = true
        defer { isChecking = false }
        homeAssistantReachable = false
        callWebhookHAReady = false
        callWebhookHAStatus = "CallWebhook-Integration noch nicht geprüft"
        homeAssistantStatus = "Home Assistant wird geprüft …"

        let input = homeAssistantURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !input.isEmpty else {
            homeAssistantStatus = "Adresse fehlt"
            return
        }
        let raw = "http://192.168.178.\(input):8123"
        guard let base = URL(string: raw),
              let url = URL(string: "/manifest.json", relativeTo: base)?.absoluteURL else {
            homeAssistantStatus = "Ungültige Home-Assistant-Adresse"
            return
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 8
        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, (200..<400).contains(http.statusCode) else {
                homeAssistantStatus = "Home Assistant antwortet nicht wie erwartet"
                return
            }
            homeAssistantReachable = true
            homeAssistantStatus = "Home Assistant erreichbar"
            await prepareConnectedHomeAssistant(base: base)
        } catch {
            homeAssistantStatus = "Lokalen Netzwerkzugriff bestätigen – prüfe automatisch erneut …"
            for _ in 0..<5 {
                try? await Task.sleep(nanoseconds: 1_000_000_000)
                do {
                    let (_, retryResponse) = try await URLSession.shared.data(for: request)
                    if let retryHTTP = retryResponse as? HTTPURLResponse, (200..<400).contains(retryHTTP.statusCode) {
                        homeAssistantReachable = true
                        homeAssistantStatus = "Home Assistant erreichbar"
                        await prepareConnectedHomeAssistant(base: base)
                        return
                    }
                } catch {
                    continue
                }
            }
            homeAssistantStatus = "Home Assistant nicht erreichbar – lokalen Netzwerkzugriff prüfen"
        }
    }

    @MainActor
    private func prepareConnectedHomeAssistant(base: URL) async {
        guard !isBootstrappingHA, !isWaitingForHARestart else { return }
        if step < 4 {
            do {
                let (_, status) = try await HomeAssistantConnection.request(base: base, path: "api/")
                haAuthenticated = status == 200
                if haAuthenticated {
                    homeAssistantReachable = true
                    homeAssistantStatus = "Home Assistant verbunden"
                    UserDefaults.standard.set(homeAssistantURL, forKey: "setupHomeAssistantURL")
                    if step == 1 { step = 2 }
                } else { homeAssistantStatus = "Bitte mit Home Assistant anmelden" }
            } catch {
                haAuthenticated = false
                homeAssistantStatus = "Bitte mit Home Assistant anmelden"
            }
            return
        }
        await checkCallWebhookHAIntegration(base: base)
        // Install only after a confirmed missing/old backend, never after a
        // network/authentication error. The backend check has released its lock.
        if haAuthenticated && bootstrapInstallationNeeded {
            await installBootstrapAutomatically()
        }
    }

    @MainActor
    private func checkCallWebhookHAIntegration(base: URL) async {
        guard !isCheckingBackend else { return }
        isCheckingBackend = true
        bootstrapInstallationNeeded = false
        defer { isCheckingBackend = false }
        callWebhookHAReady = false
        guard let url = URL(string: "/api/callwebhook/setup/status", relativeTo: base)?.absoluteURL else {
            callWebhookHAStatus = "CallWebhook-Endpunkt konnte nicht gebildet werden"
            return
        }
        guard var token = SetupKeychain.get(account: "home-assistant-token"), !token.isEmpty else {
            callWebhookHAStatus = "Home Assistant noch nicht autorisiert"
            haAuthenticated = false
            return
        }

        do {
            var request = URLRequest(url: url)
            request.timeoutInterval = 8
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            var (data, response) = try await URLSession.shared.data(for: request)
            var http = response as? HTTPURLResponse

            if http?.statusCode == 401 {
                token = try await HomeAssistantAuth.shared.refresh(instance: base)
                setupHAToken = token
                request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
                (data, response) = try await URLSession.shared.data(for: request)
                http = response as? HTTPURLResponse
            }

            guard let http else {
                callWebhookHAStatus = "CallWebhook-Integration antwortet nicht"
                return
            }
            if http.statusCode == 404 {
                // Validate the stored authorization independently: an absent
                // custom route can return 404 even for an expired token.
                var authRequest = URLRequest(url: base.appendingPathComponent("api/"))
                authRequest.timeoutInterval = 8
                authRequest.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
                let (_, authResponse) = try await URLSession.shared.data(for: authRequest)
                guard (authResponse as? HTTPURLResponse)?.statusCode == 200 else {
                    haAuthenticated = false
                    callWebhookHAStatus = "Home Assistant erneut verbinden"
                    return
                }
                haAuthenticated = true
                bootstrapInstallationNeeded = true
                callWebhookHAStatus = "CallWebhook-HA-Integration fehlt – Bootstrap erforderlich"
                return
            }
            if http.statusCode == 401 {
                haAuthenticated = false
                callWebhookHAStatus = "Home-Assistant-Autorisierung abgelaufen – erneut verbinden"
                return
            }
            guard (200..<300).contains(http.statusCode),
                  let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                  (json["ok"] as? Bool) == true else {
                callWebhookHAStatus = "CallWebhook-Integration nicht bereit (HTTP \(http.statusCode))"
                return
            }
            let version = json["api_version"] as? Int ?? 0
            guard version >= 18,
                  (json["asterisk_provisioning"] as? Bool) == true else {
                haAuthenticated = true
                bootstrapInstallationNeeded = true
                callWebhookHAStatus = "CallWebhook-Backend veraltet – Update wird automatisch gestartet"
                return
            }
            setupHAToken = token
            haAuthenticated = true
            if let previous = bootstrapPreviousBootID, json["boot_id"] as? String == previous {
                callWebhookHAStatus = "Warte auf den neuen Home-Assistant-Start …"
                return
            }
            guard json["ready_for_asterisk"] as? Bool == true else {
                callWebhookHAStatus = json["message"] as? String ?? "Home Assistant und Bootstrap werden noch vorbereitet …"
                return
            }
            bootstrapPreviousBootID = nil
            bootstrapRestartPending = false
            callWebhookHAReady = true
            bootstrapProgressStep = bootstrapProgressTotal
            callWebhookHAStatus = "CallWebhook-Backend bereit (API \(version))"
            homeAssistantReachable = true
            homeAssistantStatus = "Home Assistant vollständig gestartet und erreichbar"
            await continueHASetup()
        } catch {
            callWebhookHAStatus = "CallWebhook-Prüfung fehlgeschlagen: \(error.localizedDescription)"
        }
    }

    @MainActor
    private func synchronizeFritzCredentials() async throws {
        guard !fritzUser.isEmpty, !fritzPassword.isEmpty,
              let base = URL(string: "http://192.168.178.\(homeAssistantURL.trimmingCharacters(in: .whitespacesAndNewlines)):8123") else {
            throw NSError(domain: "CallWebhook.Setup", code: 1, userInfo: [NSLocalizedDescriptionKey: "FRITZ!Box-Zugangsdaten im ersten Schritt eingeben"])
        }
        let (data, status) = try await HomeAssistantConnection.request(base: base,
            path: "api/callwebhook/setup/fritz-credentials", method: "POST",
            body: ["username": fritzUser, "password": fritzPassword])
        let result = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        guard status == 200, result?["ok"] as? Bool == true else {
            throw NSError(domain: "CallWebhook.Setup", code: status, userInfo: [NSLocalizedDescriptionKey:
                result?["error"] as? String ?? "FRITZ!Box-Zugangsdaten konnten nicht an Home Assistant übertragen werden"])
        }
    }

    @MainActor
    private func continueHASetup() async {
        // Both an existing backend and a newly bootstrapped backend enter here.
        // Readiness of the backend alone never means the whole HA step is done.
        guard step == 4, callWebhookHAReady, haAuthenticated,
              !isContinuingHASetup, !isInstallingAsterisk, !isCreatingCallHelper else { return }
        isContinuingHASetup = true
        defer { isContinuingHASetup = false }
        do {
            callWebhookHAStatus = "FRITZ!Box-Zugangsdaten werden in Home Assistant gespeichert …"
            try await synchronizeFritzCredentials()
        } catch {
            callWebhookHAStatus = error.localizedDescription
            return
        }

        if !mailboxNumbersVerified {
            guard await saveMailboxSelection() else { return }
        }
        if !asteriskInstalled {
            asteriskInstallFailed = false
            if !asteriskConfigReady { prepareAsteriskConfiguration() }
            guard asteriskConfigReady else {
                asteriskInstallFailed = true
                return
            }
            await installAsteriskConfiguration()
        }
        guard asteriskInstalled else { return }
        if !callHelperReady { await ensureCallHelper() }
        guard callHelperReady, setupSIP.registered else { return }
        callWebhookHAStatus = "CallWebhook, Asterisk und Anrufstatus-Schalter bereit"
        if step == 4 {
            step = 5
            await setupPush.completeSetup()
        }
    }

    @MainActor
    private func waitForCallWebhookAfterRestart() async {
        guard !isWaitingForHARestart else { return }
        isWaitingForHARestart = true
        defer { isWaitingForHARestart = false; bootstrapRestartPending = false }
        bootstrapRestartPending = true
        homeAssistantReachable = false
        callWebhookHAReady = false
        callWebhookHAStatus = "Home Assistant startet neu … Warte auf vollständige Bereitschaft."

        let input = homeAssistantURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !input.isEmpty, let base = URL(string: "http://192.168.178.\(input):8123") else {
            callWebhookHAStatus = "Home-Assistant-Adresse ist ungültig"
            return
        }

        let restartDeadline = Date().addingTimeInterval(600)
        while Date() < restartDeadline {
            do {
                try await Task.sleep(nanoseconds: 500_000_000)
                try Task.checkCancellation()
                await checkCallWebhookHAIntegration(base: base)
                // The successful readiness check already continues Asterisk and
                // helper setup, including the pre-existing-backend path.
                if callWebhookHAReady { return }
                if !haAuthenticated { return }
            } catch {
                if Task.isCancelled {
                    callWebhookHAStatus = "Warten auf Home Assistant abgebrochen – Einrichtung erneut prüfen"
                    return
                }
            }
        }
        callWebhookHAStatus = "Home Assistant ist noch nicht bereit – automatische Einrichtung erneut prüfen"
    }

    private let bootstrapRepository = "https://github.com/oooonoooorenoooo/-CallWebhook"

    private func resolveBootstrapSupervisorSlug(base: URL, token: String) async throws -> String {
        let response = try await supervisorWrite(base: base, token: token, endpoint: "/store", method: "get")
        let store = response["result"] as? [String: Any] ?? [:]
        let repositories = store["repositories"] as? [[String: Any]] ?? []
        let repositorySlugs = Set(repositories.compactMap { item -> String? in
            let source = (item["source"] as? String ?? "").trimmingCharacters(in: CharacterSet(charactersIn: "/"))
            guard source == bootstrapRepository || source == bootstrapRepository + ".git" else { return nil }
            return item["slug"] as? String
        })
        let addons = (store["addons"] as? [[String: Any]]) ?? (store["apps"] as? [[String: Any]]) ?? []
        guard let addon = addons.first(where: { item in
            guard let slug = item["slug"] as? String,
                  let repository = item["repository"] as? String else { return false }
            return repositorySlugs.contains(repository) && slug == repository + "_callwebhook_bootstrap"
        }), let slug = addon["slug"] as? String else {
            throw NSError(domain: "CallWebhook.Bootstrap", code: 404, userInfo: [NSLocalizedDescriptionKey: "CallWebhook Bootstrap ist im registrierten Repository noch nicht verfügbar"])
        }
        return slug
    }

    private func supervisorWrite(base: URL, token: String, endpoint: String, method: String = "post", data: [String: Any] = [:]) async throws -> [String: Any] {
        var components = URLComponents(url: base, resolvingAgainstBaseURL: false)
        components?.scheme = base.scheme == "https" ? "wss" : "ws"
        components?.path = "/api/websocket"
        components?.query = nil
        guard let wsURL = components?.url else { throw URLError(.badURL) }
        let task = URLSession.shared.webSocketTask(with: wsURL)
        task.resume()
        // URLSession receive() has no per-message deadline. Close the socket to
        // unblock authentication/receive if HA restarts or the network disappears.
        let requestTimeout: Double = method.lowercased() == "get" ? 25 : 120
        let timeout = Task {
            try await Task.sleep(nanoseconds: UInt64(requestTimeout + 5) * 1_000_000_000)
            task.cancel(with: .goingAway, reason: nil)
        }
        defer { timeout.cancel(); task.cancel(with: .goingAway, reason: nil) }
        func receiveJSON() async throws -> [String: Any] {
            let message = try await task.receive()
            let raw: Data
            switch message {
            case .string(let value): raw = Data(value.utf8)
            case .data(let value): raw = value
            @unknown default: throw URLError(.cannotDecodeContentData)
            }
            guard let json = try JSONSerialization.jsonObject(with: raw) as? [String: Any] else { throw URLError(.cannotDecodeContentData) }
            return json
        }
        func sendJSON(_ value: [String: Any]) async throws {
            let bytes = try JSONSerialization.data(withJSONObject: value)
            // Home Assistant's WebSocket API requires JSON TEXT frames.
            try await task.send(.string(String(decoding: bytes, as: UTF8.self)))
        }
        guard try await receiveJSON()["type"] as? String == "auth_required" else { throw URLError(.userAuthenticationRequired) }
        try await sendJSON(["type": "auth", "access_token": token])
        guard try await receiveJSON()["type"] as? String == "auth_ok" else { throw URLError(.userAuthenticationRequired) }
        var command: [String: Any] = ["id": 1, "type": "supervisor/api", "endpoint": endpoint, "method": method.lowercased(), "timeout": requestTimeout]
        if !data.isEmpty { command["data"] = data }
        try await sendJSON(command)
        while true {
            let response = try await receiveJSON()
            guard response["id"] as? Int == 1 else { continue }
            guard (response["success"] as? Bool) == true else {
                let error = response["error"] as? [String: Any]
                throw NSError(domain: "CallWebhook.Supervisor", code: 1, userInfo: [NSLocalizedDescriptionKey: error?["message"] as? String ?? "Supervisor-Anfrage fehlgeschlagen: \(endpoint)"])
            }
            return response
        }
    }

    @MainActor
    private func installBootstrapAutomatically() async {
        guard step == 4, !isBootstrappingHA, !isWaitingForHARestart else { return }
        isBootstrappingHA = true
        bootstrapAttemptFailed = false
        defer {
            isBootstrappingHA = false
            bootstrapAttemptFailed = !callWebhookHAReady
        }
        let input = homeAssistantURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let base = URL(string: "http://192.168.178.\(input):8123"),
              let savedToken = SetupKeychain.get(account: "home-assistant-token"), !savedToken.isEmpty else {
            callWebhookHAStatus = "Home Assistant muss zuerst verbunden und autorisiert sein"
            return
        }
        do {
            let token = (try? await HomeAssistantAuth.shared.refresh(instance: base)) ?? savedToken
            bootstrapProgressStep = 0
            bootstrapRestartPending = false
            callWebhookHAReady = false
            callWebhookHAStatus = "Bootstrap-Repository wird geprüft …"
            let response = try await supervisorWrite(base: base, token: token, endpoint: "/store", method: "get")
            let store = response["result"] as? [String: Any] ?? [:]
            let repositories = store["repositories"] as? [[String: Any]] ?? []
            if !repositories.contains(where: {
                let source = ($0["source"] as? String ?? "").trimmingCharacters(in: CharacterSet(charactersIn: "/"))
                return source == bootstrapRepository || source == bootstrapRepository + ".git"
            }) {
                _ = try await supervisorWrite(base: base, token: token, endpoint: "/store/repositories", data: ["repository": bootstrapRepository])
            }
            bootstrapProgressStep = 1
            callWebhookHAStatus = "Bootstrap-Repository bestätigt – Store wird aktualisiert …"
            _ = try await supervisorWrite(base: base, token: token, endpoint: "/store/reload")
            var slug: String?
            let discoveryDeadline = Date().addingTimeInterval(90)
            while Date() < discoveryDeadline {
                try Task.checkCancellation()
                do { slug = try await resolveBootstrapSupervisorSlug(base: base, token: token); break }
                catch let error as NSError where error.domain == "CallWebhook.Bootstrap" && error.code == 404 {
                    try await Task.sleep(nanoseconds: 500_000_000)
                }
            }
            guard let slug else { throw NSError(domain: "CallWebhook.Bootstrap", code: 404, userInfo: [NSLocalizedDescriptionKey: "Bootstrap im Repository noch nicht gefunden. Automatische Einrichtung erneut versuchen."] ) }
            bootstrapProgressStep = 2
            let storeResponse = try await supervisorWrite(base: base, token: token, endpoint: "/store/addons/\(slug)", method: "get")
            let info = storeResponse["result"] as? [String: Any] ?? [:]
            let installed = (info["installed"] as? Bool) == true || !(info["installed"] as? String ?? "").isEmpty
            if !installed || (info["update_available"] as? Bool) == true {
                callWebhookHAStatus = installed ? "Bootstrap wird aktualisiert …" : "Bootstrap wird installiert …"
                let action = installed ? "update" : "install"
                _ = try await supervisorWrite(base: base, token: token, endpoint: "/store/addons/\(slug)/\(action)", data: ["background": true])
                let deadline = Date().addingTimeInterval(600)
                var ready = false
                while Date() < deadline {
                    try await Task.sleep(nanoseconds: 500_000_000)
                    let check = try await supervisorWrite(base: base, token: token, endpoint: "/store/addons/\(slug)", method: "get")
                    let current = check["result"] as? [String: Any] ?? [:]
                    let present = (current["installed"] as? Bool) == true || !(current["installed"] as? String ?? "").isEmpty
                    if present && (current["update_available"] as? Bool) != true { ready = true; break }
                }
                guard ready else { throw URLError(.timedOut) }
            }
            bootstrapProgressStep = 3
            callWebhookHAStatus = "Bootstrap installiert – wird gestartet …"
            let stateResponse = try await supervisorWrite(base: base, token: token, endpoint: "/addons/\(slug)/info", method: "get")
            let state = stateResponse["result"] as? [String: Any] ?? [:]
            if state["state"] as? String != "started" {
                bootstrapPreviousBootID = nil
                var statusRequest = URLRequest(url: base.appendingPathComponent("api/callwebhook/setup/status"))
                statusRequest.timeoutInterval = 8
                statusRequest.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
                if let (bytes, _) = try? await URLSession.shared.data(for: statusRequest),
                   let status = try? JSONSerialization.jsonObject(with: bytes) as? [String: Any] {
                    bootstrapPreviousBootID = status["boot_id"] as? String
                }
                // Announce the restart before sending start: HA can disappear
                // while the Supervisor response is still in flight.
                bootstrapRestartPending = true
                homeAssistantReachable = false
                callWebhookHAReady = false
                callWebhookHAStatus = "Home Assistant startet neu … Warte auf vollständige Bereitschaft."
                _ = try await supervisorWrite(base: base, token: token, endpoint: "/addons/\(slug)/start")
            }
            bootstrapProgressStep = 4
            await waitForCallWebhookAfterRestart()
        } catch {
            if bootstrapRestartPending, (error as NSError).domain == NSURLErrorDomain {
                // A lost connection after /start is expected during the reboot;
                // the authenticated readiness check decides whether it succeeded.
                bootstrapProgressStep = 4
                await waitForCallWebhookAfterRestart()
                return
            }
            bootstrapRestartPending = false
            callWebhookHAStatus = "Bootstrap-Einrichtung fehlgeschlagen: \(error.localizedDescription). Bitte die automatische Einrichtung erneut versuchen."
        }
    }

    private func persistSetup(completed: Bool = true) {
        let defaults = UserDefaults.standard
        defaults.set(fritzHost, forKey: "setupFritzHost")
        defaults.set(fritzUser, forKey: "setupFritzUser")
        defaults.set(homeAssistantURL, forKey: "setupHomeAssistantURL")
        defaults.set(easybellEnabled, forKey: "setupEasybellEnabled")
        defaults.set(line1Label, forKey: "sipLine1Label")
        defaults.set(line2Label, forKey: "sipLine2Label")
        defaults.set(line3Label, forKey: "sipLine3Label")
        defaults.set(line1Number, forKey: "sipLine1Number")
        defaults.set(line2Number, forKey: "sipLine2Number")
        defaults.set(line3Number, forKey: "sipLine3Number")
        defaults.set(mailbox1TAM, forKey: "setupMailbox1TAM")
        defaults.set(mailbox2TAM, forKey: "setupMailbox2TAM")
        defaults.set(mailbox3TAM, forKey: "setupMailbox3TAM")
        // Save completion with the configuration, before dismissing the wizard.
        if completed {
            defaults.set(true, forKey: "phoneDefaultsReviewed")
            defaults.set(true, forKey: "setupCompleted")
        }
        // Passwords are intentionally not persisted in UserDefaults.
    }
}

private struct CallsView: View {
    @Environment(\.scenePhase) private var scenePhase
    @EnvironmentObject var monitor: CallMonitor
    @ObservedObject var dialer: DialerModel
    @StateObject private var history = CallHistoryModel()
    @State private var selection = 0
    @State private var searchText = ""
    @State private var isSelectingCalls = false
    @State private var selectedCallIDs: Set<UUID> = []
    @AppStorage("hiddenCallIDs") private var hiddenCallIDs = ""
    @AppStorage("hiddenCallRecords") private var hiddenCallRecords = Data()

    private var hiddenRecords: [CallRecord] {
        (try? JSONDecoder().decode([CallRecord].self, from: hiddenCallRecords)) ?? []
    }

    private var hiddenIDs: Set<String> {
        Set(hiddenCallIDs.split(separator: "\n").map(String.init))
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                Picker("Ansicht", selection: $selection) {
                    Text("Anrufe").tag(0)
                    Text("HA-Status").tag(1)
                }
                .pickerStyle(.segmented)
                .padding()

                if selection == 0 {
                    if let error = history.errorMessage {
                        Text(error).font(.caption).foregroundStyle(.secondary).padding(.horizontal)
                    }
                    if filteredCalls.isEmpty {
                        ContentUnavailableView(searchText.isEmpty ? "Keine Anrufe" : "Keine Treffer", systemImage: "phone", description: Text("Deine eingehenden, ausgehenden und verpassten Anrufe erscheinen hier."))
                    } else {
                        List(filteredCalls) { call in
                            Button {
                                if isSelectingCalls {
                                    if selectedCallIDs.contains(call.id) {
                                        selectedCallIDs.remove(call.id)
                                    } else {
                                        selectedCallIDs.insert(call.id)
                                    }
                                } else {
                                    guard let number = call.handles.first?.value, !number.isEmpty else { return }
                                    dialer.call(number)
                                }
                            } label: {
                                HStack(spacing: 12) {
                                    if isSelectingCalls {
                                        Image(systemName: selectedCallIDs.contains(call.id) ? "checkmark.circle.fill" : "circle")
                                            .foregroundStyle(selectedCallIDs.contains(call.id) ? .blue : .secondary)
                                    }
                                    Image(systemName: directionIcon(call))
                                        .foregroundStyle(callStatusColor(call))
                                        .frame(width: 28)
                                    VStack(alignment: .leading, spacing: 3) {
                                        Text(call.handles.first?.value ?? "Unbekannt").font(.headline)
                                        Text(call.lineLabel).font(.caption).foregroundStyle(.secondary)
                                    }
                                    Spacer()
                                    VStack(alignment: .trailing, spacing: 3) {
                                        Text(call.date, style: .date).font(.caption)
                                        Text(call.date, style: .time).font(.caption2).foregroundStyle(.secondary)
                                        if call.duration > 0 {
                                            Text("\(Int(call.duration) / 60):\(String(format: "%02d", Int(call.duration) % 60))")
                                                .font(.caption2).foregroundStyle(.secondary)
                                        }
                                    }
                                    Image(systemName: "phone.fill")
                                        .foregroundStyle(.green)
                                }
                            }
                            .buttonStyle(.plain)
                            .onLongPressGesture {
                                guard !isSelectingCalls,
                                      let number = call.handles.first?.value,
                                      !number.isEmpty else { return }
                                UIPasteboard.general.string = number
                            }
                            .disabled(!isSelectingCalls && (call.handles.first?.value ?? "").isEmpty)
                        }
                        .listStyle(.plain)
                        .safeAreaInset(edge: .bottom) {
                            if isSelectingCalls {
                                HStack {
                                    Button {
                                        shareSelectedCalls()
                                    } label: {
                                        Label("Teilen", systemImage: "square.and.arrow.up")
                                    }
                                    .disabled(selectedCallIDs.isEmpty)
                                    Spacer()
                                    Button(role: .destructive) {
                                        hideSelectedCalls()
                                    } label: {
                                        Label("Löschen", systemImage: "trash")
                                    }
                                    .disabled(selectedCallIDs.isEmpty)
                                    Spacer()
                                    Text("\(selectedCallIDs.count) ausgewählt")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                                .padding()
                                .background(.bar)
                            }
                        }
                        .refreshable { await history.refresh() }
                    }
                } else {
                    List(monitor.log, id: \.self) { entry in
                        Label(entry, systemImage: "house.fill").font(.callout)
                    }
                    .listStyle(.plain)
                }
            }
            .navigationTitle("Anrufe")
            .navigationBarTitleDisplayMode(.inline)
            .searchable(text: $searchText, prompt: "Telefonnummer")
            .toolbar {
                if selection == 0 {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button(isSelectingCalls ? "Fertig" : "Auswählen") {
                            isSelectingCalls.toggle()
                            if !isSelectingCalls { selectedCallIDs.removeAll() }
                        }
                    }
                }
            }
            .task { await history.refresh() }
            .onChange(of: scenePhase) { _, phase in
                if phase == .active { Task { await history.refresh() } }
            }
        }
    }

    private func shareSelectedCalls() {
        let selected = history.conversations.filter { selectedCallIDs.contains($0.id) }
        let formatter = DateFormatter()
        formatter.dateStyle = .medium
        formatter.timeStyle = .short
        let text = selected.map { call in
            let number = call.handles.first?.value ?? "Unbekannt"
            return "\(formatter.string(from: call.date)) – \(number) – \(directionText(call)) – \(call.lineLabel)"
        }.joined(separator: "\n")
        guard !text.isEmpty else { return }
        let controller = UIActivityViewController(activityItems: [text], applicationActivities: nil)
        guard let scene = UIApplication.shared.connectedScenes.compactMap({ $0 as? UIWindowScene }).first(where: { $0.activationState == .foregroundActive }),
              let root = scene.windows.first(where: { $0.isKeyWindow })?.rootViewController else { return }
        var presenter = root
        while let presented = presenter.presentedViewController { presenter = presented }
        presenter.present(controller, animated: true)
    }

    private func hideSelectedCalls() {
        var ids = hiddenIDs
        ids.formUnion(selectedCallIDs.map { $0.uuidString })
        hiddenCallIDs = ids.sorted().joined(separator: "\n")
        let records = hiddenRecords + history.conversations.filter { selectedCallIDs.contains($0.id) }
        if let data = try? JSONEncoder().encode(records) { hiddenCallRecords = data }
        selectedCallIDs.removeAll()
    }

    private var filteredCalls: [CallRecord] {
        history.conversations.filter {
            !hiddenIDs.contains($0.id.uuidString) &&
            !LocalCallHistory.isHidden($0, records: hiddenRecords) &&
            (searchText.isEmpty || ($0.handles.first?.value ?? "").localizedCaseInsensitiveContains(searchText))
        }
    }

    private func directionText(_ call: CallRecord) -> String {
        String(describing: call.direction).lowercased().contains("incoming") ? "Eingehend" : "Ausgehend"
    }

    private func directionIcon(_ call: CallRecord) -> String {
        String(describing: call.direction).lowercased().contains("incoming") ? "phone.arrow.down.left" : "phone.arrow.up.right"
    }

    private func callStatusColor(_ call: CallRecord) -> Color {
        let status = String(describing: call.status).lowercased()
        return status == "connected" || call.connectedAt != nil || call.duration > 0 ? .green : .red
    }
}

private struct ContactsView: View {
    private enum ContactArea: String, CaseIterable {
        case privateContacts = "Privat"
        case business = "Beruflich"
    }

    private enum ContactSort: String, CaseIterable {
        case firstName = "Vorname"
        case lastName = "Nachname"
        case company = "Unternehmen"
    }

    @ObservedObject var dialer: DialerModel
    @State private var area = ContactArea.privateContacts
    @State private var contacts: [CNContact] = []
    @State private var showSortOptions = false
    @AppStorage("contactSort") private var sortValue = ContactSort.firstName.rawValue
    @AppStorage("businessContactIDs") private var businessContactIDs = ""

    private var businessIDs: Set<String> {
        Set(businessContactIDs.split(separator: "\n").map(String.init))
    }

    private var visibleContacts: [CNContact] {
        contacts
            .filter { contact in
                area == .business ? businessIDs.contains(contact.identifier) : !businessIDs.contains(contact.identifier)
            }
            .sorted { lhs, rhs in
                let left = sortText(for: lhs)
                let right = sortText(for: rhs)
                let comparison = left.localizedCaseInsensitiveCompare(right)
                if comparison == .orderedSame {
                    let leftFallback = CNContactFormatter.string(from: lhs, style: .fullName) ?? lhs.organizationName
                    let rightFallback = CNContactFormatter.string(from: rhs, style: .fullName) ?? rhs.organizationName
                    return leftFallback.localizedCaseInsensitiveCompare(rightFallback) == .orderedAscending
                }
                return comparison == .orderedAscending
            }
    }

    private func sortText(for contact: CNContact) -> String {
        switch ContactSort(rawValue: sortValue) ?? .firstName {
        case .firstName:
            return [contact.givenName, contact.familyName, contact.organizationName]
                .first(where: { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }) ?? ""
        case .lastName:
            return [contact.familyName, contact.givenName, contact.organizationName]
                .first(where: { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }) ?? ""
        case .company:
            return [contact.organizationName, contact.familyName, contact.givenName]
                .first(where: { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }) ?? ""
        }
    }

    private func move(_ contact: CNContact, to target: ContactArea) {
        var ids = businessIDs
        if target == .business {
            ids.insert(contact.identifier)
        } else {
            ids.remove(contact.identifier)
        }
        businessContactIDs = ids.sorted().joined(separator: "\n")
    }

    private func phoneNumbers(for contact: CNContact) -> [String] {
        contact.phoneNumbers
            .map { $0.value.stringValue.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
    }

    private func addToBlacklist(_ contact: CNContact) {
        var values = Set(
            UserDefaults.standard.string(forKey: "blacklistEntries")?
                .split(separator: "\n")
                .map(String.init) ?? []
        )
        values.formUnion(phoneNumbers(for: contact))
        UserDefaults.standard.set(values.sorted().joined(separator: "\n"), forKey: "blacklistEntries")
    }

    private func report(_ contact: CNContact) {
        addToBlacklist(contact)
        var values = Set(
            UserDefaults.standard.string(forKey: "reportedContactNumbers")?
                .split(separator: "\n")
                .map(String.init) ?? []
        )
        values.formUnion(phoneNumbers(for: contact))
        UserDefaults.standard.set(values.sorted().joined(separator: "\n"), forKey: "reportedContactNumbers")
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                Picker("Kontaktliste", selection: $area) {
                    ForEach(ContactArea.allCases, id: \.self) { item in
                        Text(item.rawValue).tag(item)
                    }
                }
                .pickerStyle(.segmented)
                .padding()

                Button { showSortOptions = true } label: {
                    HStack {
                        Label("Sortierung", systemImage: "arrow.up.arrow.down")
                        Spacer()
                        Text(sortValue)
                        Image(systemName: "chevron.down")
                    }
                    .padding(.horizontal, 12)
                    .frame(maxWidth: .infinity, minHeight: 56)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.glass)
                .padding(.horizontal)
                .sheet(isPresented: $showSortOptions) {
                    NavigationStack {
                        ScrollView {
                        VStack(spacing: 12) {
                        ForEach(ContactSort.allCases, id: \.self) { option in
                            Button {
                                sortValue = option.rawValue
                                showSortOptions = false
                            } label: {
                                HStack {
                                    Text(option.rawValue)
                                    Spacer()
                                    if sortValue == option.rawValue { Image(systemName: "checkmark") }
                                }
                                .padding(.horizontal, 12)
                                .frame(maxWidth: .infinity, minHeight: 56)
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.glass)
                        }
                        }
                        .padding()
                        }
                        .navigationTitle("Kontakte sortieren")
                        .navigationBarTitleDisplayMode(.inline)
                        .toolbar { Button("Fertig") { showSortOptions = false } }
                    }
                    .presentationDetents([.medium])
                }

                List(visibleContacts, id: \.identifier) { contact in
                    NavigationLink {
                        ContactDetailView(contact: contact, dialer: dialer, businessContactIDs: $businessContactIDs)
                    } label: {
                        HStack(spacing: 12) {
                            Group {
                                if let data = contact.thumbnailImageData, let image = UIImage(data: data) {
                                    Image(uiImage: image).resizable().scaledToFill()
                                } else {
                                    Image(systemName: "person.crop.circle.fill").resizable()
                                }
                            }
                            .frame(width: 44, height: 44)
                            .clipShape(Circle())

                            VStack(alignment: .leading) {
                                Text(CNContactFormatter.string(from: contact, style: .fullName) ?? contact.organizationName)
                                if !contact.organizationName.isEmpty {
                                    Text(contact.organizationName).font(.caption).foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                    .swipeActions(edge: .leading, allowsFullSwipe: false) {
                        Button {
                            move(contact, to: area == .business ? .privateContacts : .business)
                        } label: {
                            Label(area == .business ? "Privat" : "Beruflich",
                                  systemImage: area == .business ? "person.fill" : "briefcase.fill")
                        }
                        .tint(.blue)
                    }
                    .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                        Button {
                            addToBlacklist(contact)
                        } label: {
                            Label("Blockieren", systemImage: "hand.raised.fill")
                        }
                        .tint(.orange)

                        Button(role: .destructive) {
                            report(contact)
                        } label: {
                            Label("Melden", systemImage: "exclamationmark.bubble.fill")
                        }
                    }
                }
            }
            .navigationTitle("Kontakte")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        presentNewContact()
                    } label: {
                        Image(systemName: "plus")
                    }
                    .accessibilityLabel("Neuen Kontakt hinzufügen")
                }
            }
            .task { loadContacts() }
        }
    }

    private func presentNewContact() {
        let controller = CNContactViewController(forNewContact: nil)
        controller.allowsEditing = true
        let navigation = UINavigationController(rootViewController: controller)
        navigation.modalPresentationStyle = .formSheet

        guard let scene = UIApplication.shared.connectedScenes
            .compactMap({ $0 as? UIWindowScene })
            .first(where: { $0.activationState == .foregroundActive }),
              let root = scene.windows.first(where: { $0.isKeyWindow })?.rootViewController else { return }

        var presenter = root
        while let presented = presenter.presentedViewController {
            presenter = presented
        }
        presenter.present(navigation, animated: true)
    }

    private func loadContacts() {
        let store = CNContactStore()
        let keys: [CNKeyDescriptor] = [
            CNContactVCardSerialization.descriptorForRequiredKeys(),
            CNContactViewController.descriptorForRequiredKeys(),
            CNContactFormatter.descriptorForRequiredKeys(for: .fullName),
            CNContactOrganizationNameKey as CNKeyDescriptor,
            CNContactThumbnailImageDataKey as CNKeyDescriptor,
            CNContactPhoneNumbersKey as CNKeyDescriptor,
            CNContactEmailAddressesKey as CNKeyDescriptor,
            CNContactPostalAddressesKey as CNKeyDescriptor
        ]
        func fetchContacts() {
            let request = CNContactFetchRequest(keysToFetch: keys)
            var loaded: [CNContact] = []
            do {
                try store.enumerateContacts(with: request) { contact, _ in
                    loaded.append(contact)
                }
                DispatchQueue.main.async { contacts = loaded }
            } catch {
                DispatchQueue.main.async { contacts = [] }
            }
        }

        switch CNContactStore.authorizationStatus(for: .contacts) {
        case .authorized, .limited:
            fetchContacts()
        case .notDetermined:
            store.requestAccess(for: .contacts) { granted, _ in
                guard granted else { return }
                fetchContacts()
            }
        case .denied, .restricted:
            contacts = []
        @unknown default:
            contacts = []
        }
    }
}

private struct ContactDetailView: View {
    let contact: CNContact
    @ObservedObject var dialer: DialerModel
    @Binding var businessContactIDs: String

    @State private var sharedContact: SharedFiles?
    @State private var shareError: String?

    private var isBusiness: Bool {
        Set(businessContactIDs.split(separator: "\n").map(String.init)).contains(contact.identifier)
    }

    var body: some View {
        VStack(spacing: 0) {
            Picker("Zuordnung", selection: Binding(
                get: { isBusiness ? "Beruflich" : "Privat" },
                set: { newValue in
                    var ids = Set(businessContactIDs.split(separator: "\n").map(String.init))
                    if newValue == "Beruflich" {
                        ids.insert(contact.identifier)
                    } else {
                        ids.remove(contact.identifier)
                    }
                    businessContactIDs = ids.sorted().joined(separator: "\n")
                }
            )) {
                Text("Privat").tag("Privat")
                Text("Beruflich").tag("Beruflich")
            }
            .pickerStyle(.segmented)
            .padding(.horizontal)
            .padding(.vertical, 8)

            NativeContactView(contact: contact, dialer: dialer)
        }
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button {
                    do {
                        let data = try CNContactVCardSerialization.data(with: [contact])
                        sharedContact = try SharedFiles.make(data: data, name: "Kontakt.vcf")
                    } catch { shareError = error.localizedDescription }
                } label: { Label("Kontakt teilen", systemImage: "square.and.arrow.up") }
            }
        }
        .sheet(item: $sharedContact, onDismiss: { SharedFiles.cleanTemporaryFiles() }) { files in
            FileShareSheet(urls: files.urls)
        }
        .alert("Kontakt konnte nicht geteilt werden", isPresented: Binding(get: { shareError != nil }, set: { if !$0 { shareError = nil } })) {
            Button("OK") { shareError = nil }
        } message: { Text(shareError ?? "") }
    }
}

private struct NativeContactView: UIViewControllerRepresentable {
    let contact: CNContact
    @ObservedObject var dialer: DialerModel

    func makeCoordinator() -> Coordinator {
        Coordinator(dialer: dialer)
    }

    func makeUIViewController(context: Context) -> CNContactViewController {
        let controller = CNContactViewController(for: contact)
        controller.delegate = context.coordinator
        controller.allowsEditing = true
        controller.allowsActions = true
        controller.shouldShowLinkedContacts = true
        return controller
    }

    func updateUIViewController(_ uiViewController: CNContactViewController, context: Context) {}

    final class Coordinator: NSObject, CNContactViewControllerDelegate {
        let dialer: DialerModel

        init(dialer: DialerModel) {
            self.dialer = dialer
        }

        nonisolated func contactViewController(
            _ viewController: CNContactViewController,
            shouldPerformDefaultActionFor property: CNContactProperty
        ) -> Bool {
            if property.key == CNContactPhoneNumbersKey,
               let phone = property.value as? CNPhoneNumber {
                let number = phone.stringValue
                Task { @MainActor [dialer] in
                    dialer.call(number)
                }
                return false
            }
            return true
        }
    }
}


private struct MailboxView: View {
    @ObservedObject var dialer: DialerModel
    @StateObject private var mailbox = MailboxModel()
    @AppStorage("mailboxNumber") private var mailboxNumber = ""
    @State private var playingMessage: MailboxMessage?
    @State private var isSelectingMailbox = false
    @State private var selectedMessageIDs: Set<String> = []

    @State private var exportFiles: SharedFiles?
    @State private var exporting = false

    var body: some View {
        NavigationStack {
            Group {
                if mailbox.isLoading && mailbox.messages.isEmpty {
                    ProgressView("Mailbox wird geladen …")
                } else if let error = mailbox.errorMessage, mailbox.messages.isEmpty {
                    ContentUnavailableView(
                        "Mailbox nicht erreichbar",
                        systemImage: "exclamationmark.triangle",
                        description: Text(error)
                    )
                } else if mailbox.messages.isEmpty {
                    ContentUnavailableView(
                        "Keine Nachrichten",
                        systemImage: "recordingtape",
                        description: Text("Auf dem CallWebhook-Anrufbeantworter befinden sich keine Nachrichten.")
                    )
                } else {
                    List(mailbox.messages) { message in
                        Button {
                            if isSelectingMailbox {
                                if selectedMessageIDs.contains(message.id) {
                                    selectedMessageIDs.remove(message.id)
                                } else {
                                    selectedMessageIDs.insert(message.id)
                                }
                            } else {
                                playingMessage = message
                            }
                        } label: {
                            HStack(spacing: 12) {
                                if isSelectingMailbox {
                                    Image(systemName: selectedMessageIDs.contains(message.id) ? "checkmark.circle.fill" : "circle")
                                        .foregroundStyle(selectedMessageIDs.contains(message.id) ? .blue : .secondary)
                                }
                                Image(systemName: message.isNew ? "recordingtape.circle.fill" : "recordingtape.circle")
                                    .font(.title2)
                                    .foregroundStyle(message.isNew ? .blue : .secondary)

                                VStack(alignment: .leading, spacing: 4) {
                                    Text(message.name.isEmpty ? (message.number.isEmpty ? "Unbekannt" : message.number) : message.name)
                                        .font(.headline)
                                        .foregroundStyle(.primary)
                                    if !message.name.isEmpty && !message.number.isEmpty {
                                        Text(message.number)
                                            .font(.caption)
                                            .foregroundStyle(.secondary)
                                    }
                                    Text(message.date)
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }

                                Spacer()

                                VStack(alignment: .trailing, spacing: 5) {
                                    Text(message.duration)
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                    Image(systemName: "play.circle.fill")
                                        .font(.title2)
                                }
                            }
                        }
                        .buttonStyle(.plain)
                        .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                            Button(role: .destructive) {
                                Task {
                                    do {
                                        try await mailbox.delete(message)
                                    } catch {
                                        mailbox.setError(error.localizedDescription)
                                    }
                                }
                            } label: {
                                Label("Löschen", systemImage: "trash")
                            }
                        }
                        .swipeActions(edge: .leading, allowsFullSwipe: false) {
                            Button {
                                Task { await exportMessages([message]) }
                            } label: { Label("Auf iPhone", systemImage: "square.and.arrow.down") }
                            .tint(.green)
                            .disabled(exporting)
                            if !message.isArchived {
                                Button {
                                    Task {
                                        do {
                                            try await mailbox.archive(message)
                                        } catch {
                                            mailbox.setError(error.localizedDescription)
                                        }
                                    }
                                } label: {
                                    Label("Auf HA", systemImage: "archivebox.fill")
                                }
                                .tint(.blue)
                            }
                        }
                    }
                    .listStyle(.plain)
                    .refreshable { await mailbox.refresh() }
                    .safeAreaInset(edge: .bottom) {
                        if isSelectingMailbox {
                            HStack {
                                Menu {
                                    Button {
                                        Task { await archiveSelectedMessages() }
                                    } label: { Label("Auf Home Assistant", systemImage: "archivebox.fill") }
                                    Button {
                                        Task { await exportMessages(mailbox.messages.filter { selectedMessageIDs.contains($0.id) }) }
                                    } label: { Label("In Dateien sichern", systemImage: "square.and.arrow.down") }
                                } label: { Label("Speichern", systemImage: "square.and.arrow.down") }
                                .disabled(selectedMessageIDs.isEmpty || exporting)
                                Spacer()

                                Text("\(selectedMessageIDs.count) ausgewählt")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)

                                Spacer()

                                Button(role: .destructive) {
                                    Task { await deleteSelectedMessages() }
                                } label: {
                                    Label("Löschen", systemImage: "trash")
                                }
                                .disabled(selectedMessageIDs.isEmpty)
                            }
                            .padding()
                            .background(.bar)
                        }
                    }
                }
            }
            .navigationTitle("Mailbox")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItemGroup(placement: .topBarTrailing) {
                    Button(isSelectingMailbox ? "Fertig" : "Auswählen") {
                        isSelectingMailbox.toggle()
                        if !isSelectingMailbox { selectedMessageIDs.removeAll() }
                    }

                    Button {
                        Task { await mailbox.refresh() }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }

                    if !mailboxNumber.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        Button {
                            dialer.call(mailboxNumber)
                        } label: {
                            Image(systemName: "phone.fill")
                        }
                    }
                }
            }
            .overlay { if exporting { ProgressView("Aufnahmen werden geladen …").padding().background(.regularMaterial, in: RoundedRectangle(cornerRadius: 16)) } }
            .sheet(item: $exportFiles, onDismiss: { SharedFiles.cleanTemporaryFiles() }) { files in
                FileExportPicker(urls: files.urls)
            }
            .alert("Mailbox", isPresented: Binding(get: { mailbox.errorMessage != nil && !mailbox.messages.isEmpty }, set: { if !$0 { mailbox.clearError() } })) {
                Button("OK") { mailbox.clearError() }
            } message: { Text(mailbox.errorMessage ?? "") }
            .task { await mailbox.refresh() }
            .sheet(item: $playingMessage) { message in
                NavigationStack {
                    VoicemailPlayerView(mailbox: mailbox, message: message)
                        .navigationTitle(message.name.isEmpty ? message.number : message.name)
                        .navigationBarTitleDisplayMode(.inline)
                }
                .presentationDetents([.medium])
            }
        }
    }

    private func exportMessages(_ messages: [MailboxMessage]) async {
        guard !exporting else { return }
        exporting = true
        defer { exporting = false }
        do {
            var urls: [URL] = []
            for message in messages {
                let data = try await mailbox.loadAudio(for: message)
                let files = try SharedFiles.make(data: data, name: "Mailbox-\(message.tam)-\(message.index).wav")
                urls.append(contentsOf: files.urls)
            }
            if !urls.isEmpty { exportFiles = SharedFiles(urls: urls) }
        } catch {
            SharedFiles.cleanTemporaryFiles()
            mailbox.setError(error.localizedDescription)
        }
    }

    private func archiveSelectedMessages() async {
        let selected = mailbox.messages.filter { selectedMessageIDs.contains($0.id) && !$0.isArchived }
        for message in selected {
            do {
                try await mailbox.archive(message)
            } catch {
                mailbox.setError(error.localizedDescription)
                return
            }
        }
        selectedMessageIDs.removeAll()
    }

    private func deleteSelectedMessages() async {
        let selected = mailbox.messages.filter { selectedMessageIDs.contains($0.id) }
        for message in selected {
            do {
                try await mailbox.delete(message)
            } catch {
                mailbox.setError(error.localizedDescription)
                return
            }
        }
        selectedMessageIDs.removeAll()
    }
}

private struct VoicemailPlayerView: View {
    @ObservedObject var mailbox: MailboxModel
    let message: MailboxMessage
    @State private var player: AVPlayer?
    @State private var isLoading = true
    @State private var errorMessage: String?

    var body: some View {
        VStack(spacing: 24) {
            Image(systemName: "waveform.circle.fill")
                .font(.system(size: 72))
                .foregroundStyle(.blue)

            if isLoading {
                ProgressView("Aufnahme wird geladen …")
            } else if let errorMessage {
                ContentUnavailableView(
                    "Aufnahme nicht verfügbar",
                    systemImage: "waveform.slash",
                    description: Text(errorMessage)
                )
            } else if let player {
                VideoPlayer(player: player)
                    .frame(height: 80)

                Button {
                    player.seek(to: .zero)
                    player.play()
                } label: {
                    Label("Von vorn abspielen", systemImage: "play.fill")
                }
                .buttonStyle(.borderedProminent)
            }
        }
        .padding()
        .task {
            do {
                let data = try await mailbox.loadAudio(for: message)
                let fileURL = FileManager.default.temporaryDirectory
                    .appendingPathComponent("callwebhook_\(message.tam)_\(message.index).wav")
                try data.write(to: fileURL, options: .atomic)
                let newPlayer = AVPlayer(url: fileURL)
                player = newPlayer
                isLoading = false
                newPlayer.play()
            } catch {
                errorMessage = error.localizedDescription
                isLoading = false
            }
        }
        .onDisappear {
            player?.pause()
        }
    }
}

private struct DialPadView: View {
    @EnvironmentObject var monitor: CallMonitor
    @ObservedObject var dialer: DialerModel
    @ObservedObject private var sip = SIPService.shared
    @State private var callStatus = "Bereit"
    let primaryPhoneNumber: String
    let secondaryPhoneNumber: String
    let sipLine2Enabled: Bool
    let sipLine3Enabled: Bool
    @AppStorage("sipEnabled") private var sipEnabled = false
    @AppStorage("sipLine3Number") private var landlineNumber = ""
    private let rows = [["1","2","3"],["4","5","6"],["7","8","9"],["*","0","#"]]

    var body: some View {
        NavigationStack {
            VStack(spacing: 18) {
                VStack(spacing: 3) {
                    if !primaryPhoneNumber.isEmpty {
                        Text("SIM 1  \(primaryPhoneNumber)")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                    }
                    if sipLine2Enabled && !secondaryPhoneNumber.isEmpty {
                        Text("SIM 2  \(secondaryPhoneNumber)")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                    }
                    if sipLine3Enabled && !landlineNumber.isEmpty {
                        Text("Festnetz  \(landlineNumber)")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                    }
                }
                .fixedSize(horizontal: false, vertical: true)

                Spacer()

                HStack(spacing: 12) {
                    Button {
                        if let value = UIPasteboard.general.string?
                            .trimmingCharacters(in: .whitespacesAndNewlines),
                           !value.isEmpty {
                            dialer.number = value
                        }
                    } label: {
                        Image(systemName: "doc.on.clipboard")
                            .font(.title3)
                            .frame(width: 44, height: 44)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Aus Zwischenablage einfügen")
                    .disabled(sip.active || dialer.isDialing)

                    Text(dialer.number.isEmpty ? " " : dialer.number)
                        .font(.system(size: 34, weight: .regular, design: .rounded))
                        .minimumScaleFactor(0.6)
                        .lineLimit(1)
                        .frame(maxWidth: .infinity)
                        .frame(height: 44)

                    Image(systemName: "delete.left")
                        .font(.title3)
                        .frame(width: 44, height: 44)
                        .contentShape(Rectangle())
                        .foregroundStyle(dialer.number.isEmpty ? Color.secondary : Color.primary)
                        .opacity(dialer.number.isEmpty ? 0.35 : 1)
                        .onTapGesture {
                            guard !dialer.number.isEmpty, !sip.active, !dialer.isDialing else { return }
                            dialer.deleteLast()
                        }
                        .onLongPressGesture(minimumDuration: 0.6, maximumDistance: 30) {
                            guard !dialer.number.isEmpty, !sip.active, !dialer.isDialing else { return }
                            dialer.number = ""
                        }
                        .accessibilityLabel("Letzte Ziffer löschen; lange drücken zum Leeren")
                }

                ForEach(rows, id: \.self) { row in
                    HStack(spacing: 26) {
                        ForEach(row, id: \.self) { digit in
                            Button {
                                dialer.append(digit)
                            } label: {
                                Text(digit)
                                    .font(.system(size: 30, weight: .medium, design: .rounded))
                                    .frame(width: 72, height: 72)
                                    .background(.thinMaterial, in: Circle())
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }

                HStack(spacing: 10) {
                    if sip.incoming {
                        Button { IncomingCallProvider.shared.answer() } label: {
                            Image(systemName: "phone.fill")
                                .font(.system(size: 27, weight: .semibold))
                                .foregroundStyle(.white)
                                .frame(width: 72, height: 72)
                                .background(.green, in: Circle())
                        }.accessibilityLabel("Anruf annehmen")
                        endCallButton
                    } else if CellularRouting.cellularOnlyNumber(dialer.number) != nil {
                        callButton(line: nil)
                    } else if sipEnabled {
                        sipCallButton(line: 1)
                        if sipLine2Enabled { sipCallButton(line: 2) }
                        if sipLine3Enabled { sipCallButton(line: 3) }
                        endCallButton
                    } else if !secondaryPhoneNumber.isEmpty {
                        callButton(line: 1)
                        endCallButton
                        callButton(line: 2)
                    } else {
                        callButton(line: nil)
                        endCallButton
                    }
                }

                Text(callStatus)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                    .frame(maxWidth: .infinity, minHeight: 28)
                    .onAppear { callStatus = sip.active ? sip.callStatus : dialer.status }
                    .onChange(of: dialer.status) { _, value in callStatus = value }
                    .onChange(of: sip.callStatus) { _, value in callStatus = value }

                Spacer()
            }
            .padding(.horizontal)
            .navigationTitle("Zifferblatt")
        }
    }

    private var endCallButton: some View {
        Button {
            dialer.hangup()
        } label: {
            Image(systemName: "phone.down.fill")
                .font(.system(size: 27, weight: .semibold))
                .foregroundStyle(.white)
                .frame(width: 72, height: 72)
                .background((monitor.active || sip.active || dialer.isDialing) ? Color.red : Color.gray.opacity(0.45), in: Circle())
        }
        .buttonStyle(.plain)
        .disabled(!monitor.active && !sip.active && !dialer.isDialing)
        .accessibilityLabel("Anruf beenden")
    }

    private func sipCallButton(line: Int) -> some View {
        Button {
            dialer.call(line: line)
        } label: {
            ZStack {
                Circle().fill(.green).frame(width: 72, height: 72)
                Image(systemName: "phone.fill")
                    .font(.system(size: 30, weight: .semibold))
                    .foregroundStyle(.white)
                Text("\(line)")
                    .font(.caption2.bold())
                    .foregroundStyle(.white)
                    .frame(width: 20, height: 20)
                    .background(.black.opacity(0.55), in: Circle())
                    .offset(x: 24, y: -24)
            }
            .frame(width: 72, height: 72)
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Mit SIP-Leitung \(line) anrufen")
        .disabled(sip.active || dialer.isDialing)
    }

    @ViewBuilder
    private func callButton(line: Int?) -> some View {
        Button {
            dialer.call()
        } label: {
            ZStack {
                Circle()
                    .fill(.green)
                    .frame(width: 72, height: 72)

                Image(systemName: "phone.fill")
                    .font(.system(size: 30, weight: .semibold))
                    .foregroundStyle(.white)

                if let line {
                    Text("\(line)")
                        .font(.caption2.bold())
                        .foregroundStyle(.white)
                        .frame(width: 20, height: 20)
                        .background(.black.opacity(0.55), in: Circle())
                        .offset(x: 24, y: -24)
                }
            }
            .frame(width: 72, height: 72)
        }
        .buttonStyle(.plain)
        .accessibilityLabel(line == nil ? "Anrufen" : "Mit Leitung \(line!) anrufen")
    }
}

private struct ExtrasView: View {
    let onRestartSetup: () -> Void
    @EnvironmentObject var monitor: CallMonitor
    private enum InputField: Hashable { case primary, secondary, mailbox, sipHost, sipUsername, sipPassword, sipLine2Prefix, sipLine3Prefix, haToken, externalListName, externalListURL, blacklist, whitelist }
    @FocusState private var focusedInputField: InputField?
    @AppStorage("primaryPhoneNumber") private var primaryPhoneNumber = ""
    @AppStorage("secondaryPhoneNumber") private var secondaryPhoneNumber = ""
    @AppStorage("callFilterMode") private var callFilterMode = "Blacklist"
    @AppStorage("blacklistEntries") private var blacklistEntries = ""
    @AppStorage("whitelistEntries") private var whitelistEntries = ""
    @AppStorage("phoneBlockEnabled") private var phoneBlockEnabled = false
    @AppStorage("phoneBlockMinVotes") private var phoneBlockMinVotes = 4
    @AppStorage("externalListURL") private var externalListURL = ""
    @AppStorage("externalListName") private var externalListName = ""
    @AppStorage("externalListEnabled") private var externalListEnabled = false
    @AppStorage("mailboxNumber") private var mailboxNumber = ""
    @AppStorage("sipEnabled") private var sipEnabled = false
    @AppStorage("sipHost") private var sipHost = "192.168.178.26"
    @AppStorage("sipUsername") private var sipUsername = "callwebhook-ios"
    @AppStorage("sipPassword") private var sipPassword = ""
    @AppStorage("sipLine2Enabled") private var sipLine2Enabled = false
    @AppStorage("sipLine3Enabled") private var sipLine3Enabled = false
    @AppStorage("sipLine2Prefix") private var sipLine2Prefix = ""
    @AppStorage("sipLine3Prefix") private var sipLine3Prefix = ""
    @State private var showSIP = false
    @ObservedObject private var sip = SIPService.shared
    @State private var showMobile = false
    @State private var showHomeAssistant = false
    @State private var showCallFilter = false
    @State private var newBlacklistEntry = ""
    @State private var newWhitelistEntry = ""

    private var blacklist: [String] {
        blacklistEntries.split(separator: "\n").map(String.init).filter { !$0.isEmpty }
    }

    private var whitelist: [String] {
        whitelistEntries.split(separator: "\n").map(String.init).filter { !$0.isEmpty }
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Button {
                        focusedInputField = nil
                        onRestartSetup()
                    } label: {
                        Label("Einrichtungsassistent erneut starten", systemImage: "arrow.clockwise")
                            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.borderless)
                    .disabled(sip.active)
                    if sip.active {
                        Text("Nach dem laufenden Gespräch kann der Assistent wieder geöffnet werden.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }

                DisclosureGroup("Asterisk / VoIP", isExpanded: $showSIP) {
                    Toggle("Anrufe über Asterisk", isOn: $sipEnabled)
                    TextField("Asterisk Host", text: $sipHost)
                        .focused($focusedInputField, equals: .sipHost)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    TextField("SIP Benutzer", text: $sipUsername)
                        .focused($focusedInputField, equals: .sipUsername)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    SecureField("SIP Passwort", text: $sipPassword)
                        .focused($focusedInputField, equals: .sipPassword)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    settingsAction("SIP verbinden / neu registrieren", systemImage: "antenna.radiowaves.left.and.right") {
                        do {
                            try SIPService.shared.configureAndStart(
                                host: sipHost,
                                username: sipUsername,
                                password: sipPassword
                            )
                        } catch {
                            // Status wird im SIP-Dienst gesetzt.
                        }
                    }
                    Text(sip.status)
                        .font(.caption)
                        .foregroundStyle(.secondary)

                    Toggle("SIP-Leitung 2 aktiv", isOn: $sipLine2Enabled)
                    if sipLine2Enabled {
                        TextField("Asterisk-Präfix Leitung 2", text: $sipLine2Prefix)
                            .focused($focusedInputField, equals: .sipLine2Prefix)
                            .keyboardType(.numbersAndPunctuation)
                    }

                    Toggle("SIP-Leitung 3 aktiv", isOn: $sipLine3Enabled)
                    if sipLine3Enabled {
                        TextField("Asterisk-Präfix Leitung 3", text: $sipLine3Prefix)
                            .focused($focusedInputField, equals: .sipLine3Prefix)
                            .keyboardType(.numbersAndPunctuation)
                    }
                    Text("Aktiv: Zifferblatt, Anrufliste und Mailbox wählen über Asterisk. Deaktiviert: bisheriger Mobilfunkpfad.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                DisclosureGroup("Mobilfunk / Dual-SIM", isExpanded: $showMobile) {
                    TextField("Primäre Rufnummer", text: $primaryPhoneNumber)
                        .keyboardType(.phonePad)
                        .focused($focusedInputField, equals: .primary)
                        .submitLabel(.done)
                    TextField("Zweite Rufnummer", text: $secondaryPhoneNumber)
                        .keyboardType(.phonePad)
                        .focused($focusedInputField, equals: .secondary)
                        .submitLabel(.done)

                }

                DisclosureGroup("Home Assistant", isExpanded: $showHomeAssistant) {
                    LabeledContent("iPhone Telefonstatus", value: monitor.haState)
                    LabeledContent("FRITZ!Box Anrufmonitor", value: monitor.fritzCallState)

                    Picker("HA-Schalter aktivieren bei", selection: $monitor.haTriggerMode) {
                        Text("Klingeln").tag("ringing")
                        Text("Gespräch verbunden").tag("connected")
                    }
                    .pickerStyle(.menu)

                    Text(monitor.haTriggerMode == "ringing" ? "Der HA-Schalter wird bereits beim Klingeln bzw. Start eines ausgehenden Anrufs aktiviert." : "Der HA-Schalter wird erst aktiviert, wenn das Gespräch tatsächlich verbunden ist.")
                        .font(.caption)
                        .foregroundStyle(.secondary)

                    settingsAction("HA-Status aktualisieren", systemImage: "arrow.clockwise") {
                        monitor.refreshHAState()
                    }

                    settingsAction("Aktuellen Telefonstatus senden", systemImage: "paperplane.fill") {
                        monitor.sendCurrentState()
                    }

                    SecureField("Long-Lived Access Token von Home Assistant eintragen", text: $monitor.haToken)
                        .focused($focusedInputField, equals: .haToken)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .textContentType(.password)

                    settingsAction("Token speichern & testen", systemImage: "checkmark.shield.fill") {
                        monitor.refreshHAState()
                    }
                }

                DisclosureGroup("Anruffilter", isExpanded: $showCallFilter) {
                    Section {
                        Toggle("PhoneBlock Community", isOn: $phoneBlockEnabled)

                        if phoneBlockEnabled {
                            Stepper("Mindestmeldungen: \(phoneBlockMinVotes)", value: $phoneBlockMinVotes, in: 1...20)
                            Text("Vorkonfigurierte Community-Quelle. Die persönliche Whitelist hat später immer Vorrang.")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    } header: {
                        Text("Community-Listen")
                    }

                    Section {
                        TextField("Listenname", text: $externalListName)
                            .focused($focusedInputField, equals: .externalListName)
                        TextField("HTTPS-URL (TXT / CSV / JSON)", text: $externalListURL)
                            .focused($focusedInputField, equals: .externalListURL)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .keyboardType(.URL)
                        Toggle("Externe Liste aktiv", isOn: $externalListEnabled)
                            .disabled(externalListURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)

                        settingsAction(
                            "Liste jetzt laden",
                            systemImage: "arrow.down.circle.fill",
                            disabled: !externalListEnabled || externalListURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                        ) {
                            // Netzwerkimport und Parser werden als eigener Dienst angebunden.
                        }
                    } header: {
                        Text("Eigene Listenquelle")
                    }

                    Section {
                    Picker("Modus", selection: $callFilterMode) {
                        Text("Blacklist").tag("Blacklist")
                        Text("Whitelist").tag("Whitelist")
                    }
                    .pickerStyle(.segmented)

                    if callFilterMode == "Blacklist" {
                        filterEditor(
                            title: "Blacklist",
                            placeholder: "Nummer oder Eintrag hinzufügen",
                            newEntry: $newBlacklistEntry,
                            entries: blacklist,
                            storage: $blacklistEntries
                        )
                    } else {
                        filterEditor(
                            title: "Whitelist",
                            placeholder: "Nummer oder Eintrag hinzufügen",
                            newEntry: $newWhitelistEntry,
                            entries: whitelist,
                            storage: $whitelistEntries
                        )
                    }

                    Text(callFilterMode == "Blacklist"
                         ? "Einträge dieser Liste sollen später automatisch abgewiesen bzw. blockiert werden."
                         : "Im Whitelist-Modus sollen später nur freigegebene Einträge durchgestellt werden.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    } header: {
                        Text("Eigene Einträge")
                    }
                }
            }
            .navigationTitle("Extras")
            .toolbar {
                ToolbarItemGroup(placement: .keyboard) {
                    Spacer()
                    Button("Fertig") {
                        focusedInputField = nil
                    }
                }
            }
            .scrollDismissesKeyboard(.interactively)
        }
    }

    @ViewBuilder
    private func settingsAction(
        _ title: String,
        systemImage: String,
        disabled: Bool = false,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            HStack(spacing: 12) {
                Image(systemName: systemImage)
                    .font(.body.weight(.semibold))
                    .frame(width: 24)
                Text(title)
                    .font(.body.weight(.semibold))
                Spacer(minLength: 8)
                Image(systemName: "chevron.right")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(.secondary)
            }
            .padding(.horizontal, 14)
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.glass)
        .controlSize(.regular)
        .disabled(disabled)
    }

    @ViewBuilder
    private func filterEditor(
        title: String,
        placeholder: String,
        newEntry: Binding<String>,
        entries: [String],
        storage: Binding<String>
    ) -> some View {
        HStack {
            TextField(placeholder, text: newEntry)
                .focused($focusedInputField, equals: title == "Blacklist" ? .blacklist : .whitelist)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()

            Button {
                let value = newEntry.wrappedValue.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !value.isEmpty else { return }
                var values = entries
                if !values.contains(value) {
                    values.append(value)
                    storage.wrappedValue = values.joined(separator: "\n")
                }
                newEntry.wrappedValue = ""
            } label: {
                Image(systemName: "plus.circle.fill")
            }
            .buttonStyle(.plain)
        }

        if entries.isEmpty {
            Text("Keine Einträge")
                .foregroundStyle(.secondary)
        } else {
            ForEach(entries, id: \.self) { entry in
                HStack {
                    Text(entry)
                    Spacer()
                    Button(role: .destructive) {
                        storage.wrappedValue = entries
                            .filter { $0 != entry }
                            .joined(separator: "\n")
                    } label: {
                        Image(systemName: "minus.circle.fill")
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }
}
