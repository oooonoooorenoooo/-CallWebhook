import Foundation

@MainActor
enum HomeAssistantConnection {
    static var configuredBase: URL? {
        let value = UserDefaults.standard.string(forKey: "setupHomeAssistantURL") ?? ""
        guard !value.isEmpty else { return nil }
        return URL(string: value.contains("://") ? value : "http://192.168.178.\(value):8123")
    }

    static func request(base: URL, path: String, method: String = "GET", body: [String: Any]? = nil, timeout: TimeInterval = 15) async throws -> (Data, Int) {
        var token = try await HomeAssistantAuth.shared.validAccessToken(instance: base)
        var request = URLRequest(url: base.appendingPathComponent(path))
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.httpMethod = method
        request.timeoutInterval = timeout
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let body { request.httpBody = try JSONSerialization.data(withJSONObject: body) }
        for attempt in 0..<2 {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse else { throw URLError(.badServerResponse) }
            if http.statusCode == 401 && attempt == 0 {
                token = try await HomeAssistantAuth.shared.refreshAfterRejection(instance: base, rejectedToken: token)
                continue
            }
            return (data, http.statusCode)
        }
        throw URLError(.userAuthenticationRequired)
    }

    static func command(base: URL, payload: [String: Any]) async throws -> Any {
        // Refresh before opening a socket; a 401 cannot be handled after WS authentication.
        let (_, status) = try await request(base: base, path: "api/")
        guard status == 200, let token = SetupKeychain.get(account: "home-assistant-token") else {
            throw URLError(.userAuthenticationRequired)
        }
        var components = URLComponents(url: base, resolvingAgainstBaseURL: false)!
        components.scheme = base.scheme == "https" ? "wss" : "ws"
        components.path = "/api/websocket"
        components.query = nil
        guard let url = components.url else { throw URLError(.badURL) }
        let socket = URLSession.shared.webSocketTask(with: url)
        socket.resume()
        let timeout = Task {
            try await Task.sleep(for: .seconds(20))
            socket.cancel(with: .goingAway, reason: nil)
        }
        defer { timeout.cancel(); socket.cancel(with: .goingAway, reason: nil) }
        func receive() async throws -> [String: Any] {
            let data: Data
            switch try await socket.receive() {
            case .string(let value): data = Data(value.utf8)
            case .data(let value): data = value
            @unknown default: throw URLError(.cannotDecodeContentData)
            }
            guard let value = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { throw URLError(.cannotDecodeContentData) }
            return value
        }
        func send(_ value: [String: Any]) async throws {
            try await socket.send(.string(String(decoding: JSONSerialization.data(withJSONObject: value), as: UTF8.self)))
        }
        guard try await receive()["type"] as? String == "auth_required" else { throw URLError(.userAuthenticationRequired) }
        try await send(["type": "auth", "access_token": token])
        guard try await receive()["type"] as? String == "auth_ok" else { throw URLError(.userAuthenticationRequired) }
        var command = payload
        command["id"] = 1
        try await send(command)
        while true {
            let response = try await receive()
            guard response["id"] as? Int == 1 else { continue }
            guard response["success"] as? Bool == true else {
                let error = response["error"] as? [String: Any]
                throw NSError(domain: "CallWebhook.HA", code: 1, userInfo: [NSLocalizedDescriptionKey: error?["message"] as? String ?? "Home Assistant hat den Auftrag abgelehnt"])
            }
            return response["result"] ?? NSNull()
        }
    }

    static func ensureCallHelper(base: URL) async throws -> String {
        let defaults = UserDefaults.standard
        let existing = defaults.string(forKey: "haCallEntityID") ?? "input_boolean.iphone_call_active"
        let (data, status) = try await request(base: base, path: "api/states/\(existing)")
        if status == 200 {
            guard let state = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  ["on", "off"].contains(state["state"] as? String ?? "") else {
                throw NSError(domain: "CallWebhook.HA", code: 2, userInfo: [NSLocalizedDescriptionKey: "Anrufstatus-Helfer ist nicht verfügbar"])
            }
            defaults.set(existing, forKey: "haCallEntityID")
            return existing
        }
        guard status == 404 else { throw URLError(.badServerResponse) }
        // Use the supported helper API: creates a persistent helper, not a temporary REST state.
        let helpers = try await command(base: base, payload: ["type": "input_boolean/list"]) as? [[String: Any]] ?? []
        var helperID = helpers.first { $0["name"] as? String == "iphone_call_active" }?["id"] as? String
        if helperID == nil {
            let created = try await command(base: base, payload: ["type": "input_boolean/create", "name": "iphone_call_active", "icon": "mdi:phone-in-talk"]) as? [String: Any]
            helperID = created?["id"] as? String
        }
        guard let helperID else { throw URLError(.cannotParseResponse) }
        let registry = try await command(base: base, payload: ["type": "config/entity_registry/list"]) as? [[String: Any]] ?? []
        guard let entity = registry.first(where: { $0["platform"] as? String == "input_boolean" && $0["unique_id"] as? String == helperID }),
              let entityID = entity["entity_id"] as? String else { throw URLError(.cannotParseResponse) }
        let (_, verified) = try await request(base: base, path: "api/states/\(entityID)")
        guard verified == 200 else { throw URLError(.badServerResponse) }
        defaults.set(entityID, forKey: "haCallEntityID")
        return entityID
    }
}
