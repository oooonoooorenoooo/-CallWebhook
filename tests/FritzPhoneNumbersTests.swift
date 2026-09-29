import Foundation

@main
struct FritzPhoneNumbersTests {
    static func main() {
        precondition(!FritzPhoneNumbers.tamResponds(to: "03012345", configured: "3"))
        precondition(!FritzPhoneNumbers.tamResponds(to: "3", configured: ""))
        precondition(!FritzPhoneNumbers.tamResponds(to: "03012345", configured: "030 12345,03099999"))
        precondition(FritzPhoneNumbers.tamResponds(to: "03012345", configured: "030 12345"))
        precondition(FritzPhoneNumbers.tamMatches(numbers: ["03012345", "03099999"], configured: "03099999,03012345"))
        precondition(!FritzPhoneNumbers.tamMatches(numbers: ["03012345", "03099999"], configured: "03012345"))
        precondition(!FritzPhoneNumbers.tamMatches(numbers: [], configured: ""))
        precondition(!FritzPhoneNumbers.tamResponds(to: "03012345", configured: ""))
        precondition(!FritzPhoneNumbers.tamResponds(to: "03012345", configured: "03012345,"))
        let assignment = "<List><Item><Number>03012345</Number><Type>eVoIP</Type><Index>2</Index><Name>Festnetz</Name></Item></List>"
        let assignments = FritzPhoneNumbers.incomingAssignments(from: assignment)
        precondition(assignments["03012345"] == assignment)
        let wrapped = assignment.replacingOccurrences(of: "<", with: "&lt;").replacingOccurrences(of: ">", with: "&gt;")
        precondition(FritzPhoneNumbers.incomingAssignments(from: "<Envelope><NewNumberList>\(wrapped)</NewNumberList></Envelope>") == assignments)
        precondition(FritzPhoneNumbers.clientMatches(number: "03012345", outgoing: "03012345", incoming: assignment))
        precondition(FritzPhoneNumbers.clientMatches(number: "03012345", outgoing: "03012345", incoming: "<Envelope><NewX_AVM-DE_InComingNumbers>\(wrapped)</NewX_AVM-DE_InComingNumbers></Envelope>"))
        precondition(!FritzPhoneNumbers.clientMatches(number: "03012345", outgoing: "3", incoming: assignment))
        precondition(!FritzPhoneNumbers.clientMatches(number: "03012345", outgoing: "03012345", incoming: "03012345"))
        precondition(FritzPhoneNumbers.incomingAssignments(from: "<List><Item><Number>3</Number><Type>eVoIP</Type><Index>2</Index></Item></List>").isEmpty)
        let numbers = ["030111111", "030222222", "+4930333333", "030444444"]
        let list = "<List>" + numbers.map { "<Item><Number>\($0)</Number><Index>0</Index></Item>" }.joined() + "</List>"
        let escaped = list.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;").replacingOccurrences(of: ">", with: "&gt;")
        precondition(FritzPhoneNumbers.parse("<Envelope><NewNumberList>\(escaped)</NewNumberList></Envelope>") == numbers)
        precondition(FritzPhoneNumbers.parse("<Envelope><NewNumberList><![CDATA[\(list)]]></NewNumberList></Envelope>") == numbers)
        precondition(FritzPhoneNumbers.parse(list) == numbers)
        precondition(FritzPhoneNumbers.parse("<NewExistingVoIPNumbers>3</NewExistingVoIPNumbers>").isEmpty)
        precondition(FritzPhoneNumbers.parse("<AccountList><Account><VoIPAccountIndex>3</VoIPAccountIndex><VoIPNumber>030111111</VoIPNumber></Account></AccountList>") == ["030111111"])
        precondition(FritzPhoneNumbers.parse("<List><Number>030111111</Number><Number>030111111</Number><Number> </Number></List>") == ["030111111"])
        precondition(FritzPhoneNumbers.parse("<broken>").isEmpty)
        print("FRITZ!Box number parsing and verified client assignment checks passed")
    }
}
