import Foundation

struct FritzSIPClient: Identifiable, Hashable {
    let index: Int
    let username: String
    let phoneName: String
    let outgoingNumber: String
    let internalNumber: String

    var id: Int { index }
    var displayName: String {
        let name = phoneName.isEmpty ? username : phoneName
        let number = outgoingNumber.isEmpty ? "" : " – \(outgoingNumber)"
        return "\(name)\(number)"
    }
}

enum FritzSIPReadiness {
    // The generated Asterisk configuration currently uses both base clients.
    // The direct FRITZ path additionally requires client 3 when enabled.
    static func missingClients(in clients: [FritzSIPClient], thirdLineEnabled: Bool) -> [String] {
        let required = thirdLineEnabled ? ["callwhapp1", "callwhapp2", "callwhapp3"] : ["callwhapp1", "callwhapp2"]
        return required.filter { name in
            !clients.contains { $0.username == name || $0.phoneName == name }
        }
    }
}
