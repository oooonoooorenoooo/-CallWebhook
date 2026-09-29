import Foundation

@main
struct PushSetupDiagnosticsTests {
    static func main() {
        var state = PushSetupVerification()
        precondition(state.message(configured: true, routeReady: true, serviceAvailable: true).contains("noch nicht geprüft"))
        state.succeed()
        precondition(state.verified)
        let tls = NSError(domain: NSURLErrorDomain, code: -1200)
        let diagnostic = PushSetupDiagnostics.message(tls, stage: "Apple App Attest")
        precondition(diagnostic.contains("Apple App Attest: TLS"))
        precondition(diagnostic.contains("-1200"))
        state.fail(diagnostic)
        // Persisted HA configuration and route must not overwrite a live error.
        precondition(!state.verified)
        precondition(state.message(configured: true, routeReady: true, serviceAvailable: true) == diagnostic)
        state.begin()
        precondition(state.error == diagnostic)
        state.succeed()
        precondition(state.error == nil)
        precondition(state.message(configured: true, routeReady: true, serviceAvailable: true).contains("Zustellung mit einem Anruf"))
        let nested = NSError(domain: "com.apple.devicecheck", code: 4, userInfo: [NSUnderlyingErrorKey: tls])
        precondition(PushSetupDiagnostics.message(nested, stage: "Apple").contains("TLS-Verbindung"))
        print("Push verification cannot overwrite errors with persisted configuration")
    }
}
