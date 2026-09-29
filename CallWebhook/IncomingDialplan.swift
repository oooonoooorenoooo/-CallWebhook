import Foundation

enum IncomingDialplan {
    static func addingIncomingRoute(to original: String) -> String {
        guard !original.components(separatedBy: .newlines).contains(where: {
            $0.trimmingCharacters(in: .whitespaces) == "[from-fritz]"
        }) else { return original }
        return original + """


        [from-fritz]
        exten => s,1,NoOp(CallWebhook incoming call)
         same => n,Dial(${PJSIP_DIAL_CONTACTS(callwebhook-ios)},60)
         same => n,Hangup()
        exten => _.,1,Goto(s,1)

        """
    }
}
