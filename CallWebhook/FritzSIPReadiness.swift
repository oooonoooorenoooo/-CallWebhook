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
    // Require exactly the selected lines, including the third before provisioning.
    static func missingClients(in clients: [FritzSIPClient], secondLineEnabled: Bool = true, thirdLineEnabled: Bool) -> [String] {
        let required = ["callwhapp1"] + (secondLineEnabled ? ["callwhapp2"] : []) + (thirdLineEnabled ? ["callwhapp3"] : [])
        return required.filter { name in
            !clients.contains { $0.username == name || $0.phoneName == name }
        }
    }
}
