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

    private func perform(_ path: String, method: String = "GET", timeout: TimeInterval = 15) async throws -> Data {
        guard let base = HomeAssistantConnection.configuredBase else {
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
        isLoading = true
        defer { isLoading = false }
        do {
            let data = try await perform("api/callwebhook/mailbox")
            messages = try JSONDecoder().decode([MailboxMessage].self, from: data)
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func clearError() { errorMessage = nil }

    func setError(_ message: String) { errorMessage = message }

    func archive(_ message: MailboxMessage) async throws {
        let path = try messagePath(message)
        _ = try await perform("api/callwebhook/mailbox/\(path)/archive", method: "POST")
        await refresh()
    }

    func delete(_ message: MailboxMessage) async throws {
        let path = try messagePath(message)
        _ = try await perform("api/callwebhook/mailbox/\(path)", method: "DELETE")
        messages.removeAll { $0.id == message.id }
        errorMessage = nil
    }

    func loadAudio(for message: MailboxMessage) async throws -> Data {
        guard !message.audio.isEmpty else {
            throw NSError(domain: "CallWebhook.Mailbox", code: 404, userInfo: [NSLocalizedDescriptionKey: "Aufnahme nicht verfügbar"])
        }
        let path = try messagePath(message)
        // Fetch from the configured HA only; never send its bearer token to an
        // absolute host embedded in a mailbox response from an old installation.
        let prefix = message.isArchived ? "api/callwebhook/archive/audio" : "api/callwebhook/audio"
        return try await perform("\(prefix)/\(path)", timeout: 45)
    }
}
