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
        history.begin(id: outgoing, number: "030 12345", incoming: false, line: 3, at: date)
        history.begin(id: outgoing, number: "duplicate", incoming: false, at: date)
        assert(history.entries.count == 1)
        assert(history.entries[0].lineLabel == "Festnetz")
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
        let incoming = UUID()
        history.begin(id: incoming, number: "123", incoming: true, at: date)
        history.end(incoming, at: date.addingTimeInterval(2))
        history.setLine(incoming, line: 2) // metadata may arrive after a declined push
        history.setLine(incoming, line: 0)
        history.setLine(incoming, line: 1) // do not overwrite an established assignment
        assert(history.entries[0].lineLabel == "SIM 2")
        assert(history.entries[0].status == "missed")
        assert(LocalCallHistory(defaults: defaults).entries[0].line == 2)
        var otherLine = duplicate
        otherLine.line = 1
        assert(!LocalCallHistory.isHidden(otherLine, records: [own]))
        assert(LocalCallHistory.merged(local: [own], system: [otherLine]).count == 2)
        let legacyData = try! JSONEncoder().encode([own])
        var legacyJSON = try! JSONSerialization.jsonObject(with: legacyData) as! [[String: Any]]
        legacyJSON[0].removeValue(forKey: "line")
        let legacy = try! JSONDecoder().decode([CallRecord].self, from: JSONSerialization.data(withJSONObject: legacyJSON))
        assert(legacy.count == 1 && legacy[0].line == nil && legacy[0].lineLabel == "Leitung unbekannt")
        assert(otherLine.lineLabel == "SIM 1")
        let archive = CallHistoryArchive(defaults: defaults)
        archive.save(own)
        assert(archive.entries.count == 1 && archive.contains(duplicate))
        assert(!archive.contains(later) && !archive.contains(otherLine))
        var refreshed = duplicate
        refreshed.endedAt = own.endedAt
        refreshed.reportedDuration = own.duration
        refreshed.line = own.line
        archive.save(refreshed)
        assert(archive.entries.count == 1) // same call with a new system UUID
        assert(archive.entries[0].duration == 60)
        let reopenedArchive = CallHistoryArchive(defaults: defaults)
        assert(reopenedArchive.entries == archive.entries)
        defaults.removeObject(forKey: "callwebhook.callHistory.v1")
        assert(CallHistoryArchive(defaults: defaults).entries.count == 1)
        reopenedArchive.remove([own])
        assert(CallHistoryArchive(defaults: defaults).entries.isEmpty)
        archive.save(later) // unfinished calls must not be frozen in the archive
        assert(archive.entries.count == 1)
        print("Call history lifecycle, persistence, recovery and merge tests passed")
    }
}
