import Foundation

@main
struct ExternalCallRequestTests {
    static func main() {
        precondition(ExternalCallRequest.number(["+49 (30) 123-456"]) != nil)
        precondition(ExternalCallRequest.number(["112"]) == "112")
        precondition(ExternalCallRequest.number([]) == nil)
        precondition(ExternalCallRequest.number(["123", "456"]) == nil)
        for value in ["Alice 123", "alice@example.com", "*21*123#", "123;456", "+", "١٢٣"] {
            precondition(ExternalCallRequest.number([value]) == nil)
        }
        print("External call request tests passed")
    }
}
