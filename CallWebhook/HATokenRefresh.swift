import Foundation

/// Keep expired credentials away from HA's failed-login/IP-ban middleware.
enum HATokenLifetime {
    static func needsRefresh(expiry: String?, now: TimeInterval = Date().timeIntervalSince1970) -> Bool {
        guard let expiry, let deadline = Double(expiry), deadline.isFinite else { return true }
        return deadline <= now + 60
    }
}

@MainActor
final class HATokenRefreshGate {
    private var pending: Task<String, Error>?

    func run(_ operation: @escaping @MainActor () async throws -> String) async throws -> String {
        if let pending { return try await pending.value }
        let task = Task { try await operation() }
        pending = task
        defer { pending = nil }
        return try await task.value
    }
}
