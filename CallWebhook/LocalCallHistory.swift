import Foundation
import Combine

struct CallRecord: Identifiable, Codable, Equatable {
    struct NumberHandle { let value: String }
    let id: UUID
    let date: Date
    let number: String
    let direction: String
    var status: String
    var connectedAt: Date?
    var endedAt: Date?
    var reportedDuration: TimeInterval?
    var handles: [NumberHandle] { number.isEmpty ? [] : [NumberHandle(value: number)] }
    var duration: TimeInterval {
        if let reportedDuration { return reportedDuration }
        guard let connectedAt, let endedAt else { return 0 }
        return max(0, endedAt.timeIntervalSince(connectedAt))
    }
}

@MainActor
final class LocalCallHistory: ObservableObject {
    static let shared = LocalCallHistory()
    @Published private(set) var entries: [CallRecord]
    private let defaults: UserDefaults
    private let key = "callwebhook.callHistory.v1"

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        entries = defaults.data(forKey: key).flatMap { try? JSONDecoder().decode([CallRecord].self, from: $0) } ?? []
        // An interrupted process cannot supply a reliable hang-up time.
        for index in entries.indices where entries[index].endedAt == nil {
            entries[index].status = "interrupted"
            entries[index].endedAt = entries[index].connectedAt ?? entries[index].date
        }
        persist()
    }

    func begin(id: UUID, number: String, incoming: Bool, at date: Date = Date()) {
        guard !entries.contains(where: { $0.id == id }) else { return }
        entries.insert(CallRecord(id: id, date: date, number: number, direction: incoming ? "incoming" : "outgoing", status: "ringing"), at: 0)
        entries = Array(entries.prefix(1000))
        persist()
    }

    func connected(_ id: UUID, at date: Date = Date()) {
        guard let index = entries.firstIndex(where: { $0.id == id }), entries[index].endedAt == nil,
              entries[index].connectedAt == nil else { return }
        entries[index].connectedAt = date
        entries[index].status = "connected"
        persist()
    }

    func end(_ id: UUID, reason: String? = nil, at date: Date = Date()) {
        guard let index = entries.firstIndex(where: { $0.id == id }), entries[index].endedAt == nil else { return }
        entries[index].endedAt = date
        entries[index].status = entries[index].connectedAt != nil ? "connected" :
            (reason ?? (entries[index].direction == "incoming" ? "missed" : "cancelled"))
        persist()
    }

    static func merged(local: [CallRecord], system: [CallRecord]) -> [CallRecord] {
        let ids = Set(local.map(\.id))
        // CallKit can also expose our incoming calls in iOS history. Prefer the
        // local record, whose lifecycle we know, without duplicating that call.
        let remaining = system.filter { item in
            !ids.contains(item.id) && !local.contains { own in
                own.direction == item.direction && !own.number.isEmpty &&
                (own.number == item.number ||
                 (!own.number.filter { $0.isNumber }.isEmpty &&
                  own.number.filter { $0.isNumber } == item.number.filter { $0.isNumber })) &&
                abs(own.date.timeIntervalSince(item.date)) < 2
            }
        }
        return (local + remaining).sorted { $0.date > $1.date }
    }

    static func isHidden(_ call: CallRecord, records: [CallRecord]) -> Bool {
        records.contains { record in
            if record.id == call.id { return true }
            let number = record.number.filter { $0.isNumber }
            let sameNumber = record.number == call.number ||
                (!number.isEmpty && number == call.number.filter { $0.isNumber })
            return !record.number.isEmpty && sameNumber && record.direction == call.direction &&
                abs(record.date.timeIntervalSince(call.date)) < 2
        }
    }

    private func persist() {
        if let data = try? JSONEncoder().encode(entries) { defaults.set(data, forKey: key) }
    }
}
