import AVFoundation
import CallKit

@MainActor
final class IncomingCallProvider: NSObject, CXProviderDelegate {
    static let shared = IncomingCallProvider()
    private let provider: CXProvider
    private let controller = CXCallController()
    private var callID: UUID?
    private var waitingForSIP = false
    private var finished: [UUID: Date] = [:]
    var currentCallID: UUID? { callID }
    var hasCall: Bool { callID != nil }

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

    func isWaiting(for id: UUID) -> Bool { callID == id && waitingForSIP }

    func report(caller: String, id: UUID = UUID(), line: Int? = nil, completion: @escaping () -> Void = {}) {
        if callID == id {
            // Every VoIP delivery must reach CallKit, even a duplicate APNs delivery.
            // CallKit rejects the existing UUID; keep the already active call intact.
            let duplicate = CXCallUpdate()
            duplicate.remoteHandle = CXHandle(type: .generic, value: caller)
            provider.reportNewIncomingCall(with: id, update: duplicate) { _ in completion() }
            return
        }
        let busy = callID != nil || finished[id] != nil
        LocalCallHistory.shared.begin(id: id, number: caller, incoming: true, line: line)
        if !busy { callID = id; waitingForSIP = true }
        let update = CXCallUpdate()
        update.remoteHandle = CXHandle(type: .generic, value: caller)
        update.hasVideo = false
        update.supportsHolding = false
        update.supportsGrouping = false
        update.supportsUngrouping = false
        provider.reportNewIncomingCall(with: id, update: update) { error in
            // Always complete PushKit after reporting, including failed/stale calls.
            completion()
            Task { @MainActor in
                if busy {
                    LocalCallHistory.shared.end(id, reason: "declined")
                    if error == nil { self.provider.reportCall(with: id, endedAt: Date(), reason: .failed) }
                    VoIPPushService.shared.finish(id)
                } else if let error, self.callID == id {
                    LocalCallHistory.shared.end(id, reason: "failed")
                    self.callID = nil
                    self.waitingForSIP = false
                    VoIPPushService.shared.finish(id)
                    SIPService.shared.incomingPresentationFailed(error)
                }
            }
        }
    }

    // Push and SIP carry the same ID. Declined/expired calls must not ring again.
    func attachSIP(caller: String, id: UUID?, line: Int? = nil) -> Bool {
        finished = finished.filter { Date().timeIntervalSince($0.value) < 120 }
        if let id, finished[id] != nil { return false }
        if let current = callID {
            guard id == current else { return false }
            LocalCallHistory.shared.setLine(current, line: line)
            waitingForSIP = false
            return true
        }
        report(caller: caller, id: id ?? UUID(), line: line)
        waitingForSIP = false
        return true
    }

    func answer() {
        guard let callID else { return }
        controller.request(CXTransaction(action: CXAnswerCallAction(call: callID))) { error in
            if let error { Task { @MainActor in SIPService.shared.incomingPresentationFailed(error) } }
        }
    }

    func end(id: UUID, failed: Bool = false) {
        guard callID == id else { return }
        ended(failed: failed)
    }

    func ended(failed: Bool = false) {
        guard let id = callID else { return }
        LocalCallHistory.shared.end(id, reason: failed ? "failed" : nil)
        finished[id] = Date()
        callID = nil
        waitingForSIP = false
        VoIPPushService.shared.finish(id)
        provider.reportCall(with: id, endedAt: Date(), reason: failed ? .failed : .remoteEnded)
    }

    nonisolated func providerDidReset(_ provider: CXProvider) {
        Task { @MainActor in self.ended(failed: true); SIPService.shared.hangup() }
    }
    nonisolated func provider(_ provider: CXProvider, perform action: CXAnswerCallAction) {
        Task { @MainActor in
            do {
                // The user may answer while the awakened app is still registering.
                let deadline = Date().addingTimeInterval(12)
                while self.isWaiting(for: action.callUUID) && Date() < deadline {
                    try await Task.sleep(for: .milliseconds(100))
                }
                guard self.callID == action.callUUID, !self.waitingForSIP else {
                    action.fail(); self.end(id: action.callUUID, failed: true); return
                }
                let allowed = await withCheckedContinuation { continuation in
                    AVAudioApplication.requestRecordPermission { continuation.resume(returning: $0) }
                }
                guard allowed, self.callID == action.callUUID else {
                    action.fail(); SIPService.shared.hangup(); return
                }
                try SIPService.shared.answerIncoming()
                action.fulfill()
            } catch {
                action.fail()
                SIPService.shared.incomingPresentationFailed(error)
            }
        }
    }
    nonisolated func provider(_ provider: CXProvider, perform action: CXEndCallAction) {
        Task { @MainActor in
            guard self.callID == action.callUUID else { action.fail(); return }
            LocalCallHistory.shared.end(action.callUUID, reason: "declined")
            self.finished[action.callUUID] = Date()
            self.callID = nil
            self.waitingForSIP = false
            VoIPPushService.shared.finish(action.callUUID)
            SIPService.shared.hangup()
            action.fulfill()
        }
    }
    nonisolated func provider(_ provider: CXProvider, timedOutPerforming action: CXAction) {
        Task { @MainActor in self.ended(failed: true); SIPService.shared.hangup() }
    }
    nonisolated func provider(_ provider: CXProvider, didActivate audioSession: AVAudioSession) {
        Task { @MainActor in SIPService.shared.activateCallAudio(true) }
    }
    nonisolated func provider(_ provider: CXProvider, didDeactivate audioSession: AVAudioSession) {
        Task { @MainActor in SIPService.shared.activateCallAudio(false) }
    }
}
