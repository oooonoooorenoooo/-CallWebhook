import Foundation
import LiveCommunicationKit
import UIKit
import AVFoundation
import Combine

@MainActor
final class DialerModel: ObservableObject {
    @Published var number = ""
    @Published private(set) var status = "Bereit"
    @Published private(set) var lastDialedNumber = UserDefaults.standard.string(forKey: "lastDialedNumber") ?? ""
    @Published private(set) var isDialing = false
    private var dialingTask: Task<Void, Never>?
    private let sip = SIPService.shared
    private var callStateObservation: AnyCancellable?
    private var wasSIPActive = false

    init() {
        // Observe the service, not a particular view: remote hangup and CallKit
        // ending a call must clear the field even while another tab is visible.
        callStateObservation = sip.$active.removeDuplicates().sink { [weak self] active in
            guard let self else { return }
            if self.wasSIPActive && !active { self.number = "" }
            self.wasSIPActive = active
        }
    }

    func recallLastNumber() {
        guard !sip.active, !isDialing, !lastDialedNumber.isEmpty else { return }
        number = lastDialedNumber
    }

    func append(_ digit: String) {
        // During a call, keypad input belongs to the remote voice menu. Never
        // append it to the destination number that will be used on the next call.
        if sip.active {
            do { try sip.sendDTMF(digit) }
            catch { status = "Tastenton konnte nicht gesendet werden: \(error.localizedDescription)" }
            return
        }
        guard !isDialing else { return }
        number.append(digit)
    }

    func deleteLast() {
        guard !sip.active, !isDialing else { return }
        if !number.isEmpty { number.removeLast() }
    }

    func call() {
        call(number)
    }

    func call(line: Int) {
        let value = number.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else { status = "Bitte zuerst eine Rufnummer eingeben"; return }
        if let cellularNumber = CellularRouting.cellularOnlyNumber(value) {
            callSystemNumber(cellularNumber)
            return
        }
        if MobileForwarding.isNetworkCode(value) {
            openCellularNetworkCode(value)
            return
        }
        guard !isDialing, !sip.active else { return }
        status = "Anruf wird gestartet …"
        lastDialedNumber = value
        UserDefaults.standard.set(value, forKey: "lastDialedNumber")
        startSIPCall(value, line: line)
    }

    func call(_ phoneNumber: String) {
        let value = phoneNumber.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else { status = "Bitte zuerst eine Rufnummer eingeben"; return }
        if let cellularNumber = CellularRouting.cellularOnlyNumber(value) {
            callSystemNumber(cellularNumber)
            return
        }
        if MobileForwarding.isNetworkCode(value) {
            openCellularNetworkCode(value)
            return
        }
        guard !isDialing, !sip.active else { return }
        status = "Anruf wird gestartet …"
        lastDialedNumber = value
        UserDefaults.standard.set(value, forKey: "lastDialedNumber")
        number = value

        if UserDefaults.standard.bool(forKey: "sipEnabled") {
            startSIPCall(value, line: 1)
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

    private func startSIPCall(_ value: String, line: Int) {
        guard !isDialing, !sip.active else { status = "Ein Anruf läuft bereits"; return }
        isDialing = true
        dialingTask = Task {
            defer { isDialing = false }
            do {
                let allowed = await withCheckedContinuation { continuation in
                    AVAudioSession.sharedInstance().requestRecordPermission { granted in
                        continuation.resume(returning: granted)
                    }
                }
                try Task.checkCancellation()
                guard allowed else {
                    status = "Mikrofonzugriff fehlt. Bitte in den iPhone-Einstellungen für CallWebhook erlauben."
                    return
                }
                try sip.ensureStarted()
                status = "Verbinde mit Asterisk …"
                let deadline = Date().addingTimeInterval(15)
                while !sip.registered && Date() < deadline {
                    try await Task.sleep(for: .milliseconds(200))
                }
                try Task.checkCancellation()
                guard sip.registered else { throw SIPService.SIPError.notRegistered }
                try AVAudioSession.sharedInstance().setCategory(.playAndRecord, mode: .voiceChat, options: [.allowBluetooth])
                try AVAudioSession.sharedInstance().setActive(true)
                try sip.call(value, line: line)
                status = "Leitung \(line): \(value)"
            } catch is CancellationError {
                if number == value { number = "" }
                status = "Anrufaufbau abgebrochen"
            } catch {
                if number == value { number = "" }
                status = "Anruf fehlgeschlagen: \(error.localizedDescription)"
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
        let wasStarting = isDialing
        dialingTask?.cancel()
        if wasStarting { number = "" }
        if UserDefaults.standard.bool(forKey: "sipEnabled") {
            sip.hangup()
            status = "SIP-Anruf beendet"
        }
    }
}
