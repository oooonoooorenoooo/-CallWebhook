import SwiftUI

struct CallHistoryActionsView: View {
    let call: CallRecord
    let isArchived: Bool
    let isDialing: Bool
    let onCall: (Int) -> Void
    let onDelete: () -> Void
    let onArchive: () -> Void
    @ObservedObject private var sip = SIPService.shared
    @AppStorage("sipLine2Enabled") private var line2Enabled = false
    @AppStorage("sipLine3Enabled") private var line3Enabled = false
    @Environment(\.dismiss) private var dismiss
    @State private var confirmDelete = false

    var body: some View {
        NavigationStack {
            VStack(spacing: 24) {
                VStack(spacing: 4) {
                    Text(call.number.isEmpty ? "Unbekannt" : call.number).font(.title3.bold())
                    Text("\(call.lineLabel) · \(call.date.formatted(date: .abbreviated, time: .shortened))")
                        .font(.caption).foregroundStyle(.secondary)
                }
                HStack(spacing: 16) {
                    ForEach(1...3, id: \.self) { line in
                        Button { onCall(line) } label: {
                            VStack(spacing: 8) {
                                Image(systemName: "phone.fill")
                                    .font(.title2)
                                    .frame(width: 56, height: 56)
                                    .background(.green, in: Circle())
                                    .foregroundStyle(.white)
                                    .overlay(alignment: .topTrailing) {
                                        Text("\(line)").font(.caption.bold())
                                            .frame(width: 23, height: 23)
                                            .background(.green.opacity(0.9), in: Circle())
                                            .foregroundStyle(.white)
                                    }
                                Text(line == 3 ? "Festnetz" : "SIM \(line)").font(.caption)
                            }
                            .frame(maxWidth: .infinity, minHeight: 88)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .disabled(call.number.isEmpty || sip.active || isDialing || !lineEnabled(line))
                        .opacity(lineEnabled(line) ? 1 : 0.35)
                        .accessibilityLabel("Über \(line == 3 ? "Festnetz" : "SIM \(line)") anrufen")
                    }
                }
                HStack(spacing: 16) {
                    Button(role: .destructive) { confirmDelete = true } label: {
                        Label("Löschen", systemImage: "trash")
                            .frame(maxWidth: .infinity, minHeight: 48)
                    }
                    .buttonStyle(.glass)
                    Button(action: onArchive) {
                        Label(isArchived ? "Archiviert" : "Archivieren", systemImage: "folder")
                            .frame(maxWidth: .infinity, minHeight: 48)
                    }
                    .buttonStyle(.glass)
                    .disabled(isArchived || call.endedAt == nil)
                }
            }
            .padding()
            .navigationTitle("Anrufaktionen")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Fertig") { dismiss() } } }
            .confirmationDialog("Anruf aus CallWebhook löschen?", isPresented: $confirmDelete, titleVisibility: .visible) {
                Button("Anruf löschen", role: .destructive, action: onDelete)
                Button("Abbrechen", role: .cancel) {}
            } message: {
                Text("Dieser Eintrag bleibt in CallWebhook auch nach erneutem Laden ausgeblendet.")
            }
        }
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
    }

    private func lineEnabled(_ line: Int) -> Bool {
        line == 1 || (line == 2 && line2Enabled) || (line == 3 && line3Enabled)
    }
}
