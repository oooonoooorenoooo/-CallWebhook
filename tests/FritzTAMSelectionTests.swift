import Foundation

@main
struct FritzTAMSelectionTests {
    static func main() {
        // Existing landline mailbox stays assigned; mobile lines get new slots.
        precondition(FritzTAMSelection.resolved(selections: [-2, -2, 0], available: [0, 1, 2]) == [1, 2, 0])
        precondition(FritzTAMSelection.resolved(selections: [-2, -2, -2], available: [4, 0, 2]) == [0, 2, 4])
        precondition(FritzTAMSelection.resolved(selections: [4, -1, -1], available: [0]) == [0, -1, -1])
        precondition(FritzTAMSelection.resolved(selections: [2, 0, 4], available: [0, 2, 4]) == [2, 0, 4])
        precondition(FritzTAMSelection.resolved(selections: [-2, -2, -1], available: [0]) == [0, -2, -1])
        print("FRITZ TAM selection tests passed")
    }
}
