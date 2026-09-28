import Foundation
import linphonesw

@MainActor
final class SIPService: ObservableObject {
    static let shared = SIPService()

    @Published private(set) var status = "SIP nicht verbunden"
    @Published private(set) var active = false
    @Published private(set) var registered = false

    private var core: Core?
    private var iterateTimer: Timer?

    private init() {}

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
        core = nil
        registered = false

        let newCore = try Factory.Instance.createCore(configPath: "", factoryConfigPath: "", systemContext: nil)
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

        iterateTimer = Timer.scheduledTimer(withTimeInterval: 0.02, repeats: true) { [weak self] _ in
            self?.core?.iterate()
            if let state = self?.core?.defaultAccount?.state {
                let stateText = String(describing: state)
                self?.status = "SIP: \(stateText)"
                self?.registered = stateText.lowercased().contains("ok")
            }
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
        let targetNumber = prefix + number
        let target = try Factory.Instance.createAddress(addr: "sip:\(targetNumber)@\(host)")
        core.configureAudioSession()
        _ = core.inviteAddress(addr: target)
        active = true
        status = "SIP Leitung \(line): \(number)"
    }

    func hangup() {
        guard let core else { return }
        do {
            try core.terminateAllCalls()
        } catch {
            status = "SIP-Auflegen fehlgeschlagen: \(error.localizedDescription)"
            return
        }
        active = false
        status = "SIP-Anruf beendet"
    }

    enum SIPError: LocalizedError {
        case notConfigured

        var errorDescription: String? {
            "SIP ist noch nicht vollständig konfiguriert."
        }
    }
}
