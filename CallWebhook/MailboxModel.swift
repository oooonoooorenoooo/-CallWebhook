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

    func refresh() async {
        isLoading = true
        defer { isLoading = false }

        guard let url = URL(string: "\(baseURL)/local/callwebhook/mailbox.json") else {
            errorMessage = "Ungültige Mailbox-URL"
            return
        }

        var request = URLRequest(url: url)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        if let token = UserDefaults.standard.string(forKey: "haToken"),
           !token.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            request.setValue("Bearer \(token.trimmingCharacters(in: .whitespacesAndNewlines))", forHTTPHeaderField: "Authorization")
        }

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

    func audioURL(for message: MailboxMessage) -> URL? {
        if message.audio.hasPrefix("http") {
            return URL(string: message.audio)
        }
        return URL(string: "\(baseURL)\(message.audio)")
    }
}
