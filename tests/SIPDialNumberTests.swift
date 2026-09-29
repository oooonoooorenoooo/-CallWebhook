import Foundation

@main
struct SIPDialNumberTests {
    static func main() {
        precondition(SIPDialNumber.normalized("+49 (30) 123-4567") == "0049301234567")
        precondition(SIPDialNumber.normalized("030 1234567") == "0301234567")
        precondition(SIPDialNumber.normalized("**620") == "**620")
        precondition(SIPDialNumber.normalized("+44 20 12345678") == "00442012345678")
        for input in ["", "+", "sip:test@example.com", "123@evil", "+49+30123", "123,456"] {
            precondition(SIPDialNumber.normalized(input) == nil)
        }
        print("SIP dial numbers match Asterisk patterns and reject invalid addresses")
    }
}
