import Foundation

/// A persisted configuration is not proof that the current enrollment succeeded.
struct PushSetupVerification {
    private(set) var verified = false
    private(set) var error: String?

    mutating func begin() { verified = false }
    mutating func succeed() { verified = true; error = nil }
    mutating func fail(_ message: String) { verified = false; error = message }

    func message(configured: Bool, routeReady: Bool, serviceAvailable: Bool) -> String {
        if let error { return error }
        if configured && verified {
            return routeReady
                ? "Push-Anmeldung und Asterisk-Anrufstrecke geprüft. Zustellung mit einem Anruf bei gesperrtem iPhone testen."
                : "Push-Anmeldung geprüft; Asterisk-Anrufstrecke noch einrichten."
        }
        if configured { return "Push-Konfiguration vorhanden; aktuelle Anmeldung noch nicht geprüft." }
        return serviceAvailable ? "Automatische Anmeldung beim Push-Dienst noch nicht abgeschlossen."
            : "Der gemeinsame Push-Dienst wurde vom App-Anbieter noch nicht bereitgestellt."
    }
}

enum PushSetupDiagnostics {
    static func message(_ error: Error, stage: String) -> String {
        let ns = error as NSError
        var underlying = ns
        for _ in 0..<5 {
            guard let next = underlying.userInfo[NSUnderlyingErrorKey] as? NSError else { break }
            underlying = next
        }
        let tlsCodes = [-1200, -1201, -1202, -1203, -1204, -1205, -1206]
        if (ns.domain == NSURLErrorDomain && tlsCodes.contains(ns.code)) ||
            (underlying.domain == NSURLErrorDomain && tlsCodes.contains(underlying.code)) {
            let code = ns.domain == NSURLErrorDomain ? ns.code : underlying.code
            return "\(stage): TLS-Verbindung fehlgeschlagen (\(code)). Sichere Verbindung konnte nicht bestätigt werden. Erneut prüfen."
        }
        return "\(stage): \(ns.localizedDescription) (\(ns.domain) \(ns.code))"
    }
}
