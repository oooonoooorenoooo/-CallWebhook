import Foundation

// A CANCEL/487 alone is not evidence that another device answered.
enum MacCallOutcome {
    static func isAnsweredElsewhere(protocolName: String?, code: Int) -> Bool {
        switch protocolName?.uppercased() {
        case "Q.850": return code == 26
        case "SIP": return code == 200
        default: return false
        }
    }

    static func label(_ status: String) -> String {
        switch status {
        case "answered_elsewhere": return "Auf iPhone/anderem Gerät angenommen"
        case "missed": return "Verpasst"
        case "connected": return "Angenommen"
        case "declined": return "Abgelehnt"
        case "cancelled": return "Abgebrochen"
        case "failed": return "Fehlgeschlagen"
        case "interrupted": return "Unterbrochen"
        case "ringing": return "Klingelt"
        default: return status
        }
    }
}
