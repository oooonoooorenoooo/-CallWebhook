import SwiftUI
import UIKit
import Contacts
import LiveCommunicationKit
import AVKit

struct ContentView: View {
    @AppStorage("primaryPhoneNumber") private var primaryPhoneNumber = ""
    @AppStorage("secondaryPhoneNumber") private var secondaryPhoneNumber = ""
    @EnvironmentObject var monitor: CallMonitor
    @StateObject private var dialer = DialerModel()

    var body: some View {
        TabView {
            ContactsView()
                .tabItem { Label("Kontakte", systemImage: "person.crop.circle.fill") }

            CallsView(dialer: dialer)
                .tabItem { Label("Anrufe", systemImage: "clock.fill") }

            MailboxView(dialer: dialer)
                .tabItem { Label("Mailbox", systemImage: "recordingtape") }

            DialPadView(dialer: dialer, primaryPhoneNumber: primaryPhoneNumber, secondaryPhoneNumber: secondaryPhoneNumber)
                .environmentObject(monitor)
                .tabItem { Label("Zifferblatt", systemImage: "circle.grid.3x3.fill") }

            ExtrasView()
                .tabItem { Label("Extras", systemImage: "ellipsis.circle.fill") }
        }
        .tint(.blue)
    }
}

private struct CallsView: View {
    @EnvironmentObject var monitor: CallMonitor
    @ObservedObject var dialer: DialerModel
    @StateObject private var history = CallHistoryModel()
    @State private var selection = 0
    @State private var searchText = ""

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
                        ContentUnavailableView("Anrufliste nicht verfügbar", systemImage: "exclamationmark.triangle", description: Text(error))
                    } else if filteredCalls.isEmpty {
                        ContentUnavailableView(searchText.isEmpty ? "Keine Anrufe" : "Keine Treffer", systemImage: "phone", description: Text("Die Mobilfunk-Anrufhistorie erscheint hier."))
                    } else {
                        List(filteredCalls) { call in
                            Button {
                                guard let number = call.handles.first?.value, !number.isEmpty else { return }
                                dialer.call(number)
                            } label: {
                                HStack(spacing: 12) {
                                    Image(systemName: directionIcon(call))
                                        .frame(width: 28)
                                    VStack(alignment: .leading, spacing: 3) {
                                        Text(call.handles.first?.value ?? "Unbekannt").font(.headline)
                                        Text(directionText(call)).font(.caption).foregroundStyle(.secondary)
                                        Text("Status: \(String(describing: call.status))")
                                            .font(.caption2)
                                            .foregroundStyle(.secondary)
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
                            .disabled((call.handles.first?.value ?? "").isEmpty)
                        }
                        .listStyle(.plain)
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
            .task { await history.refresh() }
        }
    }

    private var filteredCalls: [ConversationHistoryManager.RecentConversation] {
        history.conversations.filter {
            searchText.isEmpty || ($0.handles.first?.value ?? "").localizedCaseInsensitiveContains(searchText)
        }
    }

    private func directionText(_ call: ConversationHistoryManager.RecentConversation) -> String {
        String(describing: call.direction).lowercased().contains("incoming") ? "Eingehend" : "Ausgehend"
    }

    private func directionIcon(_ call: ConversationHistoryManager.RecentConversation) -> String {
        String(describing: call.direction).lowercased().contains("incoming") ? "phone.arrow.down.left" : "phone.arrow.up.right"
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

    @State private var area = ContactArea.privateContacts
    @State private var contacts: [CNContact] = []
    @AppStorage("contactSort") private var sortValue = ContactSort.firstName.rawValue

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

                Picker("Sortierung", selection: $sortValue) {
                    ForEach(ContactSort.allCases, id: \.self) { option in
                        Text(option.rawValue).tag(option.rawValue)
                    }
                }
                .pickerStyle(.menu)
                .frame(maxWidth: .infinity, alignment: .trailing)
                .padding(.horizontal)

                List(contacts, id: \.identifier) { contact in
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
            }
            .navigationTitle("Kontakte")
            .navigationBarTitleDisplayMode(.inline)
            .task { loadContacts() }
        }
    }

    private func loadContacts() {
        let store = CNContactStore()
        let keys: [CNKeyDescriptor] = [
            CNContactFormatter.descriptorForRequiredKeys(for: .fullName),
            CNContactOrganizationNameKey as CNKeyDescriptor,
            CNContactThumbnailImageDataKey as CNKeyDescriptor
        ]
        func fetchContacts() {
            let request = CNContactFetchRequest(keysToFetch: keys)
            var loaded: [CNContact] = []
            do {
                try store.enumerateContacts(with: request) { contact, _ in
                    loaded.append(contact)
                }
                DispatchQueue.main.async {
                    contacts = loaded
                }
            } catch {
                DispatchQueue.main.async {
                    contacts = []
                }
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


private struct MailboxView: View {
    @ObservedObject var dialer: DialerModel
    @StateObject private var mailbox = MailboxModel()
    @AppStorage("mailboxNumber") private var mailboxNumber = ""
    @State private var playingMessage: MailboxMessage?

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
                            playingMessage = message
                        } label: {
                            HStack(spacing: 12) {
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
                    }
                    .listStyle(.plain)
                    .refreshable { await mailbox.refresh() }
                }
            }
            .navigationTitle("Mailbox")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItemGroup(placement: .topBarTrailing) {
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
    let primaryPhoneNumber: String
    let secondaryPhoneNumber: String
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
                    if !secondaryPhoneNumber.isEmpty {
                        Text("SIM 2  \(secondaryPhoneNumber)")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                    }
                }
                .frame(height: 38)

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
                            guard !dialer.number.isEmpty else { return }
                            dialer.deleteLast()
                        }
                        .onLongPressGesture(minimumDuration: 0.6, maximumDistance: 30) {
                            guard !dialer.number.isEmpty else { return }
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

                HStack(spacing: 22) {
                    if !secondaryPhoneNumber.isEmpty {
                        callButton(line: 1)
                        endCallButton
                        callButton(line: 2)
                    } else {
                        callButton(line: nil)
                        endCallButton
                    }
                }

                Text(dialer.status)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .frame(height: 20)

                Spacer()
            }
            .padding(.horizontal)
            .navigationTitle("Zifferblatt")
        }
    }

    private var endCallButton: some View {
        Button {
            // Die echte Beenden-Aktion wird mit der Default-Dialer-Steuerung verbunden.
        } label: {
            Image(systemName: "phone.down.fill")
                .font(.system(size: 27, weight: .semibold))
                .foregroundStyle(.white)
                .frame(width: 72, height: 72)
                .background(monitor.active ? Color.red : Color.gray.opacity(0.45), in: Circle())
        }
        .buttonStyle(.plain)
        .disabled(!monitor.active)
        .accessibilityLabel("Anruf beenden")
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
    @EnvironmentObject var monitor: CallMonitor
    private enum PhoneField: Hashable { case primary, secondary }
    @FocusState private var focusedPhoneField: PhoneField?
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
    @State private var showMobile = false
    @State private var showHomeAssistant = false
    @State private var showCallFilter = false
    @State private var showMailbox = false
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
                DisclosureGroup("Mobilfunk / Dual-SIM", isExpanded: $showMobile) {
                    TextField("Primäre Rufnummer", text: $primaryPhoneNumber)
                        .keyboardType(.phonePad)
                        .focused($focusedPhoneField, equals: .primary)
                        .submitLabel(.done)
                    TextField("Zweite Rufnummer", text: $secondaryPhoneNumber)
                        .keyboardType(.phonePad)
                        .focused($focusedPhoneField, equals: .secondary)
                        .submitLabel(.done)

                    Button("Tastatur schließen") {
                        focusedPhoneField = nil
                    }
                    .disabled(focusedPhoneField == nil)
                }

                DisclosureGroup("Mailbox", isExpanded: $showMailbox) {
                    TextField("Mailbox-Rufnummer", text: $mailboxNumber)
                        .keyboardType(.phonePad)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()

                    HStack {
                        Button("Telekom 3311") { mailboxNumber = "3311" }
                        Spacer()
                        Button("Vodafone 5500") { mailboxNumber = "5500" }
                        Spacer()
                        Button("O2 333") { mailboxNumber = "333" }
                    }

                    Text("Die Nummer wird lokal gespeichert. Im Mailbox-Reiter kann sie anschließend direkt über Mobilfunk angerufen werden.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                DisclosureGroup("Home Assistant", isExpanded: $showHomeAssistant) {
                    LabeledContent("iPhone Telefonstatus", value: monitor.haState)
                    LabeledContent("FRITZ!Box Anrufmonitor", value: monitor.fritzCallState)

                    Section("Background-Diagnose") {
                        LabeledContent("Background", value: monitor.backgroundStatus)
                        LabeledContent("Restlaufzeit", value: monitor.backgroundRemaining)
                        LabeledContent("Letztes Ereignis", value: monitor.backgroundLastEvent)
                        Text("Die Diagnose startet automatisch, sobald CallWebhook in den Hintergrund wechselt. Zum Testen iPhone sperren und CallWebhook zwischendurch nicht erneut öffnen.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }

                    Picker("HA-Schalter aktivieren bei", selection: $monitor.haTriggerMode) {
                        Text("Klingeln").tag("ringing")
                        Text("Gespräch verbunden").tag("connected")
                    }
                    .pickerStyle(.menu)

                    Text(monitor.haTriggerMode == "ringing" ? "Der HA-Schalter wird bereits beim Klingeln bzw. Start eines ausgehenden Anrufs aktiviert." : "Der HA-Schalter wird erst aktiviert, wenn das Gespräch tatsächlich verbunden ist.")
                        .font(.caption)
                        .foregroundStyle(.secondary)

                    Button("HA-Status aktualisieren") {
                        monitor.refreshHAState()
                    }

                    Button("Aktuellen Telefonstatus senden") {
                        monitor.sendCurrentState()
                    }

                    SecureField("Long-Lived Access Token von Home Assistant eintragen", text: $monitor.haToken)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .textContentType(.password)

                    Button("Token speichern & testen") {
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
                        TextField("HTTPS-URL (TXT / CSV / JSON)", text: $externalListURL)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .keyboardType(.URL)
                        Toggle("Externe Liste aktiv", isOn: $externalListEnabled)
                            .disabled(externalListURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)

                        Button("Liste jetzt laden") {
                            // Netzwerkimport und Parser werden als eigener Dienst angebunden.
                        }
                        .disabled(!externalListEnabled || externalListURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
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
                        focusedPhoneField = nil
                    }
                }
            }
            .scrollDismissesKeyboard(.interactively)
            .onTapGesture {
                focusedPhoneField = nil
            }
        }
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
