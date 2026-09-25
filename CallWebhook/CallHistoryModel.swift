import Foundation
import LiveCommunicationKit

@MainActor
final class CallHistoryModel: ObservableObject {
    @Published private(set) var conversations: [ConversationHistoryManager.RecentConversation] = []
    @Published private(set) var errorMessage: String?

    private let manager = ConversationHistoryManager.sharedInstance

    func refresh() async {
        do {
            let predicate = #Predicate<ConversationHistoryManager.RecentConversation> { _ in true }
            conversations = try await manager
                .recentConversations(matching: predicate)
                .sorted { $0.date > $1.date }
            errorMessage = nil
        } catch {
            conversations = []
            errorMessage = error.localizedDescription
        }
    }
}
