import SwiftUI
import UserNotifications
import AppKit

final class MacAppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        // Refresh the running Dock tile as well as the bundle's Finder icon.
        // macOS may retain the generic icon after replacing an installed build.
        if let url = Bundle.main.url(forResource: "AppIcon", withExtension: "icns"),
           let icon = NSImage(contentsOf: url) {
            NSApp.applicationIconImage = icon
        }
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
    }
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }
    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        DispatchQueue.main.async {
            NSApp.activate(ignoringOtherApps: true)
            NSApp.windows.first?.makeKeyAndOrderFront(nil)
        }
        completionHandler()
    }
}

@main
struct CallWebhookMacApp: App {
    @NSApplicationDelegateAdaptor(MacAppDelegate.self) private var delegate
    @StateObject private var sip = MacSIPClient()
    var body: some Scene {
        WindowGroup("CallWebhook · Festnetz") {
            MacMainView(sip: sip)
                .frame(minWidth: 760, minHeight: 520)
        }
    }
}

struct MacMainView: View {
    @ObservedObject var sip: MacSIPClient
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
                    if sip.incoming { Button("Annehmen") { Task { await sip.answer() } }.tint(.green) }
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
                            Text("\(record.direction == "incoming" ? "Eingehend" : "Ausgehend") · \(record.status) · \(Int(record.duration)) s").foregroundStyle(.secondary)
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
            if !configuration.host.isEmpty {
                do { try sip.start(configuration) } catch { sip.error = error.localizedDescription }
            } else { settings = true }
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
