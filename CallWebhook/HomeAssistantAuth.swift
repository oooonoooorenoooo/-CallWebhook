import Foundation
import AuthenticationServices
import UIKit

@MainActor
final class HomeAssistantAuth: NSObject, ASWebAuthenticationPresentationContextProviding {
    static let shared = HomeAssistantAuth()

    private let clientID = "https://oooonoooorenoooo.github.io/-CallWebhook/"
    private let callbackScheme = "callwebhook"
    private let redirectURI = "callwebhook://auth"

    private override init() {}

    func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
        let scenes = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
        return scenes.flatMap(\.windows).first(where: { $0.isKeyWindow }) ?? ASPresentationAnchor()
    }

    func authenticate(instance: URL) async throws -> String {
        let state = UUID().uuidString
        guard var components = URLComponents(url: instance.appendingPathComponent("auth/authorize"), resolvingAgainstBaseURL: false) else {
            throw URLError(.badURL)
        }
        components.queryItems = [
            URLQueryItem(name: "client_id", value: clientID),
            URLQueryItem(name: "redirect_uri", value: redirectURI),
            URLQueryItem(name: "state", value: state)
        ]
        guard let authURL = components.url else { throw URLError(.badURL) }

        let callbackURL: URL = try await withCheckedThrowingContinuation { continuation in
            let session = ASWebAuthenticationSession(url: authURL, callbackURLScheme: callbackScheme) { url, error in
                if let error {
                    continuation.resume(throwing: error)
                } else if let url {
                    continuation.resume(returning: url)
                } else {
                    continuation.resume(throwing: URLError(.badServerResponse))
                }
            }
            session.presentationContextProvider = self
            session.prefersEphemeralWebBrowserSession = false
            session.start()
        }

        guard let callback = URLComponents(url: callbackURL, resolvingAgainstBaseURL: false),
              callback.queryItems?.first(where: { $0.name == "state" })?.value == state,
              let code = callback.queryItems?.first(where: { $0.name == "code" })?.value,
              !code.isEmpty else {
            throw NSError(domain: "CallWebhook.HAAuth", code: 1, userInfo: [NSLocalizedDescriptionKey: "Ungültiger Home-Assistant-Anmelderücksprung"])
        }

        return try await exchange(code: code, instance: instance)
    }

    func refresh(instance: URL) async throws -> String {
        guard let refreshToken = SetupKeychain.get(account: "home-assistant-refresh-token"), !refreshToken.isEmpty else {
            throw NSError(domain: "CallWebhook.HAAuth", code: 2, userInfo: [NSLocalizedDescriptionKey: "Kein Home-Assistant-Refresh-Token vorhanden"])
        }
        return try await tokenRequest(instance: instance, fields: [
            "grant_type": "refresh_token",
            "refresh_token": refreshToken,
            "client_id": clientID
        ], storeRefreshToken: false)
    }

    private func exchange(code: String, instance: URL) async throws -> String {
        try await tokenRequest(instance: instance, fields: [
            "grant_type": "authorization_code",
            "code": code,
            "client_id": clientID
        ], storeRefreshToken: true)
    }

    private func tokenRequest(instance: URL, fields: [String: String], storeRefreshToken: Bool) async throws -> String {
        let url = instance.appendingPathComponent("auth/token")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 15
        request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        request.httpBody = fields
            .map { key, value in
                "\(formEncode(key))=\(formEncode(value))"
            }
            .sorted()
            .joined(separator: "&")
            .data(using: .utf8)

        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode),
              let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let accessToken = json["access_token"] as? String, !accessToken.isEmpty else {
            throw NSError(domain: "CallWebhook.HAAuth", code: 3, userInfo: [NSLocalizedDescriptionKey: "Home Assistant hat keine gültigen Tokens geliefert"])
        }

        try SetupKeychain.set(accessToken, account: "home-assistant-token")
        if storeRefreshToken, let refreshToken = json["refresh_token"] as? String, !refreshToken.isEmpty {
            try SetupKeychain.set(refreshToken, account: "home-assistant-refresh-token")
        }
        if let expiresIn = json["expires_in"] as? Double {
            try? SetupKeychain.set(String(Date().timeIntervalSince1970 + expiresIn), account: "home-assistant-token-expiry")
        }
        return accessToken
    }

    private func formEncode(_ value: String) -> String {
        var allowed = CharacterSet.alphanumerics
        allowed.insert(charactersIn: "-._~")
        return value.addingPercentEncoding(withAllowedCharacters: allowed) ?? value
    }
}
