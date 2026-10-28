import Foundation
import Combine
import AVFoundation
import AppKit
import UserNotifications
import linphonesw
import OSLog

@MainActor
final class MacSIPClient: ObservableObject {
    @Published private(set) var status = "Nicht verbunden"
    @Published private(set) var registered = false
    @Published private(set) var incoming = false
    @Published private(set) var active = false
    @Published private(set) var caller = ""
    @Published var error: String?
    private let logger = Logger(subsystem: "de.reno.CallWebhookMac", category: "CallTiming")
    private var delegate: CoreDelegateStub?
    private var receivedAt: TimeInterval?
    private var acceptedElsewhere = false
    private var localEndReason: String?
    private var callFailed = false
    private var core: Core?
    private var timer: Timer?
    private var call: Call?
    private var historyID: UUID?
    private var host = ""
    private var ringTimer: Timer?
    private var notificationID: UUID?
    private var activity: NSObjectProtocol?
    private var observers: [NSObjectProtocol] = []

    init() {
        observers.append(NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in
                Task { @MainActor in self?.core?.refreshRegisters() }
            })
    }

    func start(_ configuration: MacConfiguration) throws {
        guard !active else { throw MacError.message("Zuerst das laufende Gespräch beenden.") }
        try configuration.validate()
        guard let password = MacSecrets.get("sip"), !password.isEmpty else {
            throw MacError.message("Passwort des separaten Mac-SIP-Zugangs fehlt.")
        }
        stop()
        let newCore = try Factory.Instance.createCore(configPath: "", factoryConfigPath: "", systemContext: nil)
        newCore.autoIterateEnabled = false
        newCore.maxCalls = 1
        newCore.videoCaptureEnabled = false
        newCore.videoDisplayEnabled = false
        newCore.addAuthInfo(info: try Factory.Instance.createAuthInfo(username: configuration.username,
            userid: nil, passwd: password, ha1: nil, realm: nil, domain: configuration.host))
        let params = try newCore.createAccountParams()
        try params.setIdentityaddress(newValue: Factory.Instance.createAddress(addr: "sip:\(configuration.username)@\(configuration.host)"))
        try params.setServeraddress(newValue: Factory.Instance.createAddress(addr: "sip:\(configuration.host);transport=udp"))
        params.registerEnabled = true
        let account = try newCore.createAccount(params: params)
        try newCore.addAccount(account: account)
        newCore.defaultAccount = account
        let delegate = CoreDelegateStub(onCallStateChanged: { [weak self] _, call, state, _ in
            // All SDK access, including iterate(), runs on the main actor.
            MainActor.assumeIsolated { self?.callChanged(call, state: state) }
        })
        self.delegate = delegate
        newCore.addDelegate(delegate: delegate)
        try newCore.start()
        core = newCore
        activity = ProcessInfo.processInfo.beginActivity(options: .userInitiatedAllowingIdleSystemSleep, reason: "Festnetzanrufe empfangen")
        host = configuration.host
        status = "Registrierung läuft …"
        let timer = Timer(timeInterval: 0.02, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.iterate() }
        }
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    func stop() {
        timer?.invalidate(); timer = nil
        stopRinging()
        if let historyID { LocalCallHistory.shared.end(historyID, reason: "interrupted") }
        historyID = nil; call = nil
        core?.stop(); core = nil; delegate = nil
        if let activity { ProcessInfo.processInfo.endActivity(activity) }
        activity = nil
        registered = false; active = false; incoming = false
        status = "Nicht verbunden"
    }

    private func iterate() {
        core?.iterate()
        registered = core?.defaultAccount?.state == .Ok
        if !active {
            if registered { status = "Festnetz bereit" }
            else if let info = core?.defaultAccount?.errorInfo, let phrase = info.phrase, !phrase.isEmpty {
                status = "Anmeldung fehlgeschlagen: \(phrase) (\(info.protocolCode))"
            } else { status = "Verbindung zur Telefonanlage wird hergestellt …" }
        }
    }

    private func callChanged(_ candidate: Call, state: Call.State) {
        if call == nil, state == .IncomingReceived {
            receivedAt = ProcessInfo.processInfo.systemUptime
            acceptedElsewhere = false; callFailed = false; localEndReason = nil
            // The dedicated endpoint and dialplan enforce line 3 on the server too.
            guard candidate.remoteParams?.getCustomHeader(headerName: "X-CallWebhook-Line") == "3" else {
                try? candidate.terminate(); return
            }
            call = candidate; incoming = true; active = true
            caller = candidate.remoteAddress?.username ?? "Unbekannt"
            let idHeader = candidate.remoteParams?.getCustomHeader(headerName: "X-CallWebhook-ID")
            let id = idHeader.flatMap(UUID.init(uuidString:)) ?? UUID()
            historyID = id
            logger.notice("incoming_received id=\(id.uuidString, privacy: .public) sip_call_id=\(candidate.callLog?.callId ?? "-", privacy: .public) wall=\(Date().timeIntervalSince1970, privacy: .public)")
            status = "Eingehend: \(caller)"
            // Present and sound the ring before encoding/persisting up to 1000 records.
            NSSound.beep()
            logger.notice("ring_requested wall=\(Date().timeIntervalSince1970, privacy: .public) callback_ms=\((ProcessInfo.processInfo.systemUptime - (self.receivedAt ?? 0)) * 1000, privacy: .public)")
            let ring = Timer(timeInterval: 3, repeats: true) { _ in NSSound.beep() }
            RunLoop.main.add(ring, forMode: .common); ringTimer = ring
            notify(id: id, caller: caller)
            LocalCallHistory.shared.begin(id: id, number: caller, incoming: true, line: 3)
        }
        guard let call, call === candidate else { return }
        switch state {
        case .Connected, .StreamsRunning:
            if let historyID { LocalCallHistory.shared.connected(historyID) }
            incoming = false; stopRinging(); status = "Gespräch verbunden"
        case .End, .Released, .Error:
            // End can precede final CallLog classification. Stop ringing immediately,
            // but only persist the final outcome at Released.
            callFailed = callFailed || state == .Error
            var info = call.errorInfo
            for _ in 0..<4 {
                guard let current = info else { break }
                logger.notice("terminal_reason protocol=\(current.proto ?? "-", privacy: .public) code=\(current.protocolCode, privacy: .public)")
                acceptedElsewhere = acceptedElsewhere || MacCallOutcome.isAnsweredElsewhere(
                    protocolName: current.proto, code: current.protocolCode)
                info = current.subErrorInfo
            }
            acceptedElsewhere = acceptedElsewhere || call.callLog?.status == .AcceptedElsewhere
            stopRinging(); active = false; incoming = false
            logger.notice("ring_stopped id=\(self.historyID?.uuidString ?? "-", privacy: .public) wall=\(Date().timeIntervalSince1970, privacy: .public)")
            status = callFailed ? "Anruf fehlgeschlagen" : "Anruf beendet"
            if state == .Released {
                if let historyID {
                    LocalCallHistory.shared.end(historyID, reason: localEndReason ?? (acceptedElsewhere ? "answered_elsewhere" : (callFailed ? "failed" : nil)))
                }
                logger.notice("released elsewhere=\(self.acceptedElsewhere, privacy: .public)")
                historyID = nil; self.call = nil; receivedAt = nil
            }
        case .OutgoingRinging: status = "Gegenstelle klingelt …"
        default: break
        }
    }

    func ringViewAppeared() {
        guard incoming, let receivedAt else { return }
        logger.notice("ring_view_appeared wall=\(Date().timeIntervalSince1970, privacy: .public) callback_ms=\((ProcessInfo.processInfo.systemUptime - receivedAt) * 1000, privacy: .public)")
    }

    func dial(_ input: String) async {
        do {
            guard registered, let core else { throw MacError.message("Keine SIP-Registrierung.") }
            guard !active, call == nil else { throw MacError.message("Es läuft bereits ein Gespräch.") }
            guard let number = SIPDialNumber.normalized(input), number.count <= 64 else {
                throw MacError.message("Ungültige Rufnummer.")
            }
            guard await AVCaptureDevice.requestAccess(for: .audio) else {
                throw MacError.message("Mikrofonzugriff in den Systemeinstellungen erlauben.")
            }
            // Recheck after the asynchronous permission sheet.
            guard registered, !active, call == nil else { throw MacError.message("SIP ist gerade nicht verfügbar.") }
            let address = try Factory.Instance.createAddress(addr: "sip:\(number)@\(host)")
            let params = try core.createCallParams(call: nil)
            params.videoEnabled = false
            guard let call = core.inviteAddressWithParams(addr: address, params: params) else {
                throw MacError.message("Anruf konnte nicht gestartet werden.")
            }
            self.call = call; acceptedElsewhere = false; callFailed = false; localEndReason = nil
            active = true; incoming = false; caller = input
            let id = UUID(); historyID = id
            LocalCallHistory.shared.begin(id: id, number: input, incoming: false, line: 3)
            status = "Festnetz: Verbindungsaufbau …"
        } catch { self.error = error.localizedDescription }
    }

    func answer() async {
        do {
            guard await AVCaptureDevice.requestAccess(for: .audio) else {
                throw MacError.message("Mikrofonzugriff fehlt.")
            }
            guard incoming, let call else { return }
            let params = try core?.createCallParams(call: call)
            params?.videoEnabled = false
            try call.acceptWithParams(params: params)
            incoming = false; stopRinging()
        } catch { self.error = error.localizedDescription }
    }

    func hangup() {
        do {
            // SDK callbacks may run synchronously inside decline/terminate.
            localEndReason = incoming ? "declined" : "cancelled"
            if incoming { try call?.decline(reason: .Busy) }
            else { try call?.terminate() }
            if let historyID { LocalCallHistory.shared.end(historyID, reason: localEndReason) }
            stopRinging(); historyID = nil; call = nil; active = false; incoming = false
        } catch { self.error = error.localizedDescription }
    }

    func dtmf(_ digit: String) {
        guard let byte = digit.utf8.first, digit.utf8.count == 1, "0123456789*#".contains(digit) else { return }
        do { try call?.sendDtmf(dtmf: CChar(bitPattern: byte)) }
        catch { self.error = error.localizedDescription }
    }

    private func notify(id: UUID, caller: String) {
        notificationID = id
        let content = UNMutableNotificationContent()
        content.title = "Festnetzanruf"; content.body = caller
        content.categoryIdentifier = "incoming"
        let request = UNNotificationRequest(identifier: id.uuidString, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request) { [weak self] error in
            Task { @MainActor in
                if self?.historyID != id || self?.incoming != true {
                    let center = UNUserNotificationCenter.current()
                    center.removePendingNotificationRequests(withIdentifiers: [id.uuidString])
                    center.removeDeliveredNotifications(withIdentifiers: [id.uuidString])
                } else if let error { self?.error = "Benachrichtigung: \(error.localizedDescription)" }
            }
        }
    }

    private func stopRinging() {
        ringTimer?.invalidate(); ringTimer = nil
        if let notificationID {
            let center = UNUserNotificationCenter.current()
            center.removePendingNotificationRequests(withIdentifiers: [notificationID.uuidString])
            center.removeDeliveredNotifications(withIdentifiers: [notificationID.uuidString])
        }
        notificationID = nil
    }
}
