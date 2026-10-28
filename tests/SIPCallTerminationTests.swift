import Foundation

@main struct SIPCallTerminationTests {
    @MainActor static func main() {
        let suite = "SIPTerminationTests.\(UUID())"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let history = LocalCallHistory(defaults: defaults)
        let missed = MissedCallState(defaults: defaults)
        for (proto, code, answered) in [("Q.850", 26, true), ("q.850", 26, true), ("SIP", 200, true),
            ("SIP", 487, false), ("Q.850", 16, false), ("SIP", 486, false), ("", 0, false), ("unknown", 200, false)] {
            var termination = SIPCallTermination()
            termination.observe(protocolName: proto, code: code)
            assert(termination.answeredElsewhere == answered)
            let id = UUID()
            history.begin(id: id, number: "test", incoming: true, line: 3)
            history.end(id, reason: termination.historyReason)
            let record = history.entries.first { $0.id == id }!
            assert(record.status == (answered ? "answered_elsewhere" : "missed"))
            assert(record.isMissedIncomingCall == !answered)
            assert(missed.unreadCalls(in: [record]).isEmpty == answered)
        }
        var termination = SIPCallTermination()
        termination.observe(protocolName: "SIP", code: 487)
        // Evidence may arrive in chained ErrorInfo or a later final CallLog.
        termination.observe(acceptedElsewhere: true)
        assert(termination.answeredElsewhere)
        termination.failed = true
        assert(termination.historyReason == "answered_elsewhere")
        termination.localReason = "declined"
        assert(!termination.answeredElsewhere && termination.historyReason == "declined")
        termination.localReason = "cancelled"
        assert(termination.historyReason == "cancelled")
        termination.localReason = nil
        termination.connected = true
        assert(!termination.answeredElsewhere && termination.historyReason == nil)
        var outgoing = SIPCallTermination(incoming: false)
        outgoing.observe(protocolName: "SIP", code: 200)
        assert(!outgoing.answeredElsewhere && outgoing.historyReason == nil)
        var failed = SIPCallTermination()
        failed.failed = true
        assert(failed.historyReason == "failed")
        print("SIP termination, history and missed badge tests passed")
    }
}
