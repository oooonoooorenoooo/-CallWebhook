import SwiftUI

struct ContentView: View {
    @AppStorage("primaryPhoneNumber") private var primaryPhoneNumber = ""
    @AppStorage("secondaryPhoneNumber") private var secondaryPhoneNumber = ""
    @EnvironmentObject var monitor: CallMonitor
    @StateObject private var dialer = DialerModel()

    var body: some View {
        TabView {
            CallsView()
                .tabItem { Label("Anrufe", systemImage: "clock.fill") }

            ContactsView()
                .tabItem { Label("Kontakte", systemImage: "person.crop.circle.fill") }

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
    @State private var selection = 0
    @State private var searchText = ""
    @State private var filter = CallHistoryFilter.all

    private enum CallHistoryFilter: String, CaseIterable, Identifiable {
        case all = "Alle"
        case phoneNumber = "Telefonnummer"
        case contact = "Kontakt"
        case date = "Datum"
        case incoming = "Eingehend"
        case outgoing = "Ausgehend"
        case missed = "Verpasst"
        case voicemail = "Voicemail"

        var id: Self { self }
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                Picker("Ansicht", selection: $selection) {
                    Text("Anrufe").tag(0)
                    Text("HA-Status").tag(1)
                }
                .pickerStyle(.segmented)
                .padding(.horizontal)
                .padding(.vertical, 8)

                if selection == 0 {
                    VStack(spacing: 8) {
                        Picker("Filter", selection: $filter) {
                            ForEach(CallHistoryFilter.allCases) { item in
                                Text(item.rawValue).tag(item)
                            }
                        }
                        .pickerStyle(.menu)
                        .frame(maxWidth: .infinity, alignment: .trailing)
                        .padding(.horizontal)

                        ContentUnavailableView(
                            searchText.isEmpty ? "Keine Anrufe" : "Keine Treffer",
                            systemImage: "phone",
                            description: Text(searchText.isEmpty
                                ? "Die Anrufhistorie erscheint hier."
                                : "Kein Anruf entspricht dem aktuellen Filter.")
                        )
                    }
                    .searchable(
                        text: $searchText,
                        placement: .navigationBarDrawer(displayMode: .always),
                        prompt: filter == .all ? "Nummer, Kontakt oder Datum" : filter.rawValue
                    )
                } else {
                    List(monitor.log, id: \.self) { entry in
                        Label(entry, systemImage: "house.fill")
                            .font(.callout)
                    }
                    .listStyle(.plain)
                }
            }
            .navigationTitle("Anrufe")
            .navigationBarTitleDisplayMode(.inline)
        }
    }
}

private struct ContactsView: View {
    var body: some View {
        NavigationStack {
            ContentUnavailableView(
                "Kontakte",
                systemImage: "person.crop.circle",
                description: Text("Die iPhone-Kontakte werden hier eingebunden.")
            )
            .navigationTitle("Kontakte")
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
                        Text(primaryPhoneNumber)
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                    }
                    if !secondaryPhoneNumber.isEmpty {
                        Text(secondaryPhoneNumber)
                            .font(.caption)
                            .foregroundStyle(.tertiary)
                    }
                }
                .frame(height: 38)

                Spacer()

                Text(dialer.number.isEmpty ? " " : dialer.number)
                    .font(.system(size: 34, weight: .regular, design: .rounded))
                    .minimumScaleFactor(0.6)
                    .lineLimit(1)
                    .frame(height: 44)

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
    @State private var showMobile = true
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
                DisclosureGroup("Mobilfunk / Dual-SIM", isExpanded: $showMobile) {
                    TextField("Primäre Rufnummer", text: $primaryPhoneNumber)
                        .keyboardType(.phonePad)
                        .focused($focusedPhoneField, equals: .primary)
                    TextField("Zweite Rufnummer", text: $secondaryPhoneNumber)
                        .keyboardType(.phonePad)
                        .focused($focusedPhoneField, equals: .secondary)
                }

                DisclosureGroup("Home Assistant", isExpanded: $showHomeAssistant) {
                    LabeledContent("Telefonstatus", value: monitor.haState)

                    Button("HA-Status aktualisieren") {
                        monitor.refreshHAState()
                    }

                    Button("Aktuellen Telefonstatus senden") {
                        monitor.sendCurrentState()
                    }

                    SecureField("Long-Lived Access Token", text: $monitor.haToken)
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
