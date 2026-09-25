import Foundation
import LiveCommunicationKit

@MainActor
final class DialerModel: ObservableObject {
    @Published var number = ""
    @Published private(set) var status = "Bereit"

    func append(_ digit: String) {
        number.append(digit)
    }

    func deleteLast() {
        if !number.isEmpty { number.removeLast() }
    }

    func call() {
        call(number)
    }

    func call(_ phoneNumber: String) {
        let value = phoneNumber.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else { return }
        status = "Anruf wird gestartet …"
        Task {
            do {
                let handle = Handle(type: .phoneNumber, value: value)
                let action = StartCellularConversationAction(handle)
                try await TelephonyConversationManager.sharedInstance.startCellularConversation(action)
                status = "Mobilfunkanruf gestartet"
            } catch {
                status = "Fehler: \(error.localizedDescription)"
            }
        }
    }
}
