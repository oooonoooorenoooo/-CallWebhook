import SwiftUI
import UserNotifications
import AppKit
import Combine
import Carbon

final class MenuBarWindow: NSWindow {
    override func miniaturize(_ sender: Any?) {
        // The yellow button hides the window instead of creating a Dock thumbnail.
        orderOut(sender)
    }
}

@MainActor
final class MacAppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate, NSMenuDelegate, NSWindowDelegate {
    private let sip = MacSIPClient()
    private let login = MacLoginService()
    private var mainWindow: NSWindow?
    private var statusItem: NSStatusItem?
    private var observations = Set<AnyCancellable>()

    func applicationDidFinishLaunching(_ notification: Notification) {
        // A second process would replace the first SIP contact (max_contacts=1).
        // Launch Services normally reuses the app, but direct executable/Xcode
        // launches can otherwise create another independent registration.
        if let identifier = Bundle.main.bundleIdentifier,
           let existing = NSRunningApplication.runningApplications(withBundleIdentifier: identifier)
            .first(where: { $0.processIdentifier != ProcessInfo.processInfo.processIdentifier && !$0.isTerminated }) {
            if !CommandLine.arguments.contains("--background") {
                existing.activate(options: [.activateIgnoringOtherApps])
            }
            NSApp.terminate(nil)
            return
        }
        let event = NSAppleEventManager.shared().currentAppleEvent
        let loginLaunch = event?.eventID == kAEOpenApplication &&
            event?.paramDescriptor(forKeyword: keyAEPropData)?.enumCodeValue == keyAELaunchedAsLogInItem
        let background = loginLaunch || CommandLine.arguments.contains("--background")
        if let url = Bundle.main.url(forResource: "AppIcon", withExtension: "icns"),
           let icon = NSImage(contentsOf: url) {
            NSApp.applicationIconImage = icon
        }
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        // Foreground setup requests permission; login launches remain unobtrusive.
        if !background { center.requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in } }
        configureMenuBar()
        let configuration = MacConfiguration.load()
        if !configuration.host.isEmpty {
            do { try sip.start(configuration) } catch { sip.error = error.localizedDescription }
        }
        if !background { showWindow() }
    }

    private func configureMenuBar() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem?.button?.image = NSImage(systemSymbolName: "phone.fill", accessibilityDescription: "CallWebhook")
        statusItem?.button?.image?.isTemplate = true
        statusItem?.button?.setAccessibilityLabel("CallWebhook")
        let menu = NSMenu()
        menu.delegate = self
        statusItem?.menu = menu
        sip.$status.sink { [weak self] status in
            self?.statusItem?.button?.toolTip = "CallWebhook · \(status)"
        }.store(in: &observations)
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        login.refresh()
        menu.removeAllItems()
        let status = menu.addItem(withTitle: sip.status, action: nil, keyEquivalent: "")
        status.isEnabled = false
        menu.addItem(.separator())
        add("CallWebhook öffnen", action: #selector(openWindow), to: menu)
        if sip.incoming { add("Anruf annehmen", action: #selector(answerCall), to: menu) }
        if sip.active { add(sip.incoming ? "Anruf ablehnen" : "Auflegen", action: #selector(endCall), to: menu) }
        add("Im Hintergrund weiterlaufen", action: #selector(hideWindow), to: menu)
        menu.addItem(.separator())
        let item = add("Bei Anmeldung im Hintergrund starten", action: #selector(toggleLogin), to: menu)
        item.state = login.requested ? .on : .off
        if login.needsApproval { add("Anmeldeobjekt freigeben …", action: #selector(approveLogin), to: menu) }
        menu.addItem(.separator())
        add("Beenden", action: #selector(quit), to: menu)
    }

    @discardableResult
    private func add(_ title: String, action: Selector, to menu: NSMenu) -> NSMenuItem {
        let item = menu.addItem(withTitle: title, action: action, keyEquivalent: "")
        item.target = self
        return item
    }

    private func showWindow() {
        if mainWindow == nil {
            let controller = NSHostingController(rootView: MacMainView(sip: sip, login: login))
            let window = MenuBarWindow(contentViewController: controller)
            window.title = "CallWebhook · Festnetz"
            window.styleMask = [.titled, .closable, .miniaturizable, .resizable]
            window.setContentSize(NSSize(width: 900, height: 620))
            window.contentMinSize = NSSize(width: 760, height: 520)
            window.isReleasedWhenClosed = false
            window.delegate = self
            window.center()
            mainWindow = window
        }
        mainWindow?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc private func openWindow() { showWindow() }
    @objc private func hideWindow() { mainWindow?.orderOut(nil) }
    @objc private func toggleLogin() {
        login.setEnabled(!login.requested)
        if let error = login.error { sip.error = error; showWindow() }
    }
    @objc private func approveLogin() { login.openSystemSettings() }
    @objc private func answerCall() { showWindow(); Task { await sip.answer() } }
    @objc private func endCall() { sip.hangup() }
    @objc private func quit() { NSApp.terminate(nil) }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showWindow()
        return false
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil)
        return false
    }
    func applicationWillTerminate(_ notification: Notification) { sip.stop() }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        DispatchQueue.main.async {
            self.showWindow()
            completionHandler()
        }
    }
}

@main
struct CallWebhookMacApp: App {
    @NSApplicationDelegateAdaptor(MacAppDelegate.self) private var delegate
    var body: some Scene {
        Settings { EmptyView() }
    }
}

struct MacMainView: View {
    @ObservedObject var sip: MacSIPClient
    @ObservedObject var login: MacLoginService
    @ObservedObject private var history = LocalCallHistory.shared
    @StateObject private var contacts = MacContacts()
    @StateObject private var mailbox = MacMailbox()
    @State private var configuration = MacConfiguration.load()
    @State private var number = ""
    @State private var search = ""
    @State private var sipPassword = ""
    @State private var token = ""
    @State private var settings = false
    @State private var settingsError: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Image(systemName: "phone.fill").foregroundStyle(sip.registered ? .green : .secondary)
                Text(sip.status)
                Spacer()
                Button("Einstellungen") { settings = true }
            }
            HStack {
                TextField("Rufnummer · Leitung 3", text: $number).onSubmit { dial(number) }
                Button("Anrufen") { dial(number) }.disabled(!sip.registered || sip.active)
            }
            if sip.active {
                HStack {
                    Text(sip.caller).font(.title2)
                    Spacer()
                    if sip.incoming { Button("Annehmen") { Task { await sip.answer() } }.tint(.green).onAppear { sip.ringViewAppeared() } }
                    Button(sip.incoming ? "Ablehnen" : "Auflegen") { sip.hangup() }.tint(.red)
                }
                HStack {
                    ForEach(Array("123456789*0#"), id: \.self) { digit in
                        Button(String(digit)) { sip.dtmf(String(digit)) }
                    }
                }
            }
            TabView {
                VStack {
                    TextField("Kontakte suchen", text: $search)
                    if let error = contacts.error { Text(error).foregroundStyle(.red) }
                    List(contacts.entries.filter { search.isEmpty || $0.name.localizedCaseInsensitiveContains(search) || $0.number.contains(search) }) { contact in
                        HStack {
                            VStack(alignment: .leading) { Text(contact.name); Text(contact.number).foregroundStyle(.secondary) }
                            Spacer(); Button("Anrufen") { dial(contact.number) }.disabled(!sip.registered || sip.active)
                        }
                    }
                    Button("Kontakte laden") { Task { await contacts.load() } }
                }.padding().tabItem { Label("Kontakte", systemImage: "person.crop.circle") }
                List(history.entries.filter { $0.line == 3 }) { record in
                    HStack {
                        VStack(alignment: .leading) {
                            Text(record.number)
                            Text("\(record.direction == "incoming" ? "Eingehend" : "Ausgehend") · \(MacCallOutcome.label(record.status)) · \(Int(record.duration)) s").foregroundStyle(.secondary)
                        }
                        Spacer(); Text(record.date, style: .date); Text(record.date, style: .time)
                        Button("Rückruf") { dial(record.number) }.disabled(!sip.registered || sip.active)
                    }
                }.tabItem { Label("Verlauf", systemImage: "clock") }
                VStack {
                    HStack {
                        Button("Mailbox aktualisieren") { Task { await mailbox.refresh(configuration) } }.disabled(mailbox.loading)
                        Button("Wiedergabe stoppen") { mailbox.stop() }
                    }
                    if let error = mailbox.error { Text(error).foregroundStyle(.red) }
                    List(mailbox.messages) { message in
                        HStack {
                            VStack(alignment: .leading) {
                                Text(message.name.isEmpty ? message.number : message.name)
                                Text("\(message.date) · \(message.duration)").foregroundStyle(.secondary)
                            }
                            Spacer()
                            Button("Abspielen") { Task { await mailbox.play(message, configuration: configuration) } }
                        }
                    }
                }.padding().tabItem { Label("Mailbox", systemImage: "recordingtape") }
            }
            Text("Nur Festnetzleitung 3 · Eingehende Anrufe benötigen eine laufende App und Heimnetz/VPN.")
                .font(.footnote).foregroundStyle(.secondary)
        }.padding()
        .task {
            if configuration.host.isEmpty { settings = true }
            login.refresh()
        }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            login.refresh()
        }
        .sheet(isPresented: $settings) { settingsView }
        .alert("CallWebhook", isPresented: Binding(get: { sip.error != nil }, set: { if !$0 { sip.error = nil } })) {
            Button("OK") { sip.error = nil }
        } message: { Text(sip.error ?? "") }
    }

    private func dial(_ value: String) { Task { await sip.dial(value) } }

    private var settingsView: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Festnetz einrichten").font(.title)
            Text("Separater Asterisk-Endpunkt: callwebhook-mac. Serverkonfiguration gemäß macOS/README.md vorbereiten.")
            Form {
                Toggle("Bei Anmeldung im Hintergrund starten", isOn: Binding(
                    get: { login.requested }, set: { login.setEnabled($0) }))
                if login.needsApproval {
                    Button("In Systemeinstellungen freigeben") { login.openSystemSettings() }
                }
                if let error = login.error { Text(error).foregroundStyle(.red) }
                TextField("Asterisk-Host[:Port]", text: $configuration.host)
                SecureField("Mac-SIP-Passwort (leer = behalten)", text: $sipPassword)
                TextField("Home Assistant URL", text: $configuration.homeAssistant)
                SecureField("HA-Zugriffstoken (leer = behalten)", text: $token)
                TextField("TAM-ID ausschließlich für Leitung 3", text: $configuration.mailboxTAM)
            }
            Text("SIP und HA-Zugangsdaten werden im macOS-Schlüsselbund gespeichert. Die TAM-ID ist die technische, bei 0 beginnende FRITZ!Box-ID.").font(.footnote)
            if let settingsError { Text(settingsError).foregroundStyle(.red) }
            HStack {
                Button("Schließen") { settings = false }
                Button("Speichern und verbinden") {
                    do {
                        try configuration.validate()
                        guard !sip.active else { throw MacError.message("Zuerst das laufende Gespräch beenden.") }
                        try MacSecrets.set(sipPassword, name: "sip")
                        try MacSecrets.set(token, name: "ha")
                        try configuration.save()
                        try sip.start(configuration)
                        sipPassword = ""; token = ""; settingsError = nil; settings = false
                    } catch { settingsError = error.localizedDescription }
                }
            }
        }.padding(24).frame(width: 540)
    }
}
