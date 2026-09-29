import Foundation

@main
struct Tests {
    static func main() {
        let both = FritzConfirmationMethods.tr064("button,dtmf;*19876")
        precondition(both.methods == ["button", "phone"])
        precondition(both.phoneCode == "*19876", "Do not prefix the TR-064 code a second time")
        precondition(FritzConfirmationMethods.tr064("dtmf;*11234").methods == ["phone"])
        precondition(FritzConfirmationMethods.tr064("button").methods == ["button"])
        precondition(FritzConfirmationMethods.tr064("button,googleauth").methods == ["button"])
        for bad in ["dtmf", "dtmf;1234", "dtmf;*11234invalid", ""] {
            precondition(FritzConfirmationMethods.tr064(bad).methods.isEmpty)
        }
        print("FRITZ confirmation method tests passed")
    }
}
