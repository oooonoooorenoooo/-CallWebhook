import Foundation

@main
struct HATokenRefreshTests {
    @MainActor static func main() async throws {
        precondition(HATokenLifetime.needsRefresh(expiry: "999", now: 1000))
        precondition(HATokenLifetime.needsRefresh(expiry: "1060", now: 1000))
        precondition(!HATokenLifetime.needsRefresh(expiry: "1061", now: 1000))
        precondition(HATokenLifetime.needsRefresh(expiry: nil, now: 1000))
        precondition(HATokenLifetime.needsRefresh(expiry: "nan", now: 1000))
        let gate = HATokenRefreshGate()
        var count = 0
        let operation: @MainActor () async throws -> String = {
            count += 1
            try await Task.sleep(for: .milliseconds(30))
            return "renewed"
        }
        async let first = gate.run(operation)
        async let second = gate.run(operation)
        async let third = gate.run(operation)
        let tokens = try await [first, second, third]
        precondition(tokens == ["renewed", "renewed", "renewed"])
        precondition(count == 1, "Parallel HA requests must share a refresh")
        enum Failure: Error { case offline }
        do {
            _ = try await gate.run { throw Failure.offline }
            preconditionFailure("Refresh failure must reach callers")
        } catch Failure.offline {}
        let recovered = try await gate.run(operation)
        precondition(recovered == "renewed" && count == 2, "A failed refresh must not poison later retries")
        print("HA expiry checks and concurrent refresh passed")
    }
}
