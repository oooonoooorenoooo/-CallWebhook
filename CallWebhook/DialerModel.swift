import Foundation
import LiveCommunicationKit

@MainActor
final class DialerModel: ObservableObject {
    @Published var number = ""
    @Published private(set) var status = "Bereit"
    @Published private(set) var lastDialedNumber = UserDefaults.standard.string(forKey: "lastDialedNumber") ?? ""
    private let sip = SIPService.shared

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
        lastDialedNumber = value
        UserDefaults.standard.set(value, forKey: "lastDialedNumber")
        number = value

        if UserDefaults.standard.bool(forKey: "sipEnabled") {
            do {
                try sip.call(value)
                status = "Asterisk/SIP: \(value)"
            } catch {
                status = "SIP-Fehler: \(error.localizedDescription)"
            }
            return
        }

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

    func hangup() {
        if UserDefaults.standard.bool(forKey: "sipEnabled") {
            sip.hangup()
            status = "SIP-Anruf beendet"
        }
    }
}
