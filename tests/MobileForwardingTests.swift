import Foundation

@main
struct MobileForwardingTests {
    static func main() {
        precondition(MobileForwarding.activationCode(mobile: "0171 1234567", destination: "030 12345678", provider: "Telekom") == "**21*+493012345678#")
        precondition(MobileForwarding.activationCode(mobile: "+4917612345678", destination: "0049 40 1234567", provider: "o2") == "**21*+49401234567#")
        for target in ["3", "12345678", "030123#**21", "+49+30123456", "01711234567", "09001234567", "0033123456789"] {
            precondition(MobileForwarding.activationCode(mobile: "01711234567", destination: target, provider: "Vodafone") == nil)
        }
        precondition(MobileForwarding.activationCode(mobile: "03012345678", destination: "04012345678", provider: "o2") == nil)
        precondition(MobileForwarding.activationCode(mobile: "01711234567", destination: "03012345678", provider: " ") == nil)
        precondition(MobileForwarding.destination(number: "12345678", areaCode: "030") == "03012345678")
        precondition(MobileForwarding.destination(number: "04012345678", areaCode: "030") == "04012345678")
        precondition(MobileForwarding.isNetworkCode("**21*+493012345678#"))
        precondition(MobileForwarding.isNetworkCode("*#21#"))
        precondition(!MobileForwarding.isNetworkCode("*8203012345678"))
        precondition(!MobileProvider.matching("ALDI").isEmpty)
        precondition(!MobileProvider.matching("telekom").isEmpty)
        precondition(Set(MobileProvider.all.map(\.id)).count == MobileProvider.all.count)
        print("Mobile forwarding: normalization, MMI injection, SIM-independent source and provider search passed")
    }
}
