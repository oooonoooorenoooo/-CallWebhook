import Foundation
import Combine

struct MailboxMessage: Codable, Identifiable {
    let index: String
    let tam: String
    let called: String
    let date: String
    let duration: String
    let name: String
    let number: String
    let isNew: Bool
    let audio: String
    let archived: Bool?

    var isArchived: Bool { archived == true }
    var id: String { "\(tam)-\(index)" }

    enum CodingKeys: String, CodingKey {
        case index, tam, called, date, duration, name, number, audio, archived
        case isNew = "new"
    }
}

@MainActor
final class MailboxModel: ObservableObject {
    @Published private(set) var messages: [MailboxMessage] = []
    @Published private(set) var isLoading = false
    @Published private(set) var errorMessage: String?
    @Published private(set) var transcriptionStates: [String: MailboxTranscriptionState] = [:]

    private let transcriber: (any VoicemailTranscribing)?
    private let transcriptCache: MailboxTranscriptCache
    private var source: URL?
    private var revision = 0
    private var transcriptionEnabled = false
    private var transcriptionTask: Task<Void, Never>?
    private var transcriptionTaskID: UUID?
    private var transcribingKey: String?

    init(transcriber: (any VoicemailTranscribing)? = nil,
         transcriptCache: MailboxTranscriptCache? = nil) {
        self.transcriber = transcriber
        self.transcriptCache = transcriptCache ?? .shared
    }

    private func perform(_ path: String, base: URL? = nil, method: String = "GET", timeout: TimeInterval = 15) async throws -> Data {
        guard let base = base ?? HomeAssistantConnection.configuredBase else {
            throw NSError(domain: "CallWebhook.Mailbox", code: 0, userInfo: [NSLocalizedDescriptionKey: "Home Assistant ist noch nicht eingerichtet"])
        }
        let (data, code) = try await HomeAssistantConnection.request(base: base, path: path, method: method, timeout: timeout)
        guard (200..<300).contains(code) else {
            throw NSError(domain: "CallWebhook.Mailbox", code: code, userInfo: [NSLocalizedDescriptionKey: "Home Assistant HTTP \(code)"])
        }
        return data
    }

    private func messagePath(_ message: MailboxMessage) throws -> String {
        guard !message.tam.isEmpty, !message.index.isEmpty,
              message.tam.allSatisfy({ $0.isASCII && $0.isNumber }),
              message.index.allSatisfy({ $0.isASCII && $0.isNumber }) else { throw URLError(.badURL) }
        return "\(message.tam)/\(message.index)"
    }

    func refresh() async {
        guard !isLoading else { return }
        let base = HomeAssistantConnection.configuredBase
        if source != base {
            revision += 1
            transcriptionTask?.cancel()
            source = base
            messages = []
            transcriptionStates = [:]
        }
        let expectedRevision = revision
        isLoading = true
        defer { isLoading = false }
        do {
            let data = try await perform("api/callwebhook/mailbox", base: base)
            try Task.checkCancellation()
            guard source == base, HomeAssistantConnection.configuredBase == base,
                  revision == expectedRevision else { return }
            let updated = try JSONDecoder().decode([MailboxMessage].self, from: data)
            messages = updated
            reconcileTranscriptions()
            errorMessage = nil
        } catch is CancellationError {
            return
        } catch {
            guard source == base, HomeAssistantConnection.configuredBase == base,
                  revision == expectedRevision, !Task.isCancelled else { return }
            errorMessage = error.localizedDescription
        }
    }

    /// Owned by the visible, active mailbox view; cancellation ends the loop.
    func refreshAutomatically() async {
        while !Task.isCancelled {
            await refresh()
            do { try await Task.sleep(for: .seconds(30)) }
            catch { return }
        }
    }

    func setTranscriptionActive(_ active: Bool) {
        transcriptionEnabled = active
        if active {
            startNextTranscription()
        } else {
            // Keep the job slot until the recognizer has finished cancellation,
            // so rapidly reopening the tab cannot run two recognizers at once.
            transcriptionTask?.cancel()
        }
    }

    func transcription(for message: MailboxMessage) -> MailboxTranscriptionState? {
        guard let source, source == HomeAssistantConnection.configuredBase else { return nil }
        return transcriptionStates[key(for: message, source: source)]
    }

    func retryTranscription(for message: MailboxMessage) {
        guard let source, source == HomeAssistantConnection.configuredBase,
              messages.contains(where: { self.key(for: $0, source: source) == self.key(for: message, source: source) }) else { return }
        let key = self.key(for: message, source: source)
        guard key != transcribingKey else { return }
        transcriptionStates[key] = .queued
        startNextTranscription()
    }

    func clearError() { errorMessage = nil }

    func setError(_ message: String) { errorMessage = message }

    func archive(_ message: MailboxMessage) async throws {
        let path = try messagePath(message)
        let base = try currentSource()
        try requireCurrent(message, source: base)
        _ = try await perform("api/callwebhook/mailbox/\(path)/archive", base: base, method: "POST")
        guard source == base, HomeAssistantConnection.configuredBase == base else { return }
        revision += 1
        await refresh()
    }

