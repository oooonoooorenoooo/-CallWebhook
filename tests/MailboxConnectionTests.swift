import Foundation

// Exercise the real model with the shared authenticated transport replaced by a
// recorder. No legacy haToken defaults or hard-coded cloud address are provided.
@MainActor
enum HomeAssistantConnection {
    static var configuredBase: URL? = URL(string: "http://192.168.178.26:8123")!
    static var calls: [(String, String)] = []
    static var responseCode = 200
    static let fixture = Data("""
    [{"index":"7","tam":"0","called":"12345","date":"2026-09-29","duration":"10","name":"Test","number":"0301234567","new":true,"audio":"https://old.invalid/api/callwebhook/audio/0/7","archived":false}]
    """.utf8)
    static func request(base: URL, path: String, method: String = "GET") async throws -> (Data, Int) {
        precondition(base == configuredBase)
        precondition(!path.contains("://"))
        calls.append((path, method))
        return (path == "api/callwebhook/mailbox" ? fixture : Data([1, 2, 3]), responseCode)
    }
}

@main @MainActor
struct MailboxConnectionTests {
    static func main() async throws {
        UserDefaults.standard.removeObject(forKey: "haToken")
        let model = MailboxModel()
        await model.refresh()
        precondition(model.errorMessage == nil && model.messages.count == 1)
        let message = model.messages[0]
        let audio = try await model.loadAudio(for: message)
        precondition(audio == Data([1, 2, 3]))
        precondition(HomeAssistantConnection.calls.last!.0 == "api/callwebhook/audio/0/7")
        try await model.archive(message)
        precondition(HomeAssistantConnection.calls.contains { $0.0 == "api/callwebhook/mailbox/0/7/archive" && $0.1 == "POST" })
        try await model.delete(message)
        precondition(model.messages.isEmpty)
        precondition(HomeAssistantConnection.calls.last!.1 == "DELETE")
        HomeAssistantConnection.responseCode = 401
        await model.refresh()
        precondition(model.errorMessage?.contains("401") == true)
        HomeAssistantConnection.configuredBase = nil
        await model.refresh()
        precondition(model.errorMessage?.contains("noch nicht eingerichtet") == true)
        print("Mailbox uses shared setup connection for list, audio, archive and delete")
    }
}
