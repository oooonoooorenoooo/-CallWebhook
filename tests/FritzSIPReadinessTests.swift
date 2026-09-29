import Foundation

@main
struct FritzSIPReadinessTests {
    static func main() {
        let existing = [
            FritzSIPClient(index: 4, username: "callwhapp1", phoneName: "Mobil 1", outgoingNumber: "1234567", internalNumber: "624"),
            FritzSIPClient(index: 7, username: "callwhapp2", phoneName: "Mobil 2", outgoingNumber: "+49307654321", internalNumber: "627")
        ]
        // Existing clients must pass without any provisioning event and regardless
        // of the outgoing-number representation supplied by the FRITZ!Box.
        precondition(FritzSIPReadiness.missingClients(in: existing, thirdLineEnabled: false).isEmpty)
        precondition(FritzSIPReadiness.missingClients(in: existing, thirdLineEnabled: true) == ["callwhapp3"])
        let third = FritzSIPClient(index: 9, username: "", phoneName: "callwhapp3", outgoingNumber: "", internalNumber: "629")
        precondition(FritzSIPReadiness.missingClients(in: existing + [third], thirdLineEnabled: true).isEmpty)
        precondition(FritzSIPReadiness.missingClients(in: [existing[0]], thirdLineEnabled: false) == ["callwhapp2"])
        precondition(FritzSIPReadiness.missingClients(in: [], thirdLineEnabled: false) == ["callwhapp1", "callwhapp2"])
        let unrelated = FritzSIPClient(index: 0, username: "other", phoneName: "Telephone", outgoingNumber: "1234567", internalNumber: "620")
        precondition(FritzSIPReadiness.missingClients(in: [unrelated, existing[1]], thirdLineEnabled: false) == ["callwhapp1"])
        print("Existing and missing FRITZ SIP client readiness passed")
    }
}
