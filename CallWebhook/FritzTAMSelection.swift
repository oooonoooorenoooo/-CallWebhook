import Foundation

enum FritzTAMSelection {
    /// Reserve existing selections first, then resolve pending/missing slots.
    /// -1 is an explicit opt-out; -2 requests an answering machine.
    static func resolved(selections: [Int], available: [Int]) -> [Int] {
        let existing = Set(available)
        let reserved = Set(selections.filter { existing.contains($0) })
        var unused = existing.subtracting(reserved).sorted()
        return selections.map { selection in
            if selection == -1 || existing.contains(selection) { return selection }
            guard !unused.isEmpty else { return -2 }
            return unused.removeFirst()
        }
    }
}
