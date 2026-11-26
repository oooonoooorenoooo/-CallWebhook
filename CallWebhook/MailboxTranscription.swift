import Foundation
import CryptoKit

@MainActor
protocol VoicemailTranscribing: AnyObject {
    func transcribe(audio: Data) async throws -> String
}

enum MailboxTranscriptionState: Equatable {
    case queued
    case transcribing
    case ready(String)
    case noSpeech
    case failed(String)

    var preview: String {
        switch self {
        case .queued: return "Transkription wartet …"
        case .transcribing: return "Nachricht wird transkribiert …"
        case .ready(let text): return text
        case .noSpeech: return "Keine Sprache erkannt"
        case .failed: return "Transkription nicht verfügbar – Nachricht öffnen"
        }
    }
}

/// FRITZ!Box message indices can be reused after deletion. Include the recording
/// metadata and HA instance; archiving or marking a message read must not change
/// its transcription identity. Hashing also keeps phone numbers out of filenames.
enum MailboxTranscriptionIdentity {
    static func key(source: String, message: MailboxMessage) -> String {
        let fields = [source, message.tam, message.index, message.date,
                      message.duration, message.number, message.called]
        let data = (try? JSONEncoder().encode(fields)) ?? Data()
        return SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }
}

/// Only completed results are persisted. Audio stays in temporary storage and
/// failed/interrupted recognition is never cached as a successful transcript.
@MainActor
final class MailboxTranscriptCache {
    static let shared = MailboxTranscriptCache()

    private struct Entry: Codable {
        let text: String
        let created: Date
    }

    private let fileURL: URL
    private var entries: [String: Entry]
    private let limit = 300

    init(fileURL: URL? = nil) {
        self.fileURL = fileURL ?? FileManager.default.urls(for: .applicationSupportDirectory,
            in: .userDomainMask)[0].appendingPathComponent("CallWebhook/VoicemailTranscripts-v1.json")
        if let data = try? Data(contentsOf: self.fileURL), data.count <= 8_000_000,
           let stored = try? JSONDecoder().decode([String: Entry].self, from: data) {
            entries = stored
        } else {
            entries = [:]
        }
    }

    func text(for key: String) -> String? { entries[key]?.text }

    func save(_ text: String, for key: String) {
        entries[key] = Entry(text: text, created: Date())
        if entries.count > limit {
            for item in entries.sorted(by: { $0.value.created > $1.value.created }).dropFirst(limit) {
                entries.removeValue(forKey: item.key)
            }
        }
        persist()
    }

    func remove(_ key: String) {
        guard entries.removeValue(forKey: key) != nil else { return }
        persist()
    }

    private func persist() {
        do {
            let directory = fileURL.deletingLastPathComponent()
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            let data = try JSONEncoder().encode(entries)
            #if os(iOS)
            try data.write(to: fileURL, options: [.atomic, .completeFileProtection])
            #else
            try data.write(to: fileURL, options: .atomic)
            #endif
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: fileURL.path)
            var url = fileURL
            var values = URLResourceValues()
            values.isExcludedFromBackup = true
            try url.setResourceValues(values)
        } catch {
            // A full/locked disk must not hide a result already available in
            // memory. It can be regenerated from the original recording later.
        }
    }
}
