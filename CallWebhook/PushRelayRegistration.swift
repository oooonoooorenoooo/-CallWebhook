import Foundation
import CryptoKit
import DeviceCheck
import Security

private final class PushRelaySessionDelegate: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

@MainActor
final class PushRelayRegistration {
    static let shared = PushRelayRegistration()
    private let session = URLSession(configuration: .ephemeral, delegate: PushRelaySessionDelegate(), delegateQueue: nil)
    private var discoveredURL: URL?

    func discoverOperatorService() async throws {
        guard baseURL == nil, let ha = HomeAssistantConnection.configuredBase else { return }
        let (data, code) = try await HomeAssistantConnection.request(base: ha,
            path: "api/callwebhook/push-relay/host", timeout: 35)
        guard code == 200, let status = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        guard status["available"] as? Bool == true else {
            if status["operator"] as? Bool == true {
                throw failure(status["message"] as? String ?? "Betreiber-Push-Dienst noch nicht erreichbar")
            }
            return
        }
        discoveredURL = validatedURL(status["public_url"] as? String ?? "")
    }

    var baseURL: URL? {
        if let value = Bundle.main.object(forInfoDictionaryKey: "CallWebhookPushRelayURL") as? String,
           let configured = validatedURL(value) { return configured }
        return discoveredURL
    }

    private func validatedURL(_ value: String) -> URL? {
        guard let url = URL(string: value), url.scheme == "https", url.host != nil,
              url.user == nil, url.password == nil, url.query == nil, url.fragment == nil else { return nil }
        let path = url.path.hasSuffix("/") ? String(url.path.dropLast()) : url.path
        guard ["", "/api/callwebhook/push-relay"].contains(path) else { return nil }
        return url
    }

    func invalidateRegistration() {
        UserDefaults.standard.removeObject(forKey: "pushRelayRegistrationFingerprint")
    }

    func register(token: String, environment: String) async throws -> (url: String, credential: String) {
        guard let base = baseURL else {
            throw failure("Der gemeinsame Push-Dienst wurde vom App-Anbieter noch nicht bereitgestellt. Du musst keinen Apple-Schlüssel eintragen.")
        }
        let service = DCAppAttestService.shared
        guard service.isSupported else { throw failure("Dieses Gerät unterstützt die sichere Push-Anmeldung nicht.") }
        let defaults = UserDefaults.standard
        // App Attest keys don't survive reinstallation; UserDefaults intentionally
        // keeps the identifier out of the otherwise persistent setup keychain.
        let keyAccount = "pushRelayAppAttestKey-" + hash(base.absoluteString + environment)
        var keyID = defaults.string(forKey: keyAccount) ?? ""
        if keyID.isEmpty {
            keyID = try await service.generateKey()
            defaults.set(keyID, forKey: keyAccount)
        }
        var credential = try grant(for: keyID)
        var fingerprint = hash(base.absoluteString + environment + token + keyID + credential)
        if defaults.string(forKey: "pushRelayRegistrationFingerprint") == fingerprint {
            return (base.absoluteString.trimmingCharacters(in: CharacterSet(charactersIn: "/")), credential)
        }
        func challenge(_ id: String, _ grant: String) async throws -> [String: Any] {
            try await request(base: base, path: "v1/challenge", body: [
                "key_id": id, "token": token, "environment": environment, "credential_hash": hash(grant)
            ])
        }
        var response = try await challenge(keyID, credential)
        if response["attested"] as? Bool == false && defaults.bool(forKey: keyAccount + "-attested") {
            // A restored/new relay database cannot validate old assertions.
            // App Attest cannot attest the same key twice with a new challenge.
            keyID = try await service.generateKey()
            defaults.set(keyID, forKey: keyAccount)
            defaults.removeObject(forKey: keyAccount + "-attested")
            credential = try grant(for: keyID)
            fingerprint = hash(base.absoluteString + environment + token + keyID + credential)
            response = try await challenge(keyID, credential)
        }
        guard let nonce = response["nonce"] as? String,
              let challengeID = response["challenge_id"] as? String,
              let attested = response["attested"] as? Bool else { throw URLError(.badServerResponse) }
        let clientData = ["callwebhook-register-v1", nonce, keyID, environment, token, hash(credential)].joined(separator: "\n")
        let digest = Data(SHA256.hash(data: Data(clientData.utf8)))
        let proof: Data
        if attested {
            proof = try await service.generateAssertion(keyID, clientDataHash: digest)
        } else {
            proof = try await service.attestKey(keyID, clientDataHash: digest)
            defaults.set(true, forKey: keyAccount + "-attested")
        }
        let result = try await request(base: base, path: "v1/register", body: [
            "challenge_id": challengeID, (attested ? "assertion" : "attestation"): proof.base64EncodedString()
        ])
        guard result["registered"] as? Bool == true else { throw URLError(.badServerResponse) }
        defaults.set(fingerprint, forKey: "pushRelayRegistrationFingerprint")
        return (base.absoluteString.trimmingCharacters(in: CharacterSet(charactersIn: "/")), credential)
    }

    private func grant(for keyID: String) throws -> String {
        let account = "push-relay-grant-" + hash(keyID)
        if let existing = SetupKeychain.get(account: account) { return existing }
        var bytes = [UInt8](repeating: 0, count: 32)
        guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else { throw URLError(.cannotCreateFile) }
        let value = bytes.map { String(format: "%02x", $0) }.joined()
        try SetupKeychain.set(value, account: account)
        return value
    }

    private func request(base: URL, path: String, body: [String: Any]) async throws -> [String: Any] {
        var request = URLRequest(url: base.appendingPathComponent(path))
        request.httpMethod = "POST"
        request.timeoutInterval = 30
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            throw failure("Die sichere Anmeldung beim Push-Dienst ist fehlgeschlagen. Bitte erneut versuchen.")
        }
        return try JSONSerialization.jsonObject(with: data) as? [String: Any] ?? [:]
    }

    private func hash(_ value: String) -> String {
        SHA256.hash(data: Data(value.utf8)).map { String(format: "%02x", $0) }.joined()
    }

    private func failure(_ message: String) -> NSError {
        NSError(domain: "CallWebhook.PushRelay", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
    }
}
