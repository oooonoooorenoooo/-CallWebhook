import Foundation

// Public mobile-line calls use Easybell; internal and landline calls stay on FRITZ!Box.
enum OutgoingDialplan {
    static func make(easybellEnabled: Bool, mobile1: String, mobile2: String,
                     line2Prefix: String, line3Prefix: String) -> String {
        let prefix2 = line2Prefix.isEmpty ? "*82" : line2Prefix
        let prefix3 = line3Prefix.isEmpty ? "*83" : line3Prefix
        let publicLine1Endpoint = easybellEnabled ? "easybell-endpoint" : "fritz1-endpoint"
        let publicLine2Endpoint = easybellEnabled ? "easybell-endpoint" : "fritz2-endpoint"
        let line1CallerID = easybellEnabled ? " same => n,Set(CALLERID(num)=\(mobile1))\n" : ""
        let line2CallerID = easybellEnabled ? " same => n,Set(CALLERID(num)=\(mobile2))\n" : ""
        let dialplan = """
        [from-callwebhook-ios]
        exten => _\(prefix2)**X.,1,NoOp(CallWebhook Leitung 2 internal FRITZ call to ${EXTEN:\(prefix2.count)})
         same => n,Dial(PJSIP/${EXTEN:\(prefix2.count)}@fritz2-endpoint,60)
         same => n,Hangup()

        exten => _\(prefix2)X.,1,NoOp(CallWebhook Leitung 2 to ${EXTEN:\(prefix2.count)})
        \(line2CallerID) same => n,Dial(PJSIP/${EXTEN:\(prefix2.count)}@\(publicLine2Endpoint),60)
         same => n,Hangup()

        exten => _\(prefix3)**X.,1,NoOp(CallWebhook Leitung 3 internal FRITZ call to ${EXTEN:\(prefix3.count)})
         same => n,Dial(PJSIP/${EXTEN:\(prefix3.count)}@fritz3-endpoint,60)
         same => n,Hangup()

        exten => _\(prefix3)X.,1,NoOp(CallWebhook Leitung 3 to ${EXTEN:\(prefix3.count)})
         same => n,Dial(PJSIP/${EXTEN:\(prefix3.count)}@fritz3-endpoint,60)
         same => n,Hangup()

        exten => _**X.,1,NoOp(CallWebhook internal FRITZ call to ${EXTEN})
         same => n,Dial(PJSIP/${EXTEN}@fritz1-endpoint,60)
         same => n,Hangup()

        exten => _X.,1,NoOp(CallWebhook Leitung 1 to ${EXTEN})
        \(line1CallerID) same => n,Dial(PJSIP/${EXTEN}@\(publicLine1Endpoint),60)
         same => n,Hangup()
        """

        return dialplan
    }
}
