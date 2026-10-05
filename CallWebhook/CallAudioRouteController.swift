import AVFoundation
import Combine
import Foundation

/// Changes only the current call's output override. CallKit/Linphone continue
/// to own audio-session activation, category and Bluetooth/CarPlay defaults.
@MainActor
final class CallAudioRouteController: NSObject, ObservableObject {
    static let shared = CallAudioRouteController()

    @Published private(set) var speakerEnabled = false
    @Published var errorMessage: String?
    private var callInProgress = false
    private var requestedSpeaker = false

    private override init() {
        super.init()
        NotificationCenter.default.addObserver(self, selector: #selector(routeChanged),
            name: AVAudioSession.routeChangeNotification, object: nil)
    }

    func setCallInProgress(_ enabled: Bool) {
        guard enabled != callInProgress else { return }
        callInProgress = enabled
        if !enabled {
            if requestedSpeaker {
                try? AVAudioSession.sharedInstance().overrideOutputAudioPort(.none)
            }
            requestedSpeaker = false
            errorMessage = nil
        }
        refreshRoute()
    }

    func toggleSpeaker() {
        guard callInProgress else { return }
        let enable = !speakerEnabled
        do {
            try AVAudioSession.sharedInstance().overrideOutputAudioPort(enable ? .speaker : .none)
            requestedSpeaker = enable
            errorMessage = nil
            refreshRoute()
        } catch {
            errorMessage = "Audioausgabe konnte nicht umgeschaltet werden: \(error.localizedDescription)"
        }
    }

    @objc nonisolated private func routeChanged(_ notification: Notification) {
        Task { @MainActor in self.refreshRoute() }
    }

    private func refreshRoute() {
        speakerEnabled = callInProgress && AVAudioSession.sharedInstance().currentRoute.outputs
            .contains { $0.portType == .builtInSpeaker }
    }
}
