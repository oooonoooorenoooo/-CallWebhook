import Foundation

/// Preserve terminal evidence across End -> Released. A bare CANCEL is not an answer.
struct SIPCallTermination {
    var incoming = true
    var connected = false
    var failed = false
    var localReason: String?
    private var remoteAnswer = false

    // The private stored evidence makes Swift's synthesized memberwise initializer
    // private as well. Both SIPService and regression tests need this public-to-module
    // entry point to distinguish the outgoing caller leg from an incoming call.
    init(incoming: Bool = true) {
        self.incoming = incoming
    }

    mutating func observe(protocolName: String?, code: Int) {
        if (protocolName?.uppercased() == "Q.850" && code == 26) ||
           (protocolName?.uppercased() == "SIP" && code == 200) {
            remoteAnswer = true
        }
    }

    mutating func observe(acceptedElsewhere: Bool) {
        remoteAnswer = remoteAnswer || acceptedElsewhere
    }

    var answeredElsewhere: Bool { incoming && remoteAnswer && !connected && localReason == nil }
    var historyReason: String? {
        if let localReason { return localReason }
        if connected { return nil }
        if answeredElsewhere { return "answered_elsewhere" }
        return failed ? "failed" : nil
    }
}
