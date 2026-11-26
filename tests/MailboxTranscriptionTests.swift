import Foundation

// Run the production mailbox queue and disk cache with a controlled transport.
// Recognition deliberately ignores cancellation until completed by the test,
// reproducing callbacks that arrive after a tab closes or a message is deleted.
@MainActor
enum HomeAssistantConnection {
    struct Request {
        let base: URL
        let path: String
        let method: String
    }

    static let instanceA = URL(string: "https://first.invalid")!
    static let instanceB = URL(string: "https://second.invalid")!
    static var configuredBase: URL? = instanceA
    static var messagesByBase: [URL: [MailboxMessage]] = [:]
    static var requests: [Request] = []
    static var holdNextList = false
    static var heldList: (CheckedContinuation<(Data, Int), Error>, Data)?
    static var holdNextAudio = false
    static var heldAudio: (CheckedContinuation<(Data, Int), Error>, Data)?

    static func reset(_ messages: [MailboxMessage]) {
        precondition(heldList == nil && heldAudio == nil)
        configuredBase = instanceA
        messagesByBase = [instanceA: messages]
        requests = []
        holdNextList = false
        holdNextAudio = false
    }

    static func request(base: URL, path: String, method: String = "GET",
                        timeout: TimeInterval = 15) async throws -> (Data, Int) {
        precondition(!path.contains("://"), "Mailbox must never follow an audio URL to another host")
        requests.append(Request(base: base, path: path, method: method))
        if path == "api/callwebhook/mailbox" {
            let snapshot = try JSONEncoder().encode(messagesByBase[base] ?? [])
            if holdNextList {
                holdNextList = false
                return try await withCheckedThrowingContinuation { heldList = ($0, snapshot) }
            }
            return (snapshot, 200)
        }
        if method == "DELETE" {
            let suffix = path.split(separator: "/").suffix(2).joined(separator: "-")
            messagesByBase[base]?.removeAll { $0.id == suffix }
            return (Data(), 200)
        }
        if method == "POST" && path.hasSuffix("/archive") {
            let parts = path.split(separator: "/")
            let id = "\(parts[parts.count - 3])-\(parts[parts.count - 2])"
            messagesByBase[base] = (messagesByBase[base] ?? []).map { message in
                guard message.id == id else { return message }
                return MailboxMessage(index: message.index, tam: message.tam, called: message.called,
                    date: message.date, duration: message.duration, name: message.name, number: message.number,
                    isNew: false, audio: "/api/callwebhook/archive/audio/\(message.tam)/\(message.index)", archived: true)
            }
            return (Data(), 200)
        }
        precondition(path.hasPrefix("api/callwebhook/audio/") || path.hasPrefix("api/callwebhook/archive/audio/"))
        precondition(timeout == 45)
        let data = Data("\(base.absoluteString)|\(path)".utf8)
        if holdNextAudio {
            holdNextAudio = false
            return try await withCheckedThrowingContinuation { heldAudio = ($0, data) }
        }
        return (data, 200)
    }

    static func releaseList() {
        let pending = heldList!
        heldList = nil
        pending.0.resume(returning: (pending.1, 200))
    }

    static func releaseAudio() {
        let pending = heldAudio!
        heldAudio = nil
        pending.0.resume(returning: (pending.1, 200))
    }
}

@MainActor
final class ControlledTranscriber: VoicemailTranscribing {
    enum Failure: LocalizedError {
        case unavailable
        var errorDescription: String? { "Test: Spracherkennung nicht verfügbar" }
    }

    private var pending: [CheckedContinuation<String, Error>] = []
    private(set) var calls: [Data] = []
    private(set) var active = 0
    private(set) var maximumActive = 0

    var pendingCount: Int { pending.count }

    func transcribe(audio: Data) async throws -> String {
        calls.append(audio)
        active += 1
        maximumActive = max(maximumActive, active)
        defer { active -= 1 }
        return try await withCheckedThrowingContinuation { pending.append($0) }
    }

    func succeed(_ text: String) {
        precondition(!pending.isEmpty)
        pending.removeFirst().resume(returning: text)
    }

    func fail() {
        precondition(!pending.isEmpty)
        pending.removeFirst().resume(throwing: Failure.unavailable)
    }
}

