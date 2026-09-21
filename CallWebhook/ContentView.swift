import SwiftUI

struct ContentView: View {
    @EnvironmentObject var monitor: CallMonitor

    var body: some View {
        NavigationStack {
            VStack(spacing: 18) {
                Image(systemName: monitor.active ? "phone.fill" : "phone")
                    .font(.system(size: 58))
                Text(monitor.active ? "Telefon aktiv" : "Kein Telefonat")
                    .font(.title2.bold())
                Text(monitor.lastEvent)
                    .foregroundStyle(.secondary)
                Button("Status jetzt an Home Assistant senden") {
                    monitor.sendCurrentState()
                }
                .buttonStyle(.borderedProminent)
                List(monitor.log, id: \.self) { entry in
                    Text(entry)
                        .font(.caption.monospaced())
                }
            }
            .padding(.top)
            .navigationTitle("CallWebhook")
        }
    }
}
