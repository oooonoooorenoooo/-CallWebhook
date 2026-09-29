import Foundation

enum SIPDialNumber {
    static func normalized(_ input: String) -> String? {
        let value = input.filter { !$0.isWhitespace && !"()-/".contains($0) }
        guard !value.isEmpty else { return nil }
        if value.hasPrefix("+") {
            let digits = value.dropFirst()
            guard !digits.isEmpty, digits.allSatisfy({ $0.isASCII && $0.isNumber }) else { return nil }
            return "00" + digits
        }
        guard value.allSatisfy({ $0.isASCII && ($0.isNumber || $0 == "*" || $0 == "#") }) else { return nil }
        return value
    }
}