@main @MainActor
struct MailboxTranscriptionTests {
    static func message(index: String = "7", date: String = "26.11.26 18:00") -> MailboxMessage {
        MailboxMessage(index: index, tam: "0", called: "030000001", date: date,
            duration: "00:20", name: "Test", number: "030000002", isNew: true,
            audio: "https://untrusted.invalid/recording.wav", archived: false)
    }

    static func key(_ message: MailboxMessage, source: URL? = nil) -> String {
        MailboxTranscriptionIdentity.key(source: (source ?? HomeAssistantConnection.instanceA).absoluteString, message: message)
    }

    static func waitUntil(_ label: String, _ condition: @MainActor () -> Bool) async {
        for _ in 0..<2_000 {
            if condition() { return }
            await Task.yield()
        }
        preconditionFailure("Timed out waiting for \(label)")
    }

    static func settle() async {
        for _ in 0..<20 { await Task.yield() }
    }

    static func main() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("CallWebhook-transcript-tests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        try await serialQueueAndPersistentResults(directory.appendingPathComponent("serial.json"))
        try await archiveAndIndexReuse(directory.appendingPathComponent("identity.json"))
        try await deletionRejectsLateResult(directory.appendingPathComponent("delete.json"))
        await cancellationDrainsBeforeResume(directory.appendingPathComponent("cancel.json"))
        await failuresRequireExplicitRetry(directory.appendingPathComponent("retry.json"))
        await sourceChangeRejectsLateResult(directory.appendingPathComponent("source.json"))
        try await staleRefreshCannotRestoreDeletedMessage(directory.appendingPathComponent("refresh.json"))
        await sourceChangeDuringDownload(directory.appendingPathComponent("download.json"))
        await refreshLoopStopsOnCancellation(directory.appendingPathComponent("polling.json"))
        await obsoleteSnapshotsCannotMutateReplacement(directory.appendingPathComponent("obsolete.json"))
        try await publicAudioRejectsDeletedMessage(directory.appendingPathComponent("deleted-audio.json"))
        await publicAudioRejectsChangedSource(directory.appendingPathComponent("source-audio.json"))
        print("12 mailbox transcription scenarios passed: queue/cache lifecycle, archive/index identity, stale mutation guards, late audio rejection, cancellation, retry, and source isolation")
    }

    static func serialQueueAndPersistentResults(_ url: URL) async throws {
        let first = message()
        let second = message(index: "8")
        HomeAssistantConnection.reset([first, second])
        let recognizer = ControlledTranscriber()
        let model = MailboxModel(transcriber: recognizer, transcriptCache: MailboxTranscriptCache(fileURL: url))
        model.setTranscriptionActive(true)
        await model.refresh()
        await waitUntil("first recognition") { recognizer.pendingCount == 1 }
        await model.refresh()
        await model.refresh()
        await settle()
        precondition(recognizer.calls.count == 1 && recognizer.maximumActive == 1)
        precondition(model.transcription(for: second) == .queued)
        recognizer.succeed("  Erste Nachricht. \n")
        await waitUntil("second recognition") { recognizer.calls.count == 2 && recognizer.pendingCount == 1 }
        precondition(model.transcription(for: first) == .ready("Erste Nachricht."))
        recognizer.succeed("")
        await waitUntil("completed silence") { model.transcription(for: second) == .noSpeech }
        model.setTranscriptionActive(false)
        precondition(recognizer.maximumActive == 1)

        // A new store instance must recover successful text and genuine silence
        // from disk without redownloading or re-running speech recognition.
        let recoveredRecognizer = ControlledTranscriber()
        let recovered = MailboxModel(transcriber: recoveredRecognizer,
            transcriptCache: MailboxTranscriptCache(fileURL: url))
        let audioRequests = HomeAssistantConnection.requests.filter { $0.path.contains("/audio/") }.count
        await recovered.refresh()
        recovered.setTranscriptionActive(true)
        await settle()
        precondition(recovered.transcription(for: first) == .ready("Erste Nachricht."))
        precondition(recovered.transcription(for: second) == .noSpeech)
        precondition(recoveredRecognizer.calls.isEmpty)
        precondition(HomeAssistantConnection.requests.filter { $0.path.contains("/audio/") }.count == audioRequests)
        recovered.setTranscriptionActive(false)
    }

