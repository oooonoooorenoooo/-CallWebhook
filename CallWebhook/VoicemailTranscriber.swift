import AVFoundation
import Foundation
import Speech

/// Transcribes a downloaded answering-machine recording without opening the
/// microphone, changing the call's audio session, or uploading the recording.
@MainActor
final class VoicemailTranscriber: VoicemailTranscribing {
    // Asset reservations belong to the app, not an individual transcriber. Keep
    // sessions serial even if a second mailbox view creates another instance.
    private static var operationInProgress = false

    func transcribe(audio: Data) async throws -> String {
        try Task.checkCancellation()
        guard !Self.operationInProgress else {
            throw VoicemailTranscriptionError.busy
        }
        Self.operationInProgress = true
        defer { Self.operationInProgress = false }

        // File I/O and recognition run away from the UI executor. A detached
        // task needs explicit cancellation propagation from the mailbox queue.
        let operation = Task.detached(priority: .utility) {
            try await Self.transcribeRecording(audio)
        }
        return try await withTaskCancellationHandler {
            try await operation.value
        } onCancel: {
            operation.cancel()
        }
    }

    private nonisolated static func transcribeRecording(_ audio: Data) async throws -> String {
        try Task.checkCancellation()
        guard !audio.isEmpty else { throw VoicemailTranscriptionError.emptyRecording }
        guard SpeechTranscriber.isAvailable else {
            throw VoicemailTranscriptionError.deviceUnsupported
        }
        guard let locale = await SpeechTranscriber.supportedLocale(equivalentTo: Locale(identifier: "de-DE")) else {
            throw VoicemailTranscriptionError.germanUnsupported
        }
        try Task.checkCancellation()

        let fileURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("callwebhook-transcription-\(UUID().uuidString).wav")
        defer { try? FileManager.default.removeItem(at: fileURL) }

        let file: AVAudioFile
        do {
            #if os(iOS)
            try audio.write(to: fileURL, options: [.atomic, .completeFileProtection])
            #else
            try audio.write(to: fileURL, options: .atomic)
            #endif
            file = try AVAudioFile(forReading: fileURL)
        } catch {
            if Task.isCancelled { throw CancellationError() }
            throw VoicemailTranscriptionError.recordingUnreadable
        }
        guard file.length > 0 else { throw VoicemailTranscriptionError.emptyRecording }
        try Task.checkCancellation()

        let transcriber = SpeechTranscriber(locale: locale, preset: .transcription)
        let ownsReservation: Bool
        do {
            ownsReservation = try await AssetInventory.reserve(locale: locale)
        } catch {
            if Task.isCancelled { throw CancellationError() }
            throw VoicemailTranscriptionError.modelUnavailable(error.localizedDescription)
        }

        // Release only a reservation acquired here. A reservation that existed
        // before this operation may be used by another feature in the app.
        let outcome: Result<String, Error>
        do {
            try Task.checkCancellation()
            do {
                if let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
                    try await withTaskCancellationHandler {
                        try await request.downloadAndInstall()
                    } onCancel: {
                        request.progress.cancel()
                    }
                }
            } catch {
                if Task.isCancelled { throw CancellationError() }
                throw VoicemailTranscriptionError.modelUnavailable(error.localizedDescription)
            }
            try Task.checkCancellation()
            outcome = .success(try await recognize(file: file, with: transcriber))
        } catch {
            outcome = .failure(error)
        }
        if ownsReservation { await AssetInventory.release(reservedLocale: locale) }
        try Task.checkCancellation()
        return try outcome.get()
    }

    private nonisolated static func recognize(file: AVAudioFile, with transcriber: SpeechTranscriber) async throws -> String {
        let analyzer = SpeechAnalyzer(modules: [transcriber])
        let results = Task {
            var transcript = ""
            for try await result in transcriber.results {
                try Task.checkCancellation()
                // The transcription preset emits final, non-overlapping
                // passages. Do not append provisional revisions as duplicates.
                if result.isFinal { transcript += String(result.text.characters) }
            }
            return transcript.trimmingCharacters(in: .whitespacesAndNewlines)
        }

        do {
            let text = try await withTaskCancellationHandler {
                try Task.checkCancellation()
                // This helper reads/normalizes the entire file. Finalizing at
                // its last sample also waits for the tail of long recordings.
                if let lastSample = try await analyzer.analyzeSequence(from: file) {
                    try await analyzer.finalizeAndFinish(through: lastSample)
                } else {
                    await analyzer.cancelAndFinishNow()
                }
                let transcript = try await results.value
                try Task.checkCancellation()
                return transcript
            } onCancel: {
                results.cancel()
                Task { await analyzer.cancelAndFinishNow() }
            }
            // An empty final result means analysis completed without recognized
            // words. The mailbox represents this separately from a failed job.
            return text
        } catch {
            results.cancel()
            await analyzer.cancelAndFinishNow()
            // Drain the reader before the recording and its reservation are
            // released; no transcription task survives a failed/cancelled job.
            _ = await results.result
            if Task.isCancelled { throw CancellationError() }
            if let known = error as? VoicemailTranscriptionError { throw known }
            throw VoicemailTranscriptionError.recognitionFailed(error.localizedDescription)
        }
    }
}

private enum VoicemailTranscriptionError: LocalizedError {
    case busy
    case emptyRecording
    case recordingUnreadable
    case deviceUnsupported
    case germanUnsupported
    case modelUnavailable(String)
    case recognitionFailed(String)

    var errorDescription: String? {
        switch self {
        case .busy:
            return "Eine andere Nachricht wird gerade transkribiert. Bitte danach erneut versuchen."
        case .emptyRecording:
            return "Die Aufnahme enthält keine Audiodaten."
        case .recordingUnreadable:
            return "Die Aufnahme konnte für die Transkription nicht geöffnet werden."
        case .deviceUnsupported:
            return "Dieses Gerät unterstützt die lokale Transkription nicht. Du kannst die Aufnahme weiterhin anhören."
        case .germanUnsupported:
            return "Die lokale Transkription auf Deutsch ist auf diesem Gerät derzeit nicht verfügbar."
        case .modelUnavailable(let detail):
            return "Das deutsche Sprachmodell konnte nicht vorbereitet werden: \(detail)"
        case .recognitionFailed(let detail):
            return "Die Aufnahme konnte nicht transkribiert werden: \(detail)"
        }
    }
}
