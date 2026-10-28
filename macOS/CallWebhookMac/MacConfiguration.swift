import Foundation
import Security

struct MacConfiguration: Codable {
    var host = ""
    // This identity is intentionally separate from callwebhook-ios.
    var username: String { "callwebhook-mac" }
    var homeAssistant = ""
    var mailboxTAM = ""

    static func load() -> Self {
        UserDefaults.standard.data(forKey: "mac.configuration")
            .flatMap { try? JSONDecoder().decode(Self.self, from: $0) } ?? Self()
    }

    func save() throws {
        try validate()
        UserDefaults.standard.set(try JSONEncoder().encode(self), forKey: "mac.configuration")
    }

    func validate() throws {
        guard !host.isEmpty, host.allSatisfy({ $0.isASCII && ($0.isLetter || $0.isNumber || ".-:[]".contains($0)) }), let url = URL(string: "sip://" + host),
              url.host != nil, url.user == nil, url.password == nil,
              url.path.isEmpty, url.query == nil, url.fragment == nil,
              !host.contains(";"), !host.contains(where: { $0.isWhitespace }),
              url.port.map({ (1...65535).contains($0) }) ?? true else {
            throw MacError.message("Gültigen Asterisk-Host mit optionalem Port eingeben.")
        }
        if !homeAssistant.isEmpty {
            guard let url = URL(string: homeAssistant), ["http", "https"].contains(url.scheme),
                  url.host != nil, url.user == nil, url.password == nil,
                  url.query == nil, url.fragment == nil else {
                throw MacError.message("Gültige Home-Assistant-Adresse eingeben.")
            }
        }
    }
}

enum MacError: LocalizedError {
    case message(String)
    var errorDescription: String? { if case let .message(text) = self { return text }; return nil }
}

enum MacSecrets {
    private static let service = "de.reno.CallWebhookMac"
    static func get(_ name: String) -> String? {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service, kSecAttrAccount as String: name,
            kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }
    static func set(_ value: String, name: String) throws {
        // An empty settings field preserves an existing secret.
        guard !value.isEmpty else { return }
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service, kSecAttrAccount as String: name]
        let values = [kSecValueData as String: Data(value.utf8)]
        let status = SecItemUpdate(query as CFDictionary, values as CFDictionary)
        if status == errSecItemNotFound {
            var item = query
            item.merge(values) { _, new in new }
            item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
            guard SecItemAdd(item as CFDictionary, nil) == errSecSuccess else {
                throw MacError.message("Zugangsdaten konnten nicht im Schlüsselbund gespeichert werden.")
            }
        } else if status != errSecSuccess {
            throw MacError.message("Schlüsselbund nicht verfügbar (\(status)).")
        }
    }
}
