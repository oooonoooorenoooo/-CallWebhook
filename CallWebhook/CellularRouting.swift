import Foundation

/// Pure routing policy; no network calls. Short public numbers must resolve at
/// the handset's location, never at the remote Asterisk/FRITZ!Box location.
enum CellularRouting {
    static let knownEmergencyNumbers: Set<String> = [
        "112", "110", "15", "17", "18", "117", "118", "122", "133", "144", "999", "911"
    ]

    static func cellularOnlyNumber(_ input: String) -> String? {
        var value = input.trimmingCharacters(in: .whitespacesAndNewlines)
        if value.lowercased().hasPrefix("tel:") { value = String(value.dropFirst(4)) }
        value = value.removingPercentEncoding ?? value
        value = value.filter { !$0.isWhitespace && !"()-./".contains($0) }
        for prefix in ["*31#", "#31#"] where value.hasPrefix(prefix) {
            value = String(value.dropFirst(prefix.count))
        }
        // Never forward post-dial digits to an emergency operator.
        let first = String(value.prefix { $0 != "," && $0 != ";" })
        guard first.allSatisfy({ $0.isASCII && $0.isNumber }) else { return nil }
        if knownEmergencyNumbers.contains(first) { return first }
        // Covers other national European short emergency numbers without relying
        // on a hard-coded country or an incomplete emergency-number catalog.
        if (2...3).contains(first.count) { return first }
        if ["116000", "116006", "116111", "116117", "116123"].contains(first) { return first }
        return nil
    }
}
