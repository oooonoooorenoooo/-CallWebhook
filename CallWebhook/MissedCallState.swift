import Foundation

extension CallRecord {
    var isMissedIncomingCall: Bool {
        direction.lowercased().contains("incoming") && endedAt != nil &&
            connectedAt == nil && duration == 0 &&
            ["missed", "failed", "interrupted"].contains(status.lowercased())
    }
}

/// Store acknowledgements by call identity, including the number/date fallback
/// used by the history. iOS may return a different UUID for the same SIP call.
@MainActor
final class MissedCallState {
    private let defaults: UserDefaults
    private let key = "callwebhook.seenMissedCalls.v1"
    private var seen: [CallRecord]

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        seen = defaults.data(forKey: key)
            .flatMap { try? JSONDecoder().decode([CallRecord].self, from: $0) } ?? []
    }

    func unreadCalls(in calls: [CallRecord]) -> [CallRecord] {
        let hiddenIDs = Set((defaults.string(forKey: "hiddenCallIDs") ?? "")
            .split(separator: "\n").map(String.init))
        let hidden = records(forKey: "hiddenCallRecords")
        let archived = records(forKey: "callwebhook.callArchive.v1")
        return calls.filter {
            $0.isMissedIncomingCall && !hiddenIDs.contains($0.id.uuidString) &&
                !LocalCallHistory.isHidden($0, records: seen) &&
                !LocalCallHistory.isHidden($0, records: hidden) &&
                !LocalCallHistory.isHidden($0, records: archived)
        }
    }

    func markSeen(_ calls: [CallRecord]) {
        let unread = unreadCalls(in: calls)
        guard !unread.isEmpty else { return }
        seen = LocalCallHistory.merged(local: unread, system: seen)
        if let data = try? JSONEncoder().encode(seen) { defaults.set(data, forKey: key) }
    }

    private func records(forKey key: String) -> [CallRecord] {
        defaults.data(forKey: key)
            .flatMap { try? JSONDecoder().decode([CallRecord].self, from: $0) } ?? []
    }
}
