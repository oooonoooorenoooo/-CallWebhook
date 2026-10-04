import Foundation

@main
struct OutgoingDialplanTests {
    static func main() {
        for enabled in [false, true] {
            for prefixes in [("*82", "*83"), ("*92", "*93")] {
                let plan = OutgoingDialplan.make(easybellEnabled: enabled,
                    mobile1: "+491601234567", mobile2: "+491701234567",
                    line2Prefix: prefixes.0, line3Prefix: prefixes.1)
                let blocks = plan.components(separatedBy: "\n\n")
                precondition(blocks.count == 6)
                for index in [0, 2, 3, 4] {
                    precondition(!blocks[index].contains("easybell"))
                    precondition(!blocks[index].contains("CALLERID("))
                }
                precondition(blocks[0].contains("@fritz2-endpoint"))
                precondition(blocks[2].contains("@fritz3-endpoint"))
                precondition(blocks[3].contains("@fritz3-endpoint"))
                precondition(blocks[4].contains("@fritz1-endpoint"))
                if enabled {
                    precondition(blocks[1].contains("Set(CALLERID(num)=+491701234567)\n"))
                    precondition(blocks[5].contains("Set(CALLERID(num)=+491601234567)\n"))
                    precondition(blocks[1].contains("@easybell-endpoint"))
                    precondition(blocks[5].contains("@easybell-endpoint"))
                } else {
                    precondition(!plan.contains("easybell"))
                    precondition(!plan.contains("CALLERID("))
                    precondition(blocks[1].contains("@fritz2-endpoint"))
                    precondition(blocks[5].contains("@fritz1-endpoint"))
                }
                precondition(!plan.contains("\\n"))
                precondition(!plan.contains("\\("))
                precondition(blocks[1].contains("${EXTEN:\(prefixes.0.count)}"))
            }
        }
        print("Outgoing Easybell and FRITZ routing tests passed")
    }
}