    static func archiveAndIndexReuse(_ url: URL) async throws {
        let original = message()
        HomeAssistantConnection.reset([original])
        let cache = MailboxTranscriptCache(fileURL: url)
        cache.save("Bereits erkannt.", for: key(original))
        let recognizer = ControlledTranscriber()
        let model = MailboxModel(transcriber: recognizer, transcriptCache: cache)
        await model.refresh()
        try await model.archive(original)
        precondition(model.messages.count == 1 && model.messages[0].isArchived && !model.messages[0].isNew)
        precondition(model.transcription(for: model.messages[0]) == .ready("Bereits erkannt."))
        // An already-open player may still hold the unarchived value. The
        // current message determines which authenticated audio route is used.
        _ = try await model.loadAudio(for: original)
        precondition(HomeAssistantConnection.requests.last?.path == "api/callwebhook/archive/audio/0/7")
        model.setTranscriptionActive(true)
        await settle()
        precondition(recognizer.calls.isEmpty, "Archive/read changes must retain the existing transcript")

        let replacement = message(date: "26.11.26 18:01")
        HomeAssistantConnection.messagesByBase[HomeAssistantConnection.instanceA] = [replacement]
        await model.refresh()
        await waitUntil("reused index recognition") { recognizer.pendingCount == 1 }
        precondition(model.transcription(for: replacement) == .transcribing)
        precondition(model.transcription(for: original) == nil)
        recognizer.succeed("Neue Aufnahme.")
        await waitUntil("replacement result") { model.transcription(for: replacement) == .ready("Neue Aufnahme.") }
        model.setTranscriptionActive(false)
        precondition(cache.text(for: key(replacement)) == "Neue Aufnahme.")
    }

    static func deletionRejectsLateResult(_ url: URL) async throws {
        let recording = message()
        HomeAssistantConnection.reset([recording])
        let recognizer = ControlledTranscriber()
        let cache = MailboxTranscriptCache(fileURL: url)
        let model = MailboxModel(transcriber: recognizer, transcriptCache: cache)
        model.setTranscriptionActive(true)
        await model.refresh()
        await waitUntil("recognition before deletion") { recognizer.pendingCount == 1 }
        try await model.delete(recording)
        precondition(model.messages.isEmpty && model.transcription(for: recording) == nil)
        recognizer.succeed("Verspätetes Ergebnis einer gelöschten Nachricht.")
        await waitUntil("deleted job finishes") { recognizer.active == 0 }
        await settle()
        precondition(model.messages.isEmpty && model.transcriptionStates.isEmpty)
        precondition(cache.text(for: key(recording)) == nil)
        precondition(MailboxTranscriptCache(fileURL: url).text(for: key(recording)) == nil)
        model.setTranscriptionActive(false)
    }

    static func cancellationDrainsBeforeResume(_ url: URL) async {
        let recording = message()
        HomeAssistantConnection.reset([recording])
        let recognizer = ControlledTranscriber()
        let cache = MailboxTranscriptCache(fileURL: url)
        let model = MailboxModel(transcriber: recognizer, transcriptCache: cache)
        model.setTranscriptionActive(true)
        await model.refresh()
        await waitUntil("recognition before pause") { recognizer.pendingCount == 1 }
        model.setTranscriptionActive(false)
        model.setTranscriptionActive(true)
        await model.refresh()
        await settle()
        precondition(recognizer.calls.count == 1 && recognizer.active == 1,
            "Rapid reactivation must wait for cancellation cleanup")
        recognizer.succeed("Abgebrochenes Ergebnis.")
        await waitUntil("recognition resumes") { recognizer.calls.count == 2 && recognizer.pendingCount == 1 }
        precondition(cache.text(for: key(recording)) == nil)
        recognizer.succeed("Vollständig neu erkannt.")
        await waitUntil("resumed result") { model.transcription(for: recording) == .ready("Vollständig neu erkannt.") }
        precondition(recognizer.maximumActive == 1)
        model.setTranscriptionActive(false)
    }

