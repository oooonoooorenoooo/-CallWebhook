import Foundation

@main
struct SetupAssignmentsTests {
    static func main() {
        let numbers = ["93021641", "93021642", "93021640"]
        let mailboxes = [1, 2, 0]
        var confirmed: [Int: String] = [:]
        func ready(_ numbers: [String], _ boxes: [Int]) -> Bool {
            SetupAssignments.allConfirmed(numbers: numbers, mailboxes: boxes, confirmations: confirmed)
        }
        precondition(!ready(numbers, mailboxes), "Recognized defaults must not start the bootstrap")
        for index in numbers.indices {
            precondition(!ready(numbers, mailboxes), "Every enabled line requires confirmation")
            confirmed[index + 1] = SetupAssignments.fingerprint(number: numbers[index], mailbox: mailboxes[index])
        }
        precondition(ready(numbers, mailboxes))
        precondition(!ready(["different", numbers[1], numbers[2]], mailboxes), "Changing the number invalidates confirmation")
        precondition(!ready(numbers, [0, 2, 0]), "Changing the mailbox invalidates confirmation")
        precondition(ready([numbers[0]], [mailboxes[0]]), "Disabled lines must not block a one-line setup")
        precondition(!ready([], []))
        precondition(!ready(numbers, [1]))
        confirmed[1] = SetupAssignments.fingerprint(number: numbers[0], mailbox: -2)
        precondition(!ready([numbers[0]], [-2]), "A missing mailbox cannot be confirmed")
        confirmed[1] = SetupAssignments.fingerprint(number: numbers[0], mailbox: -1)
        precondition(ready([numbers[0]], [-1]), "Explicitly opting out of a mailbox is supported")
        print("Setup assignment confirmations passed")
    }
}
