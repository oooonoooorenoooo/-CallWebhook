import Foundation
import Combine
import LiveCommunicationKit

@MainActor
final class CallHistoryModel: ObservableObject {
    @Published private(set) var conversations: [CallRecord] = []
    @Published private(set) var errorMessage: String?
    private let manager = ConversationHistoryManager.sharedInstance
    private var systemCalls: [CallRecord] = []
    private var observation: AnyCancellable?

    init() {
        observation = LocalCallHistory.shared.$entries.sink { [weak self] entries in
            guard let self else { return }
            self.conversations = LocalCallHistory.merged(local: entries, system: self.systemCalls)
        }
    }

    func refresh() async {
        do {
            let predicate = #Predicate<ConversationHistoryManager.RecentConversation> { _ in true }
            let recent = try await manager.recentConversations(matching: predicate)
            systemCalls = recent.map { call in
                CallRecord(id: call.id, date: call.date, number: call.handles.first?.value ?? "",
                           direction: String(describing: call.direction).lowercased(),
                           status: String(describing: call.status).lowercased(),
                           endedAt: call.date.addingTimeInterval(call.duration), reportedDuration: call.duration)
            }
            errorMessage = nil
        } catch {
            // A denied/unavailable system history must never hide our own calls.
            errorMessage = "iOS-Anrufliste nicht verfügbar: \(error.localizedDescription)"
        }
        conversations = LocalCallHistory.merged(local: LocalCallHistory.shared.entries, system: systemCalls)
    }
}
