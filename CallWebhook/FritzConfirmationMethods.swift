import Foundation

struct FritzConfirmationMethods {
    let methods: [String]
    let phoneCode: String

    // TR-064 supplies the complete dial string (unlike WebGUI's numeric suffix).
    static func tr064(_ value: String) -> Self {
        let entries = value.lowercased().split(separator: ",").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
        var methods: [String] = []
        var phone = ""
        if entries.contains("button") { methods.append("button") }
        for entry in entries {
            let parts = entry.split(separator: ";", maxSplits: 1).map(String.init)
            if parts.count == 2, parts[0] == "dtmf", parts[1].hasPrefix("*1"),
               (3...16).contains(parts[1].count), parts[1].allSatisfy({ "*#0123456789".contains($0) }) {
                phone = parts[1]
            }
        }
        if !phone.isEmpty { methods.append("phone") }
        return Self(methods: methods, phoneCode: phone)
    }
}
