import Foundation
import Combine
import ServiceManagement

@MainActor
final class MacLoginService: ObservableObject {
    @Published private(set) var requested = false
    @Published private(set) var needsApproval = false
    @Published private(set) var error: String?

    init() { refresh() }

    func refresh() {
        // Read the operating system's state, including changes made outside this app.
        let status = SMAppService.mainApp.status
        requested = status == .enabled || status == .requiresApproval
        needsApproval = status == .requiresApproval
    }

    func setEnabled(_ enabled: Bool) {
        do {
            if enabled {
                if SMAppService.mainApp.status != .enabled && SMAppService.mainApp.status != .requiresApproval {
                    try SMAppService.mainApp.register()
                }
            } else if SMAppService.mainApp.status != .notRegistered {
                try SMAppService.mainApp.unregister()
            }
            error = nil
        } catch {
            self.error = "Anmeldestart konnte nicht geändert werden: \(error.localizedDescription)"
        }
        refresh()
    }

    func openSystemSettings() { SMAppService.openSystemSettingsLoginItems() }
}
