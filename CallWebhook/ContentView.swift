import SwiftUI

struct ContentView: View {
    @EnvironmentObject var monitor: CallMonitor
    @State private var showToken = false

    var body: some View {
        NavigationStack {
            VStack(spacing: 14) {
                Image(systemName: monitor.active ? "phone.fill" : "phone")
                    .font(.system(size: 52))
                Text(monitor.active ? "Telefon aktiv" : "Kein Telefonat")
                    .font(.title2.bold())
                Text(monitor.lastEvent)
                    .foregroundStyle(.secondary)

                HStack {
                    Circle()
                        .frame(width: 12, height: 12)
                        .opacity((monitor.haState == "ON" || monitor.haState == "OFF") ? 1 : 0.35)
                    Text("Home Assistant: \(monitor.haState)")
                        .font(.headline)
                }

                Button("HA-Status aktualisieren") {
                    monitor.refreshHAState()
                }
                .buttonStyle(.bordered)

                Button("Status jetzt an Home Assistant senden") {
                    monitor.sendCurrentState()
                }
                .buttonStyle(.borderedProminent)

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

                List(monitor.log, id: \.self) { entry in
                    Text(entry).font(.caption.monospaced())
                }
            }
            .padding(.top)
            .navigationTitle("CallWebhook")
        }
    }
}
