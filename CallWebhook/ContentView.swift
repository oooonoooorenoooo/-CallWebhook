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

    var body: some View {
        NavigationStack {
            Group {
                if monitor.log.isEmpty {
                    ContentUnavailableView("Keine Anrufe", systemImage: "phone", description: Text("Die Anrufhistorie erscheint hier."))
                } else {
                    List(monitor.log, id: \.self) { entry in
                        Label(entry, systemImage: "phone")
                            .font(.callout)
                    }
                }
            }
            .navigationTitle("Anrufe")
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

                HStack(spacing: 42) {
                    Color.clear.frame(width: 72, height: 72)

                    Button {
                        dialer.call()
                    } label: {
                        Image(systemName: "phone.fill")
                            .font(.system(size: 30, weight: .semibold))
                            .foregroundStyle(.white)
                            .frame(width: 72, height: 72)
                            .background(.green, in: Circle())
                    }
                    .buttonStyle(.plain)

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
}

private struct ExtrasView: View {
    @EnvironmentObject var monitor: CallMonitor
    @FocusState private var phoneFieldFocused: Bool
    @AppStorage("primaryPhoneNumber") private var primaryPhoneNumber = ""
    @AppStorage("secondaryPhoneNumber") private var secondaryPhoneNumber = ""
    @State private var showToken = false

    var body: some View {
        NavigationStack {
            Form {
                Section("Mobilfunk / Dual-SIM") {
                    TextField("Primäre Rufnummer", text: $primaryPhoneNumber)
                        .keyboardType(.phonePad)
                        .focused($phoneFieldFocused)
                    TextField("Zweite Rufnummer", text: $secondaryPhoneNumber)
                        .keyboardType(.phonePad)
                        .focused($phoneFieldFocused)
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
                        phoneFieldFocused = false
                    }
                }
            }
            .onChange(of: primaryPhoneNumber) { _, _ in }
        }
    }
}
