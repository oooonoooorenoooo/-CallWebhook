import SwiftUI
import UIKit

/// Native row actions remain visible in the swiped history entry.
struct CallHistorySwipeActions: View {
    let call: CallRecord
    let isArchived: Bool
    @ObservedObject var dialer: DialerModel
    let onDelete: () -> Void
    let onArchive: () -> Void
    @ObservedObject private var sip = SIPService.shared
    @AppStorage("sipLine2Enabled") private var line2Enabled = false
    @AppStorage("sipLine3Enabled") private var line3Enabled = false

    var body: some View {
        ForEach(1...3, id: \.self) { line in
            Button {
                guard lineEnabled(line), !sip.active, !dialer.isDialing, !call.number.isEmpty else { return }
                dialer.number = call.number
                dialer.call(line: line)
            } label: {
                Label {
                    Text(line == 3 ? "Festnetz" : "SIM \(line)")
                } icon: {
                    Image(uiImage: CallHistoryLineIcons.image(line)).renderingMode(.template)
                }
            }
            .tint(lineEnabled(line) ? .green : .gray)
            .disabled(!lineEnabled(line) || call.number.isEmpty || sip.active || dialer.isDialing)
            .accessibilityLabel("Über \(line == 3 ? "Festnetz" : "SIM \(line)") anrufen")
        }
        // Confirmation happens before removal, so do not let a destructive
        // swipe optimistically animate the row out of the list.
        Button(action: onDelete) { Label("Löschen", systemImage: "trash") }
            .tint(.red)
        Button(action: onArchive) {
            Label(isArchived ? "Archiviert" : "Archivieren", systemImage: "folder")
        }
        .tint(.blue)
        .disabled(isArchived || call.endedAt == nil)
    }

    private func lineEnabled(_ line: Int) -> Bool {
        line == 1 || (line == 2 && line2Enabled) || (line == 3 && line3Enabled)
    }
}

/// Embed the line number in the icon so it stays visible even when iOS hides
/// action titles to fit all five buttons into the row.
@MainActor
private enum CallHistoryLineIcons {
    private static let images = (1...3).map { line in
        UIGraphicsImageRenderer(size: CGSize(width: 32, height: 28)).image { _ in
            UIImage(systemName: "phone.fill")?
                .withTintColor(.black, renderingMode: .alwaysOriginal)
                .draw(in: CGRect(x: 0, y: 6, width: 22, height: 22))
            ("\(line)" as NSString).draw(
                at: CGPoint(x: 23, y: 0),
                withAttributes: [.font: UIFont.boldSystemFont(ofSize: 13), .foregroundColor: UIColor.black]
            )
        }
    }

    static func image(_ line: Int) -> UIImage { images[line - 1] }
}
