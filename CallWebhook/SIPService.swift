import Foundation
import linphonesw

@MainActor
final class SIPService: ObservableObject {
    static let shared = SIPService()

    @Published private(set) var status = "SIP nicht verbunden"
    @Published private(set) var active = false

    private var core: Core?
    private var iterateTimer: Timer?

    private init() {}

    func configureAndStart() throws {
        let defaults = UserDefaults.standard
        let host = defaults.string(forKey: "sipHost")?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let username = defaults.string(forKey: "sipUsername")?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let password = defaults.string(forKey: "sipPassword") ?? ""

        guard !host.isEmpty, !username.isEmpty, !password.isEmpty else {
            status = "SIP-Zugangsdaten fehlen"
            throw SIPError.notConfigured
        }

        iterateTimer?.invalidate()
        core?.stop()
        core = nil

        let newCore = try Factory.Instance.createCore(configPath: "", factoryConfigPath: "", systemContext: nil)
        let auth = try Factory.Instance.createAuthInfo(
            username: username,
            userid: nil,
            passwd: password,
            ha1: nil,
            realm: nil,
            domain: host
        )
        newCore.addAuthInfo(info: auth)

        let params = try newCore.createAccountParams()
        let identity = try Factory.Instance.createAddress(addr: "sip:\(username)@\(host)")
        try params.setIdentityaddress(newValue: identity)
        params.registerEnabled = true

        let server = try Factory.Instance.createAddress(addr: "sip:\(host);transport=udp")
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
                self?.status = "SIP: \(String(describing: state))"
            }
        }
    }

    func call(_ number: String) throws {
        if core == nil {
            try configureAndStart()
        }
        guard let core else { throw SIPError.notConfigured }

        let host = UserDefaults.standard.string(forKey: "sipHost")?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard !host.isEmpty else { throw SIPError.notConfigured }

        let target = try Factory.Instance.createAddress(addr: "sip:\(number)@\(host)")
        core.configureAudioSession()
        _ = core.inviteAddress(addr: target)
        active = true
        status = "SIP-Anruf an \(number)"
    }

    func hangup() {
        guard let core else { return }
        core.terminateAllCalls()
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
