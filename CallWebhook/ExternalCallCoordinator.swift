import AVFoundation
import Intents

@MainActor
final class ExternalCallCoordinator {
    static let shared = ExternalCallCoordinator()
    let dialer = DialerModel()
    private var handled: [String: Date] = [:]

    func start(number: String, line: Int = 1) -> String? {
        guard let number = ExternalCallRequest.number([number]) else { return "Keine eindeutige Telefonnummer verfügbar." }
        if CellularRouting.cellularOnlyNumber(number) != nil {
            dialer.call(number)
            return nil
        }
        guard UserDefaults.standard.bool(forKey: "setupCompleted") else { return "Bitte die Einrichtung auf dem iPhone abschließen." }
        guard !dialer.isDialing, !SIPService.shared.active, !IncomingCallProvider.shared.hasCall else { return "Es läuft bereits ein Anruf." }
        guard AVAudioApplication.shared.recordPermission == .granted else { return "Bitte den Mikrofonzugriff auf dem iPhone erlauben." }
        guard line == 1 || ((2...3).contains(line) && UserDefaults.standard.bool(forKey: "sipLine\(line)Enabled")) else { return "Diese Leitung ist nicht eingerichtet." }
        dialer.number = number
        dialer.call(line: line)
        return nil
    }

    @discardableResult
    func continueCall(_ activity: NSUserActivity) -> Bool {
        let handles: [String]
        if let intent = activity.interaction?.intent as? INStartCallIntent {
            guard intent.callCapability != .videoCall else { return false }
            handles = (intent.contacts ?? []).compactMap { $0.personHandle?.type == .phoneNumber ? $0.personHandle?.value : nil }
            guard handles.count == intent.contacts?.count else { return false }
        } else if let intent = activity.interaction?.intent as? INStartAudioCallIntent {
            handles = (intent.contacts ?? []).compactMap { $0.personHandle?.type == .phoneNumber ? $0.personHandle?.value : nil }
            guard handles.count == intent.contacts?.count else { return false }
        } else { return false }
        guard let number = ExternalCallRequest.number(handles) else { return false }
        let key = activity.interaction?.identifier ?? number
        handled = handled.filter { Date().timeIntervalSince($0.value) < 15 }
        if handled[key] != nil { return true }
        guard start(number: number) == nil else { return false }
        handled[key] = Date()
        return true
    }
}

// Siri resolves the telephone handle first. The actual call is started only
// when iOS continues the explicit call intent in the phone or CarPlay scene.
final class StartCallIntentHandler: NSObject, INStartCallIntentHandling {
    func resolveContacts(for intent: INStartCallIntent, with completion: @escaping ([INStartCallContactResolutionResult]) -> Void) {
        guard let contacts = intent.contacts, contacts.count == 1,
              let person = contacts.first, person.personHandle?.type == .phoneNumber,
              ExternalCallRequest.number([person.personHandle?.value ?? ""]) != nil else {
            completion([.needsValue()]); return
        }
        completion([.success(with: person)])
    }

    func handle(intent: INStartCallIntent, completion: @escaping (INStartCallIntentResponse) -> Void) {
        guard intent.callCapability != .videoCall, let contacts = intent.contacts, contacts.count == 1,
              contacts[0].personHandle?.type == .phoneNumber,
              ExternalCallRequest.number([contacts[0].personHandle?.value ?? ""]) != nil else {
            completion(INStartCallIntentResponse(code: .failure, userActivity: nil)); return
        }
        completion(INStartCallIntentResponse(code: .continueInApp, userActivity: nil))
    }
}
