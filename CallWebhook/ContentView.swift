import SwiftUI

struct ContentView: View {
    @EnvironmentObject var monitor: CallMonitor
    @StateObject private var dialer = DialerModel()
    @State private var showToken = false

    private let rows = [
        ["1", "2", "3"],
        ["4", "5", "6"],
        ["7", "8", "9"],
        ["*", "0", "#"]
    ]

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 14) {
                    Image(systemName: monitor.active ? "phone.fill" : "phone")
                        .font(.system(size: 44))
                    Text(monitor.active ? "Telefon aktiv" : "Kein Telefonat")
                        .font(.title2.bold())

                    TextField("Telefonnummer", text: $dialer.number)
                        .keyboardType(.phonePad)
                        .textFieldStyle(.roundedBorder)
                        .font(.title2.monospacedDigit())
                        .padding(.horizontal)

                    ForEach(rows, id: \.self) { row in
                        HStack(spacing: 24) {
                            ForEach(row, id: \.self) { digit in
                                Button(digit) { dialer.append(digit) }
                                    .font(.title.bold())
                                    .frame(width: 64, height: 52)
                                    .buttonStyle(.bordered)
                            }
                        }
                    }

                    HStack(spacing: 24) {
                        Button {
                            dialer.call()
                        } label: {
                            Image(systemName: "phone.fill")
                                .font(.title2)
                                .frame(width: 64, height: 44)
                        }
                        .buttonStyle(.borderedProminent)

                        Button {
                            dialer.deleteLast()
                        } label: {
                            Image(systemName: "delete.left")
                                .font(.title2)
                                .frame(width: 64, height: 44)
                        }
                        .buttonStyle(.bordered)
                    }

                    Text(dialer.status)
                        .font(.caption)
                        .foregroundStyle(.secondary)

                    Divider()

                    HStack {
                        Circle()
                            .frame(width: 12, height: 12)
                            .opacity((monitor.haState == "ON" || monitor.haState == "OFF") ? 1 : 0.35)
                        Text("Home Assistant: \(monitor.haState)")
                            .font(.headline)
                    }

                    HStack {
                        Button("HA aktualisieren") { monitor.refreshHAState() }
                            .buttonStyle(.bordered)
                        Button("Status senden") { monitor.sendCurrentState() }
                            .buttonStyle(.bordered)
                    }

                    DisclosureGroup("Home-Assistant-Token", isExpanded: $showToken) {
                        SecureField("Long-Lived Access Token", text: $monitor.haToken)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .textContentType(.password)
                            .padding(.vertical, 8)
                        Button("Token speichern & testen") {
                            monitor.refreshHAState()
                            showToken = false
                        }
                    }
                    .padding(.horizontal)

                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(monitor.log.prefix(12), id: \.self) { entry in
                            Text(entry).font(.caption2.monospaced())
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal)
                }
                .padding(.vertical)
            }
            .navigationTitle("CallWebhook Dialer")
        }
    }
}