    func delete(_ message: MailboxMessage) async throws {
        let path = try messagePath(message)
        let base = try currentSource()
        try requireCurrent(message, source: base)
        let key = self.key(for: message, source: base)
        _ = try await perform("api/callwebhook/mailbox/\(path)", base: base, method: "DELETE")
        guard source == base, HomeAssistantConnection.configuredBase == base else { return }
        revision += 1
        messages.removeAll { self.key(for: $0, source: base) == key }
        transcriptionStates.removeValue(forKey: key)
        transcriptCache.remove(key)
        if transcribingKey == key { transcriptionTask?.cancel() }
        errorMessage = nil
    }

    func loadAudio(for message: MailboxMessage) async throws -> Data {
        let base = try currentSource()
        let key = self.key(for: message, source: base)
        guard let current = messages.first(where: { self.key(for: $0, source: base) == key }) else {
            throw NSError(domain: "CallWebhook.Mailbox", code: 404,
                userInfo: [NSLocalizedDescriptionKey: "Diese Nachricht ist nicht mehr in der Mailbox vorhanden."])
        }
        let data = try await loadAudio(for: current, base: base)
        try Task.checkCancellation()
        try requireCurrent(message, source: base)
        return data
    }

    private func requireCurrent(_ message: MailboxMessage, source: URL) throws {
        guard isCurrent(key(for: message, source: source), source: source) else {
            throw NSError(domain: "CallWebhook.Mailbox", code: 404,
                userInfo: [NSLocalizedDescriptionKey: "Diese Nachricht ist nicht mehr in der aktuellen Mailbox vorhanden."])
        }
    }

    private func currentSource() throws -> URL {
        guard let source, source == HomeAssistantConnection.configuredBase else {
            throw NSError(domain: "CallWebhook.Mailbox", code: 0,
                userInfo: [NSLocalizedDescriptionKey: "Die Verbindung hat sich geändert. Bitte die Mailbox erneut laden."])
        }
        return source
    }

    private func loadAudio(for message: MailboxMessage, base: URL) async throws -> Data {
        guard !message.audio.isEmpty else {
            throw NSError(domain: "CallWebhook.Mailbox", code: 404, userInfo: [NSLocalizedDescriptionKey: "Aufnahme nicht verfügbar"])
        }
        let path = try messagePath(message)
        // Fetch from the configured HA only; never send its bearer token to an
        // absolute host embedded in a mailbox response from an old installation.
        let prefix = message.isArchived ? "api/callwebhook/archive/audio" : "api/callwebhook/audio"
        return try await perform("\(prefix)/\(path)", base: base, timeout: 45)
    }

    private func key(for message: MailboxMessage, source: URL) -> String {
        MailboxTranscriptionIdentity.key(source: source.absoluteString, message: message)
    }

    private func reconcileTranscriptions() {
        guard let source else { return }
        let currentKeys = Set(messages.map { key(for: $0, source: source) })
        transcriptionStates = transcriptionStates.filter { currentKeys.contains($0.key) }
        if let transcribingKey, !currentKeys.contains(transcribingKey) {
            transcriptionTask?.cancel()
        }
        for message in messages {
            let key = self.key(for: message, source: source)
            guard transcriptionStates[key] == nil else { continue }
            if let cached = transcriptCache.text(for: key) {
                transcriptionStates[key] = cached.isEmpty ? .noSpeech : .ready(cached)
            } else if transcriber != nil {
                transcriptionStates[key] = .queued
            }
        }
        startNextTranscription()
    }

    private func isCurrent(_ key: String, source: URL) -> Bool {
        self.source == source && HomeAssistantConnection.configuredBase == source
            && messages.contains { self.key(for: $0, source: source) == key }
    }

    private func startNextTranscription() {
        guard transcriptionEnabled, transcriptionTask == nil,
              let transcriber, let source, source == HomeAssistantConnection.configuredBase,
              let message = messages.first(where: {
                  transcriptionStates[self.key(for: $0, source: source)] == .queued
              }) else { return }
        let key = self.key(for: message, source: source)
        let taskID = UUID()
        transcribingKey = key
        transcriptionTaskID = taskID
        transcriptionStates[key] = .transcribing
        transcriptionTask = Task { [weak self] in
            guard let self else { return }
            defer {
                if self.transcriptionTaskID == taskID {
                    self.transcriptionTask = nil
                    self.transcriptionTaskID = nil
                    self.transcribingKey = nil
                    self.startNextTranscription()
                }
            }
            do {
                let data = try await self.loadAudio(for: message, base: source)
                try Task.checkCancellation()
                guard self.isCurrent(key, source: source) else { return }
                let text = try await transcriber.transcribe(audio: data)
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                try Task.checkCancellation()
                guard self.isCurrent(key, source: source) else { return }
                self.transcriptionStates[key] = text.isEmpty ? .noSpeech : .ready(text)
                self.transcriptCache.save(text, for: key)
            } catch {
                guard self.isCurrent(key, source: source) else { return }
                if Task.isCancelled || error is CancellationError {
                    self.transcriptionStates[key] = .queued
                } else {
                    self.transcriptionStates[key] = .failed(error.localizedDescription)
                }
            }
        }
    }
}
