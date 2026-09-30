import Foundation

// Only resolved telephone handles may initiate a call. Never turn a display
// name, an email address, or a list of recipients into an arbitrary number.
enum ExternalCallRequest {
    static func number(_ handles: [String]) -> String? {
        guard handles.count == 1 else { return nil }
        let value = handles[0].trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty, value.count <= 64,
              value.allSatisfy({ $0.isASCII && ($0.isNumber || "+ ()-/".contains($0)) }),
              SIPDialNumber.normalized(value) != nil else { return nil }
        return value
    }
}
