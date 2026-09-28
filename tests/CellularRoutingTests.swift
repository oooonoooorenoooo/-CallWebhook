import Foundation

@main
struct CellularRoutingTests {
    static func main() {
        for number in CellularRouting.knownEmergencyNumbers {
            precondition(CellularRouting.cellularOnlyNumber(number) == number)
        }
        for (input, expected) in [(" 1 1 2 ", "112"), ("tel:112", "112"), ("tel:%31%31%30", "110"),
                                  ("#31#112", "112"), ("*31#110", "110"), ("112,123", "112"),
                                  ("112;123", "112"), ("155", "155"), ("116 117", "116117")] {
            precondition(CellularRouting.cellularOnlyNumber(input) == expected)
        }
        for input in ["", "1", "1120", "+49112", "030112", "01711234567", "*82112", "**621", "**21*+493012345678#", "sip:112@example.com"] {
            precondition(CellularRouting.cellularOnlyNumber(input) == nil)
        }
        print("Emergency and short-number routing passed; no real calls were placed")
    }
}
