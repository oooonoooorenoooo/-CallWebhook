import Foundation
import linphonesw

@MainActor
final class SIPService: ObservableObject {
    static let shared = SIPService()

    @Published private(set) var status = "SIP nicht verbunden"
    @Published private(set) var active = false
    @Published private(set) var registered = false

    @Published private(set) var callStatus = ""
    @Published private(set) var incoming = false
    private var historyID: UUID?
    private var trackedCall: Call?
    private var core: Core?
    private var iterateTimer: Timer?

    private init() {}

    func wakeForIncomingCall() throws {
        guard UserDefaults.standard.bool(forKey: "sipEnabled") else { throw SIPError.notConfigured }
        if core == nil { try configureAndStart() }
        else if !active {
            core?.callkitEnabled = true
            registered = false
            core?.refreshRegisters()
        }
    }

    func ensureStarted() throws {
        if core == nil { try configureAndStart() }
    }

    func configureAndStart(host: String? = nil, username: String? = nil, password: String? = nil) throws {
        let defaults = UserDefaults.standard
        let resolvedHost = (host ?? defaults.string(forKey: "sipHost") ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let resolvedUsername = (username ?? defaults.string(forKey: "sipUsername") ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let resolvedPassword = password
            ?? SetupKeychain.get(account: "asterisk-sip-callwebhook-ios")
            ?? defaults.string(forKey: "sipPassword")
            ?? ""

        guard !resolvedHost.isEmpty, !resolvedUsername.isEmpty, !resolvedPassword.isEmpty else {
            status = "SIP-Zugangsdaten fehlen"
            throw SIPError.notConfigured
        }

        // Only persist complete credentials. Never let an empty SecureField overwrite
        // the password that a previous successful registration stored.
        defaults.set(resolvedHost, forKey: "sipHost")
        defaults.set(resolvedUsername, forKey: "sipUsername")
        try SetupKeychain.set(resolvedPassword, account: "asterisk-sip-callwebhook-ios")
        defaults.removeObject(forKey: "sipPassword")

        iterateTimer?.invalidate()
        core?.stop()
        CallProximityController.shared.setCallInProgress(false)
        core = nil
        registered = false

        let newCore = try Factory.Instance.createCore(configPath: "", factoryConfigPath: "", systemContext: nil)
        // Incoming calls use CallKit: let iOS activate the audio session before
        // Linphone starts capture/playback, especially when waking while locked.
        newCore.callkitEnabled = true
        let auth = try Factory.Instance.createAuthInfo(
            username: resolvedUsername,
            userid: nil,
            passwd: resolvedPassword,
            ha1: nil,
            realm: nil,
            domain: resolvedHost
        )
        newCore.addAuthInfo(info: auth)

        let params = try newCore.createAccountParams()
        let identity = try Factory.Instance.createAddress(addr: "sip:\(resolvedUsername)@\(resolvedHost)")
        try params.setIdentityaddress(newValue: identity)
        params.registerEnabled = true

        // The identity supplies the REGISTER To user; the registrar is the server.
        let server = try Factory.Instance.createAddress(addr: "sip:\(resolvedHost);transport=udp")
        try params.setServeraddress(newValue: server)

        let account = try newCore.createAccount(params: params)
        try newCore.addAccount(account: account)
        newCore.defaultAccount = account
        try newCore.start()

        core = newCore
        status = "SIP wird registriert …"

        let timer = Timer(timeInterval: 0.02, repeats: true) { [weak self] _ in
            self?.core?.iterate()
            if let state = self?.core?.defaultAccount?.state {
                let stateText = String(describing: state)
                self?.status = "SIP: \(stateText)"
                self?.registered = stateText.lowercased().contains("ok")
            }
            self?.updateCallState()
        }
        RunLoop.main.add(timer, forMode: .common)
        iterateTimer = timer
    }

    private func updateCallState() {
        defer { updateProximityMonitoring() }
        if trackedCall == nil, let call = core?.currentCall,
           String(describing: call.state).lowercased().contains("incoming") {
            let caller = call.remoteAddress?.username ?? "Unbekannt"
            let id = call.remoteParams?.getCustomHeader(headerName: "X-CallWebhook-ID")
            let lineHeader = call.remoteParams?.getCustomHeader(headerName: "X-CallWebhook-Line")
            let line = lineHeader.flatMap(Int.init)
            guard IncomingCallProvider.shared.attachSIP(caller: caller, id: id.flatMap(UUID.init(uuidString:)), line: line) else {
                try? call.terminate()
                return
            }
            trackedCall = call
            core?.callkitEnabled = true
            historyID = IncomingCallProvider.shared.currentCallID
            active = true
            incoming = true
            callStatus = "Eingehender Anruf: \(caller)"
        }
        guard let call = trackedCall else { return }
        let state = String(describing: call.state).lowercased()
        if state.contains("error") {
            finishHistory(reason: "failed")
            callStatus = "Anruf fehlgeschlagen: \(call.errorInfo?.phrase ?? "SIP-Verbindung abgelehnt")"
            active = false
            trackedCall = nil
            incoming = false
            IncomingCallProvider.shared.ended(failed: state.contains("error"))
        } else if state == "end" || state.contains("released") {
            finishHistory()
            callStatus = "Anruf beendet"
            active = false
            trackedCall = nil
            incoming = false
            IncomingCallProvider.shared.ended(failed: state.contains("error"))
        } else if state.contains("streamsrunning") || state == "connected" {
            if let historyID { LocalCallHistory.shared.connected(historyID) }
            incoming = false
            callStatus = "Gespräch verbunden"
        } else if state.contains("incoming") {
            return
        } else if state.contains("ringing") {
            callStatus = "Gegenstelle klingelt …"
        } else {
            callStatus = "Verbindungsaufbau: \(call.state)"
        }
    }

    func call(_ number: String, line: Int = 1) throws {
        // Defense in depth for future callers bypassing DialerModel. This check
        // MUST precede configuration, registration and any line-prefix addition.
        if let cellularNumber = CellularRouting.cellularOnlyNumber(number) {
            status = "\(cellularNumber) wird über iOS-Mobilfunk gewählt …"
            SystemCellularDialer.call(cellularNumber) { opened in
                Task { @MainActor in
                    self.status = opened ? "An iOS-Mobilfunk übergeben" : "iPhone-Notruffunktion verwenden – Mobilfunkübergabe fehlgeschlagen"
                }
            }
            return
        }
        if core == nil {
            try configureAndStart()
        }
        guard let core else { throw SIPError.notConfigured }

        let host = UserDefaults.standard.string(forKey: "sipHost")?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard !host.isEmpty else {
            status = "SIP-Host fehlt"
            throw SIPError.notConfigured
        }

        let defaults = UserDefaults.standard
        let prefix: String
        switch line {
        case 2:
            prefix = defaults.string(forKey: "sipLine2Prefix") ?? ""
        case 3:
            prefix = defaults.string(forKey: "sipLine3Prefix") ?? ""
        default:
            prefix = ""
        }
        guard !IncomingRouteRepair.shared.running else { throw SIPError.provisioning }
        guard registered else { throw SIPError.notRegistered }
        guard !active, !IncomingCallProvider.shared.hasCall else { throw SIPError.alreadyActive }
        guard let targetNumber = SIPDialNumber.target(number, prefix: prefix) else { throw SIPError.invalidNumber }
        let target = try Factory.Instance.createAddress(addr: "sip:\(targetNumber)@\(host)")
        // Outgoing calls currently use DialerModel's explicitly activated session,
        // not a CXStartCallAction. Do not wait for a CallKit callback on this path.
        core.callkitEnabled = false
        core.configureAudioSession()
        let callID = UUID()
        LocalCallHistory.shared.begin(id: callID, number: number, incoming: false, line: line)
        guard let call = core.inviteAddress(addr: target) else {
            LocalCallHistory.shared.end(callID, reason: "failed")
            callStatus = "SIP konnte den Anruf nicht starten"
            throw SIPError.inviteFailed
        }
        trackedCall = call
        historyID = callID
        callStatus = "Leitung \(line): \(number) – Verbindung wird aufgebaut …"
        active = true
        status = "SIP Leitung \(line): \(number)"
        updateProximityMonitoring()
    }

    func sendDTMF(_ digit: String) throws {
        guard !CallProximityController.shared.isBlockingTouches else { return }
        guard active, let call = trackedCall else { return }
        let state = String(describing: call.state).lowercased()
        guard state == "connected" || state.contains("streamsrunning") else { return }
        guard digit.utf8.count == 1, let byte = digit.utf8.first,
              "0123456789*#".contains(digit) else { throw SIPError.invalidNumber }
        try call.sendDtmf(dtmf: CChar(bitPattern: byte))
    }

    func answerIncoming() throws {
        guard incoming, let call = trackedCall else { throw SIPError.notConfigured }
        core?.callkitEnabled = true
        core?.configureAudioSession()
        try call.accept()
        incoming = false
        callStatus = "Anruf wird angenommen …"
        updateProximityMonitoring()
    }

    func activateCallAudio(_ enabled: Bool) {
        core?.activateAudioSession(activated: enabled)
    }

    func incomingPresentationFailed(_ error: Error) {
        hangup()
        callStatus = "Anruf konnte nicht angezeigt/angenommen werden: \(error.localizedDescription)"
    }

    private func finishHistory(reason: String? = nil) {
        if let historyID { LocalCallHistory.shared.end(historyID, reason: reason) }
        historyID = nil
    }

    private func updateProximityMonitoring() {
        CallProximityController.shared.setCallInProgress(active && !incoming && trackedCall != nil)
    }

    func hangup() {
        defer { updateProximityMonitoring() }
        guard let core else { IncomingCallProvider.shared.ended(); return }
        do {
            try core.terminateAllCalls()
        } catch {
            status = "SIP-Auflegen fehlgeschlagen: \(error.localizedDescription)"
            return
        }
        finishHistory(reason: incoming ? "declined" : "cancelled")
        active = false
        incoming = false
        callStatus = "Anruf beendet"
        trackedCall = nil
        IncomingCallProvider.shared.ended()
        status = "SIP-Anruf beendet"
    }

    enum SIPError: LocalizedError {
        case notConfigured, notRegistered, invalidNumber, inviteFailed, alreadyActive, provisioning

        var errorDescription: String? {
            switch self {
            case .notConfigured: return "SIP ist noch nicht vollständig konfiguriert."
            case .notRegistered: return "Keine SIP-Registrierung bei Asterisk. Verbindung zum Heimnetz/VPN und SIP-Zugang prüfen."
            case .invalidNumber: return "Die Rufnummer enthält ungültige Zeichen."
            case .inviteFailed: return "Asterisk/SIP konnte keinen Anruf starten."
            case .provisioning: return "Asterisk wird gerade aktualisiert. Bitte warten."
            case .alreadyActive: return "Es läuft bereits ein SIP-Anruf."
            }
        }
    }
}