    static func failuresRequireExplicitRetry(_ url: URL) async {
        let recording = message()
        HomeAssistantConnection.reset([recording])
        let recognizer = ControlledTranscriber()
        let model = MailboxModel(transcriber: recognizer, transcriptCache: MailboxTranscriptCache(fileURL: url))
        model.setTranscriptionActive(true)
        await model.refresh()
        await waitUntil("recognition before failure") { recognizer.pendingCount == 1 }
        recognizer.fail()
        await waitUntil("failed state") {
            if case .some(.failed(_)) = model.transcription(for: recording) { return true }
            return false
        }
        await model.refresh()
        await model.refresh()
        model.setTranscriptionActive(false)
        model.setTranscriptionActive(true)
        await settle()
        precondition(recognizer.calls.count == 1, "Refresh/tab changes must not repeatedly retry failed recognition")
        model.retryTranscription(for: recording)
        await waitUntil("explicit retry") { recognizer.calls.count == 2 && recognizer.pendingCount == 1 }
        recognizer.succeed("Erneuter Versuch erfolgreich.")
        await waitUntil("retried result") { model.transcription(for: recording) == .ready("Erneuter Versuch erfolgreich.") }
        model.setTranscriptionActive(false)
    }

    static func sourceChangeRejectsLateResult(_ url: URL) async {
        let first = message()
        // Identical recording IDs/metadata on another HA must have an isolated cache.
        HomeAssistantConnection.reset([first])
        HomeAssistantConnection.messagesByBase[HomeAssistantConnection.instanceB] = [first]
        let recognizer = ControlledTranscriber()
        let cache = MailboxTranscriptCache(fileURL: url)
        let model = MailboxModel(transcriber: recognizer, transcriptCache: cache)
        model.setTranscriptionActive(true)
        await model.refresh()
        await waitUntil("first instance recognition") { recognizer.pendingCount == 1 }
        HomeAssistantConnection.configuredBase = HomeAssistantConnection.instanceB
        precondition(model.transcription(for: first) == nil, "A changed connection must immediately hide old-instance text")
        await model.refresh()
        recognizer.succeed("Späte Antwort der ersten Instanz.")
        await waitUntil("second instance recognition") { recognizer.calls.count == 2 && recognizer.pendingCount == 1 }
        precondition(String(decoding: recognizer.calls[1], as: UTF8.self).hasPrefix(HomeAssistantConnection.instanceB.absoluteString))
        precondition(cache.text(for: key(first)) == nil)
        recognizer.succeed("Zweite Instanz.")
        await waitUntil("second instance result") { model.transcription(for: first) == .ready("Zweite Instanz.") }
        precondition(cache.text(for: key(first, source: HomeAssistantConnection.instanceB)) == "Zweite Instanz.")
        precondition(cache.text(for: key(first)) == nil)
        model.setTranscriptionActive(false)
    }

    static func staleRefreshCannotRestoreDeletedMessage(_ url: URL) async throws {
        let recording = message()
        HomeAssistantConnection.reset([recording])
        let model = MailboxModel(transcriptCache: MailboxTranscriptCache(fileURL: url))
        await model.refresh()
        HomeAssistantConnection.holdNextList = true
        let refresh = Task { await model.refresh() }
        await waitUntil("held list response") { HomeAssistantConnection.heldList != nil }
        try await model.delete(recording)
        HomeAssistantConnection.releaseList()
        await refresh.value
        precondition(model.messages.isEmpty && model.transcriptionStates.isEmpty,
            "A pre-deletion list response must not resurrect a deleted message")
    }

    static func sourceChangeDuringDownload(_ url: URL) async {
        let recording = message()
        HomeAssistantConnection.reset([recording])
        HomeAssistantConnection.messagesByBase[HomeAssistantConnection.instanceB] = [recording]
        HomeAssistantConnection.holdNextAudio = true
        let recognizer = ControlledTranscriber()
        let model = MailboxModel(transcriber: recognizer, transcriptCache: MailboxTranscriptCache(fileURL: url))
        model.setTranscriptionActive(true)
        await model.refresh()
        await waitUntil("held first-instance audio") { HomeAssistantConnection.heldAudio != nil }
        HomeAssistantConnection.configuredBase = HomeAssistantConnection.instanceB
        await model.refresh()
        HomeAssistantConnection.releaseAudio()
        await waitUntil("current-instance recognition") { recognizer.pendingCount == 1 }
        precondition(recognizer.calls.count == 1)
        precondition(String(decoding: recognizer.calls[0], as: UTF8.self).hasPrefix(HomeAssistantConnection.instanceB.absoluteString),
            "Audio downloaded for an obsolete instance must never enter recognition")
        recognizer.succeed("Aktuelle Verbindung.")
        await waitUntil("current-instance result") { model.transcription(for: recording) == .ready("Aktuelle Verbindung.") }
        model.setTranscriptionActive(false)
    }

