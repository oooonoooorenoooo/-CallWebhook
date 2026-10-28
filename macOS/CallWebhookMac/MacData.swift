import Foundation
import Combine
import Contacts
import AVFoundation

struct MacContact: Identifiable {
    let id: String
    let name: String
    let number: String
}

@MainActor
final class MacContacts: ObservableObject {
    @Published var entries: [MacContact] = []
    @Published var error: String?
    func load() async {
        do {
            let store = CNContactStore()
            guard try await store.requestAccess(for: .contacts) else {
                throw MacError.message("Kontaktzugriff wurde nicht erlaubt.")
            }
            let keys: [CNKeyDescriptor] = [CNContactIdentifierKey as CNKeyDescriptor,
                CNContactGivenNameKey as CNKeyDescriptor, CNContactFamilyNameKey as CNKeyDescriptor,
                CNContactPhoneNumbersKey as CNKeyDescriptor]
            let result = try await Task.detached { () -> [MacContact] in
                let request = CNContactFetchRequest(keysToFetch: keys)
                var contacts: [MacContact] = []
                try CNContactStore().enumerateContacts(with: request) { contact, _ in
                    for phone in contact.phoneNumbers {
                        contacts.append(MacContact(id: contact.identifier + phone.identifier,
                            name: "\(contact.givenName) \(contact.familyName)".trimmingCharacters(in: .whitespaces),
                            number: phone.value.stringValue))
                    }
                }
                return contacts.sorted { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
            }.value
            entries = result; error = nil
        } catch { self.error = error.localizedDescription }
    }
}

struct MacMessage: Decodable, Identifiable {
    let index: String
    let tam: String
    let called: String
    let date: String
    let duration: String
    let name: String
    let number: String
    let audio: String
    let archived: Bool?
    var id: String { tam + "-" + index }
}

@MainActor
final class MacMailbox: ObservableObject {
    @Published var messages: [MacMessage] = []
    @Published var error: String?
    @Published var loading = false
    @Published var playingID: String?
    private var player: AVAudioPlayer?

    private func request(_ path: String, configuration: MacConfiguration) async throws -> Data {
        guard let base = URL(string: configuration.homeAssistant), base.host != nil,
              ["http", "https"].contains(base.scheme),
              let token = MacSecrets.get("ha"), !token.isEmpty else {
            throw MacError.message("Home Assistant und Zugriffstoken fehlen.")
        }
        var request = URLRequest(url: base.appendingPathComponent(path), timeoutInterval: 45)
        request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
        // Reject redirects rather than forwarding credentials or audio to another origin.
        let session = URLSession(configuration: .ephemeral, delegate: NoRedirect(), delegateQueue: nil)
        defer { session.finishTasksAndInvalidate() }
        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse, response.statusCode == 200 else {
            throw MacError.message("Mailbox konnte nicht geladen werden (HTTP \((response as? HTTPURLResponse)?.statusCode ?? 0)).")
        }
        return data
    }

    func refresh(_ configuration: MacConfiguration) async {
        guard !loading else { return }
        loading = true
        defer { loading = false }
        do {
            guard !configuration.mailboxTAM.isEmpty,
                  configuration.mailboxTAM.allSatisfy({ $0.isASCII && $0.isNumber }) else {
                messages = []
                throw MacError.message("Die ausschließlich Leitung 3 zugeordnete TAM-ID eintragen.")
            }
            let data = try await request("api/callwebhook/mailbox", configuration: configuration)
            messages = try JSONDecoder().decode([MacMessage].self, from: data)
                .filter { $0.tam == configuration.mailboxTAM }
            error = nil
        } catch { self.error = error.localizedDescription }
    }

    func play(_ message: MacMessage, configuration: MacConfiguration) async {
        do {
            guard message.tam == configuration.mailboxTAM,
                  [message.tam, message.index].allSatisfy({ !$0.isEmpty && $0.allSatisfy({ $0.isASCII && $0.isNumber }) }),
                  !message.audio.isEmpty else { throw MacError.message("Aufnahme nicht verfügbar.") }
            let route = message.archived == true ? "archive/audio" : "audio"
            let data = try await request("api/callwebhook/\(route)/\(message.tam)/\(message.index)", configuration: configuration)
            player?.stop()
            player = try AVAudioPlayer(data: data)
            guard player?.play() == true else { throw MacError.message("Audio konnte nicht abgespielt werden.") }
            playingID = message.id; error = nil
        } catch { self.error = error.localizedDescription }
    }
    func stop() { player?.stop(); playingID = nil }
}

private final class NoRedirect: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}
