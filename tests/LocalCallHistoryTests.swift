import Foundation

@main
struct LocalCallHistoryTests {
    @MainActor static func main() {
        let suite = "CallHistoryTests.\(UUID())"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let history = LocalCallHistory(defaults: defaults)
        let date = Date(timeIntervalSince1970: 1_000)
        let outgoing = UUID()
        history.begin(id: outgoing, number: "030 12345", incoming: false, at: date)
        history.begin(id: outgoing, number: "duplicate", incoming: false, at: date)
        assert(history.entries.count == 1)
        history.connected(outgoing, at: date.addingTimeInterval(5))
        history.connected(outgoing, at: date.addingTimeInterval(10))
        history.end(outgoing, at: date.addingTimeInterval(65))
        history.end(outgoing, reason: "failed", at: date.addingTimeInterval(100))
        assert(history.entries[0].duration == 60)
        assert(history.entries[0].status == "connected")
        for (reason, expected) in [(nil, "missed"), ("declined", "declined"), ("failed", "failed")] as [(String?, String)] {
            let id = UUID()
            history.begin(id: id, number: "123", incoming: true, at: date)
            history.end(id, reason: reason, at: date.addingTimeInterval(10))
            assert(history.entries[0].status == expected)
            assert(history.entries[0].duration == 0)
        }
        let reopened = LocalCallHistory(defaults: defaults)
        assert(reopened.entries == history.entries)
        let interrupted = UUID()
        history.begin(id: interrupted, number: "456", incoming: false, at: date)
        let recovered = LocalCallHistory(defaults: defaults)
        assert(recovered.entries[0].status == "interrupted")
        assert(recovered.entries[0].endedAt != nil)
        let own = history.entries.first { $0.id == outgoing }!
        let duplicate = CallRecord(id: UUID(), date: date.addingTimeInterval(1), number: "03012345", direction: "outgoing", status: "connected")
        let later = CallRecord(id: UUID(), date: date.addingTimeInterval(60), number: "03012345", direction: "outgoing", status: "connected")
        assert(LocalCallHistory.isHidden(duplicate, records: [own]))
        assert(!LocalCallHistory.isHidden(later, records: [own]))
        assert(LocalCallHistory.isHidden(own, records: [own]))
        let merged = LocalCallHistory.merged(local: [own], system: [own, duplicate, later])
        assert(merged.count == 2 && merged.first?.id == later.id)
        assert(LocalCallHistory.merged(local: history.entries, system: []).count == history.entries.count)
        print("Call history lifecycle, persistence, recovery and merge tests passed")
    }
}
