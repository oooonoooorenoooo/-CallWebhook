import AVFoundation
import UIKit

/// Lets iOS blank the display and suppress touches while using the earpiece.
/// Keep this independent of scene activity: proximity blanking can make the
/// scene inactive, and must not immediately turn monitoring back off.
@MainActor
final class CallProximityController: NSObject {
    static let shared = CallProximityController()
    private var callInProgress = false

    private override init() {
        super.init()
        NotificationCenter.default.addObserver(
            self, selector: #selector(audioRouteChanged),
            name: AVAudioSession.routeChangeNotification, object: nil
        )
    }

    func setCallInProgress(_ inProgress: Bool) {
        guard callInProgress != inProgress else { return }
        callInProgress = inProgress
        refreshMonitoring()
    }

    /// Also reject a queued keypad action if the sensor already reports near.
    var isBlockingTouches: Bool {
        refreshMonitoring()
        return callInProgress && UIDevice.current.isProximityMonitoringEnabled
            && UIDevice.current.proximityState
    }

    @objc nonisolated private func audioRouteChanged(_ notification: Notification) {
        Task { @MainActor in self.refreshMonitoring() }
    }

    private func refreshMonitoring() {
        let outputs = AVAudioSession.sharedInstance().currentRoute.outputs
        let usingReceiver = !outputs.isEmpty && outputs.allSatisfy { $0.portType == .builtInReceiver }
        let enabled = callInProgress && usingReceiver
        if UIDevice.current.isProximityMonitoringEnabled != enabled {
            UIDevice.current.isProximityMonitoringEnabled = enabled
        }
    }
}
