import Foundation

@main
struct IncomingDialplanTests {
    static func main() {
        let outgoing = "[from-callwebhook-ios]\nexten => _X.,1,Dial(PJSIP/${EXTEN}@fritz1-endpoint)\n"
        let updated = IncomingDialplan.addingIncomingRoute(to: outgoing)
        precondition(updated.hasPrefix(outgoing), "Preserve outgoing calls")
        precondition(updated.contains("[from-fritz]"), "FRITZ endpoints need their inbound context")
        precondition(updated.contains("${PJSIP_DIAL_CONTACTS(callwebhook-ios)}"), "Ring the registered iPhone contacts")
        precondition(updated.contains("exten => _.,1,Goto(s,1)"), "Accept named registration contacts, not only numeric destinations")
        precondition(IncomingDialplan.addingIncomingRoute(to: updated) == updated, "Repair is idempotent")
        let custom = "[from-fritz]\nexten => s,1,Hangup()\n"
        precondition(IncomingDialplan.addingIncomingRoute(to: custom) == custom, "Do not overwrite an existing inbound context")
        print("Incoming dialplan tests passed")
    }
}
