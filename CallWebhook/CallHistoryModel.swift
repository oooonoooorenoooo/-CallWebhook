import Foundation
import Combine
import LiveCommunicationKit
import UserNotifications

@MainActor
final class CallHistoryModel: ObservableObject {
    static let shared = CallHistoryModel()
    @Published private(set) var conversations: [CallRecord] = []
    @Published private(set) var errorMessage: String?
    @Published private(set) var unreadMissedCallCount = 0
    private let manager = ConversationHistoryManager.sharedInstance
    private let missedCalls = MissedCallState()
    private let systemCacheKey = "callwebhook.systemCallHistory.v1"
    private var systemCalls: [CallRecord] = []
    private var observation: AnyCancellable?
    private var refreshing = false
    private var requestingBadgePermission = false
    private var badgeTask: Task<Void, Never>?
    private var badgeRevision = 0

    private init() {
        // A PushKit cold start must retain unread system calls too, before the
        // foreground-only system history refresh can run again.
        systemCalls = UserDefaults.standard.data(forKey: systemCacheKey)
            .flatMap { try? JSONDecoder().decode([CallRecord].self, from: $0) } ?? []
        observation = LocalCallHistory.shared.$entries.sink { [weak self] entries in
            guard let self else { return }
            self.conversations = LocalCallHistory.merged(local: entries, system: self.systemCalls)
            self.updateMissedCallCount()
        }
    }

    func refresh() async {
        guard !refreshing else { return }
        refreshing = true
        defer { refreshing = false }
        do {
            let predicate = #Predicate<ConversationHistoryManager.RecentConversation> { _ in true }
            let recent = try await manager.recentConversations(matching: predicate)
            systemCalls = recent.map { call in
                CallRecord(id: call.id, date: call.date, number: call.handles.first?.value ?? "",
                           direction: String(describing: call.direction).lowercased(),
                           status: String(describing: call.status).lowercased(),
                           endedAt: call.date.addingTimeInterval(call.duration), reportedDuration: call.duration)
            }
            if let data = try? JSONEncoder().encode(systemCalls) {
                UserDefaults.standard.set(data, forKey: systemCacheKey)
            }
            errorMessage = nil
        } catch {
            // A denied/unavailable system history must never hide our own calls.
            errorMessage = "iOS-Anrufliste nicht verfügbar: \(error.localizedDescription)"
        }
        conversations = LocalCallHistory.merged(local: LocalCallHistory.shared.entries, system: systemCalls)
        updateMissedCallCount()
    }

    func markMissedCallsSeen(_ calls: [CallRecord]) {
        missedCalls.markSeen(calls)
        updateMissedCallCount()
    }

    func updateMissedCallCount() {
        unreadMissedCallCount = missedCalls.unreadCalls(in: conversations).count
        synchronizeIconBadge()
    }

    /// Called only while the main app is visible, never from a background push.
    func requestBadgeAuthorizationIfNeeded() async {
        guard !requestingBadgePermission else { return }
        requestingBadgePermission = true
        defer { requestingBadgePermission = false }
        let center = UNUserNotificationCenter.current()
        let settings = await center.notificationSettings()
        if settings.authorizationStatus == .notDetermined {
            do { _ = try await center.requestAuthorization(options: [.badge]) }
            catch { print("Anruf-Badge-Berechtigung: \(error.localizedDescription)") }
        }
        synchronizeIconBadge()
    }

    private func synchronizeIconBadge() {
        badgeRevision += 1
        guard badgeTask == nil else { return }
        // Serialize writes: clearing the list while an earlier badge update is
        // pending must always leave the newest count on the Home Screen.
        badgeTask = Task { @MainActor in
            defer { badgeTask = nil }
            while true {
                let revision = badgeRevision
                do { try await UNUserNotificationCenter.current().setBadgeCount(unreadMissedCallCount) }
                catch { print("Anruf-Badge: \(error.localizedDescription)") }
                if revision == badgeRevision { return }
            }
        }
    }
}