    static func refreshLoopStopsOnCancellation(_ url: URL) async {
        HomeAssistantConnection.reset([])
        let model = MailboxModel(transcriptCache: MailboxTranscriptCache(fileURL: url))
        let polling = Task { await model.refreshAutomatically() }
        await waitUntil("automatic first refresh") { HomeAssistantConnection.requests.count == 1 }
        polling.cancel()
        await polling.value
        precondition(HomeAssistantConnection.requests.count == 1)
        precondition(!model.isLoading)
    }

    static func obsoleteSnapshotsCannotMutateReplacement(_ url: URL) async {
        let original = message()
        let replacement = message(date: "26.11.26 18:01")
        HomeAssistantConnection.reset([original])
        let cache = MailboxTranscriptCache(fileURL: url)
        cache.save("Alte Nachricht.", for: key(original))
        let model = MailboxModel(transcriptCache: cache)
        await model.refresh()
        precondition(model.transcription(for: original) == .ready("Alte Nachricht."))
        HomeAssistantConnection.messagesByBase[HomeAssistantConnection.instanceA] = [replacement]
        await model.refresh()
        let requestsBeforeMutation = HomeAssistantConnection.requests.count

        do {
            try await model.delete(original)
            preconditionFailure("Deleting an obsolete snapshot must fail before the HTTP request")
        } catch {
            precondition(HomeAssistantConnection.requests.count == requestsBeforeMutation)
        }
        do {
            try await model.archive(original)
            preconditionFailure("Archiving an obsolete snapshot must fail before the HTTP request")
        } catch {
            precondition(HomeAssistantConnection.requests.count == requestsBeforeMutation)
        }
        precondition(model.messages.count == 1 && model.messages[0].date == replacement.date)
        precondition(!HomeAssistantConnection.requests.contains { $0.method == "DELETE" || $0.method == "POST" })
    }

    static func publicAudioRejectsDeletedMessage(_ url: URL) async throws {
        let recording = message()
        HomeAssistantConnection.reset([recording])
        let model = MailboxModel(transcriptCache: MailboxTranscriptCache(fileURL: url))
        await model.refresh()
        HomeAssistantConnection.holdNextAudio = true
        let audioWasRejected = Task {
            do {
                _ = try await model.loadAudio(for: recording)
                return false
            } catch {
                return true
            }
        }
        await waitUntil("public audio before deletion") { HomeAssistantConnection.heldAudio != nil }
        try await model.delete(recording)
        HomeAssistantConnection.releaseAudio()
        let rejected = await audioWasRejected.value
        precondition(rejected, "A public audio fetch must reject data received after its message was deleted")
        precondition(model.messages.isEmpty)
    }

    static func publicAudioRejectsChangedSource(_ url: URL) async {
        let recording = message()
        HomeAssistantConnection.reset([recording])
        HomeAssistantConnection.messagesByBase[HomeAssistantConnection.instanceB] = [recording]
        let model = MailboxModel(transcriptCache: MailboxTranscriptCache(fileURL: url))
        await model.refresh()
        HomeAssistantConnection.holdNextAudio = true
        let audioWasRejected = Task {
            do {
                _ = try await model.loadAudio(for: recording)
                return false
            } catch {
                return true
            }
        }
        await waitUntil("public audio before source change") { HomeAssistantConnection.heldAudio != nil }
        HomeAssistantConnection.configuredBase = HomeAssistantConnection.instanceB
        await model.refresh()
        HomeAssistantConnection.releaseAudio()
        let rejected = await audioWasRejected.value
        precondition(rejected, "A public audio fetch must reject bytes from a connection that is no longer current")
        precondition(model.messages.count == 1)
    }
}
