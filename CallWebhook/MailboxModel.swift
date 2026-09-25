import Foundation

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

    var id: String { "\(tam)-\(index)" }

    enum CodingKeys: String, CodingKey {
        case index, tam, called, date, duration, name, number, audio
        case isNew = "new"
    }
}

@MainActor
final class MailboxModel: ObservableObject {
    @Published private(set) var messages: [MailboxMessage] = []
    @Published private(set) var isLoading = false
    @Published private(set) var errorMessage: String?

    private let baseURL = "https://vjid3noccsptgcivfuw9dqz15dzvygte.ui.nabu.casa"

    private var token: String {
        UserDefaults.standard.string(forKey: "haToken")?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    }

    func refresh() async {
        isLoading = true
        defer { isLoading = false }

        guard !token.isEmpty else {
            messages = []
            errorMessage = "Home-Assistant-Token fehlt"
            return
        }

        guard let url = URL(string: "\(baseURL)/api/callwebhook/mailbox") else {
            errorMessage = "Ungültige Mailbox-URL"
            return
        }

        var request = URLRequest(url: url)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")

        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) else {
                let code = (response as? HTTPURLResponse)?.statusCode ?? -1
                throw NSError(domain: "CallWebhook.Mailbox", code: code, userInfo: [NSLocalizedDescriptionKey: "Home Assistant HTTP \(code)"])
            }
            messages = try JSONDecoder().decode([MailboxMessage].self, from: data)
            errorMessage = nil
        } catch {
            messages = []
            errorMessage = error.localizedDescription
        }
    }

    func loadAudio(for message: MailboxMessage) async throws -> Data {
        guard !token.isEmpty else {
            throw NSError(domain: "CallWebhook.Mailbox", code: 401, userInfo: [NSLocalizedDescriptionKey: "Home-Assistant-Token fehlt"])
        }
        guard !message.audio.isEmpty else {
            throw NSError(domain: "CallWebhook.Mailbox", code: 404, userInfo: [NSLocalizedDescriptionKey: "Aufnahme nicht verfügbar"])
        }
        let address = message.audio.hasPrefix("http") ? message.audio : "\(baseURL)\(message.audio)"
        guard let url = URL(string: address) else {
            throw NSError(domain: "CallWebhook.Mailbox", code: -1, userInfo: [NSLocalizedDescriptionKey: "Ungültige Audio-URL"])
        }

        var request = URLRequest(url: url)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")

        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) else {
            let code = (response as? HTTPURLResponse)?.statusCode ?? -1
            throw NSError(domain: "CallWebhook.Mailbox", code: code, userInfo: [NSLocalizedDescriptionKey: "Audio HTTP \(code)"])
        }
        return data
    }
}
