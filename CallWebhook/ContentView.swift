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

                HStack(spacing: 28) {
                    if !secondaryPhoneNumber.isEmpty {
                        callButton(line: 1)
                        callButton(line: 2)
                    } else {
                        callButton(line: nil)
                    }

                    Button {
                        dialer.deleteLast()
                    } label: {
                        Image(systemName: "delete.left")
                            .font(.title2)
                            .frame(width: 72, height: 72)
                    }
                    .buttonStyle(.plain)
                    .opacity(dialer.number.isEmpty ? 0 : 1)
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
    @State private var showToken = false

    var body: some View {
        NavigationStack {
            Form {
                Section("Mobilfunk / Dual-SIM") {
                    TextField("Primäre Rufnummer", text: $primaryPhoneNumber)
                        .keyboardType(.phonePad)
                        .focused($focusedPhoneField, equals: .primary)
                    TextField("Zweite Rufnummer", text: $secondaryPhoneNumber)
                        .keyboardType(.phonePad)
                        .focused($focusedPhoneField, equals: .secondary)
                    Text("Die Nummern werden lokal gespeichert. Die Leitungsauswahl beim Anruf übernimmt iOS bzw. die Mobilfunk-Dialer-Schnittstelle.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                Section("Home Assistant") {
                    LabeledContent("Telefonstatus", value: monitor.haState)

                    Button("HA-Status aktualisieren") {
                        monitor.refreshHAState()
                    }

                    Button("Aktuellen Telefonstatus senden") {
                        monitor.sendCurrentState()
                    }
                }

                Section("Verbindung") {
                    DisclosureGroup("Long-Lived Access Token", isExpanded: $showToken) {
                        SecureField("Token", text: $monitor.haToken)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .textContentType(.password)

                        Button("Token speichern & testen") {
                            monitor.refreshHAState()
                            showToken = false
                        }
                    }
                }

                Section("Diagnose") {
                    LabeledContent("CallKit", value: monitor.active ? "Telefon aktiv" : "Bereit")
                    Text(monitor.lastEvent)
                        .foregroundStyle(.secondary)

                    ForEach(monitor.log.prefix(8), id: \.self) { entry in
                        Text(entry)
                            .font(.caption.monospaced())
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
}
