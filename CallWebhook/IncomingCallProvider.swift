import AVFoundation
import CallKit

@MainActor
final class IncomingCallProvider: NSObject, CXProviderDelegate {
    static let shared = IncomingCallProvider()
    private let provider: CXProvider
    private let controller = CXCallController()
    private var callID: UUID?

    private override init() {
        let config = CXProviderConfiguration()
        config.supportsVideo = false
        config.maximumCallGroups = 1
        config.maximumCallsPerCallGroup = 1
        config.supportedHandleTypes = [.phoneNumber, .generic]
        provider = CXProvider(configuration: config)
        super.init()
        provider.setDelegate(self, queue: .main)
    }

    func report(caller: String) {
        guard callID == nil else { return }
        let id = UUID()
        callID = id
        let update = CXCallUpdate()
        update.remoteHandle = CXHandle(type: .generic, value: caller)
        update.hasVideo = false
        update.supportsHolding = false
        update.supportsGrouping = false
        update.supportsUngrouping = false
        provider.reportNewIncomingCall(with: id, update: update) { error in
            Task { @MainActor in
                if let error, self.callID == id {
                    self.callID = nil
                    SIPService.shared.incomingPresentationFailed(error)
                }
            }
        }
    }

    func answer() {
        guard let callID else { return }
        controller.request(CXTransaction(action: CXAnswerCallAction(call: callID))) { error in
            if let error { Task { @MainActor in SIPService.shared.incomingPresentationFailed(error) } }
        }
    }

    func ended(failed: Bool = false) {
        guard let id = callID else { return }
        callID = nil
        provider.reportCall(with: id, endedAt: Date(), reason: failed ? .failed : .remoteEnded)
    }

    nonisolated func providerDidReset(_ provider: CXProvider) {
        Task { @MainActor in self.callID = nil; SIPService.shared.hangup() }
    }
    nonisolated func provider(_ provider: CXProvider, perform action: CXAnswerCallAction) {
        Task { @MainActor in
            do { try SIPService.shared.answerIncoming(); action.fulfill() }
            catch { action.fail(); SIPService.shared.incomingPresentationFailed(error) }
        }
    }
    nonisolated func provider(_ provider: CXProvider, perform action: CXEndCallAction) {
        Task { @MainActor in self.callID = nil; SIPService.shared.hangup(); action.fulfill() }
    }
    nonisolated func provider(_ provider: CXProvider, didActivate audioSession: AVAudioSession) {
        Task { @MainActor in SIPService.shared.activateCallAudio(true) }
    }
    nonisolated func provider(_ provider: CXProvider, didDeactivate audioSession: AVAudioSession) {
        Task { @MainActor in SIPService.shared.activateCallAudio(false) }
    }
}
