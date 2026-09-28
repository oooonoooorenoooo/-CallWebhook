import UIKit

@MainActor
enum SystemCellularDialer {
    static func call(_ number: String, completion: @escaping (Bool) -> Void) {
        var components = URLComponents()
        components.scheme = "telephony"
        components.path = number
        guard let url = components.url else { completion(false); return }
        // This bypasses tel: default-app dispatch. Let iOS choose the available
        // cellular/emergency route, with no dependence on SIP, HA or a saved SIM.
        UIApplication.shared.open(url, options: [:], completionHandler: completion)
    }
}
