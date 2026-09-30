import Foundation

/// Defaults are suggestions. Only explicit confirmations may start provisioning.
enum SetupAssignments {
    static func fingerprint(number: String, mailbox: Int) -> String {
        "\(number)|\(mailbox)"
    }

    static func allConfirmed(numbers: [String], mailboxes: [Int], confirmations: [Int: String]) -> Bool {
        guard !numbers.isEmpty, numbers.count == mailboxes.count else { return false }
        return numbers.indices.allSatisfy { index in
            !numbers[index].trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                && mailboxes[index] >= -1
                && confirmations[index + 1] == fingerprint(number: numbers[index], mailbox: mailboxes[index])
        }
    }
}
