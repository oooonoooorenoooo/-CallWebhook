import Foundation
import LiveCommunicationKit
import UIKit

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

    func call(line: Int) {
        let value = number.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else { return }
        if let cellularNumber = CellularRouting.cellularOnlyNumber(value) {
            callSystemNumber(cellularNumber)
            return
        }
        if MobileForwarding.isNetworkCode(value) {
            openCellularNetworkCode(value)
            return
        }
        status = "Anruf wird gestartet …"
        lastDialedNumber = value
        UserDefaults.standard.set(value, forKey: "lastDialedNumber")
        do {
            try sip.call(value, line: line)
            status = "SIP Leitung \(line): \(value)"
        } catch {
            status = "SIP-Fehler: \(error.localizedDescription)"
        }
    }

    func call(_ phoneNumber: String) {
        let value = phoneNumber.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else { return }
        if let cellularNumber = CellularRouting.cellularOnlyNumber(value) {
            callSystemNumber(cellularNumber)
            return
        }
        if MobileForwarding.isNetworkCode(value) {
            openCellularNetworkCode(value)
            return
        }
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

    private func callSystemNumber(_ number: String) {
        status = "\(number) wird über iOS-Mobilfunk gewählt …"
        SystemCellularDialer.call(number) { opened in
            Task { @MainActor in
                self.status = opened
                    ? "\(number) an die iOS-Mobilfunktelefonie übergeben"
                    : "iOS konnte nicht geöffnet werden. Für einen Notruf die Notruffunktion des iPhones verwenden."
            }
        }
    }

    private func openCellularNetworkCode(_ code: String) {
        // Explicit call-button fallback. Never send carrier MMI through Asterisk or tel:
        // (tel: would route back into this app when it is the default calling app).
        var components = URLComponents()
        components.scheme = "telephony"
        components.path = code
        guard let url = components.url else { return }
        UIApplication.shared.open(url) { opened in
            Task { @MainActor in
                self.status = opened
                    ? "An Mobilfunk übergeben – SIM prüfen und Netzbestätigung abwarten"
                    : "iOS lehnt den Steuercode ab. In der Telefon-App auf der passenden SIM eingeben: \(code)"
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
