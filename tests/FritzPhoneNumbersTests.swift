import Foundation

@main
struct FritzPhoneNumbersTests {
    static func main() {
        precondition(!FritzPhoneNumbers.tamResponds(to: "03012345", configured: "3"))
        precondition(!FritzPhoneNumbers.tamResponds(to: "3", configured: ""))
        precondition(FritzPhoneNumbers.tamResponds(to: "03012345", configured: "030 12345,03099999"))
        precondition(FritzPhoneNumbers.tamResponds(to: "03012345", configured: ""))
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
        print("FRITZ!Box number parsing: 7 checks passed")
    }
}
