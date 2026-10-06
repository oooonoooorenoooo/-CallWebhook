import Foundation

@main
struct MissedCallStateTests {
    @MainActor static func main() {
        let suite = "MissedCallStateTests.\(UUID())"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let history = LocalCallHistory(defaults: defaults)
        let state = MissedCallState(defaults: defaults)
        let date = Date(timeIntervalSince1970: 10_000)

        let missedID = UUID()
        history.begin(id: missedID, number: "+49 30 12345", incoming: true, line: 3, at: date)
        state.markSeen(history.entries) // opening the list while it still rings
        assert(state.unreadCalls(in: history.entries).isEmpty)
        history.end(missedID, at: date.addingTimeInterval(20))
        assert(state.unreadCalls(in: history.entries).count == 1)
        assert(MissedCallState(defaults: defaults).unreadCalls(in: history.entries).count == 1)
        history.end(missedID, at: date.addingTimeInterval(21)) // duplicate hang-up
        assert(state.unreadCalls(in: history.entries).count == 1)

        let own = history.entries[0]
        var duplicate = CallRecord(id: UUID(), date: date.addingTimeInterval(1),
            number: "+493012345", direction: "incoming", status: "missed",
            endedAt: date.addingTimeInterval(20), reportedDuration: 0)
        let merged = LocalCallHistory.merged(local: history.entries, system: [duplicate])
        assert(state.unreadCalls(in: merged).count == 1)
        state.markSeen(merged)
        let reopened = MissedCallState(defaults: defaults)
        assert(reopened.unreadCalls(in: [own, duplicate]).isEmpty)
        duplicate.line = 1 // a different line is a different call
        assert(reopened.unreadCalls(in: [duplicate]).count == 1)

        let later = UUID()
        history.begin(id: later, number: "+493012345", incoming: true, line: 3,
                      at: date.addingTimeInterval(60))
        history.end(later, at: date.addingTimeInterval(80))
        assert(reopened.unreadCalls(in: history.entries).count == 1)

        for (incoming, reason) in [(false, "cancelled"), (false, "failed"), (true, "declined")] {
            let id = UUID()
            history.begin(id: id, number: "456", incoming: incoming, at: date.addingTimeInterval(120))
            history.end(id, reason: reason)
        }
        let answered = UUID()
        history.begin(id: answered, number: "789", incoming: true, at: date.addingTimeInterval(180))
        history.connected(answered, at: date.addingTimeInterval(181))
        history.end(answered, at: date.addingTimeInterval(190))
        assert(reopened.unreadCalls(in: history.entries).count == 1)

        let laterCall = history.entries.first { $0.id == later }!
        defaults.set(later.uuidString, forKey: "hiddenCallIDs")
        assert(reopened.unreadCalls(in: history.entries).isEmpty)
        defaults.removeObject(forKey: "hiddenCallIDs")
        defaults.set(try! JSONEncoder().encode([laterCall]), forKey: "hiddenCallRecords")
        var systemCopy = CallRecord(id: UUID(), date: laterCall.date, number: laterCall.number,
            direction: "incoming", status: "missed", endedAt: laterCall.endedAt)
        assert(reopened.unreadCalls(in: [systemCopy]).isEmpty)
        defaults.removeObject(forKey: "hiddenCallRecords")
        let archive = CallHistoryArchive(defaults: defaults)
        archive.save(laterCall)
        assert(reopened.unreadCalls(in: [systemCopy]).isEmpty)
        archive.remove([laterCall])

        // A connected system call must not be counted even with an inconsistent status.
        systemCopy.reportedDuration = 10
        assert(reopened.unreadCalls(in: [systemCopy]).isEmpty)

        let failed = UUID()
        history.begin(id: failed, number: "failed", incoming: true, at: date.addingTimeInterval(240))
        history.end(failed, reason: "failed")
        assert(reopened.unreadCalls(in: history.entries).count == 2)
        let interrupted = UUID()
        history.begin(id: interrupted, number: "interrupted", incoming: true, at: date.addingTimeInterval(300))
        let recovered = LocalCallHistory(defaults: defaults)
        assert(reopened.unreadCalls(in: recovered.entries).count == 3)
        reopened.markSeen(recovered.entries)
        assert(MissedCallState(defaults: defaults).unreadCalls(in: recovered.entries).isEmpty)
        print("Missed-call counting, acknowledgement, deletion, duplicate and restart tests passed")
    }
}
