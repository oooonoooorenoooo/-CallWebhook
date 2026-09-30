import CarPlay
import Contacts
import Combine

@MainActor
final class CarPlaySceneDelegate: UIResponder, CPTemplateApplicationSceneDelegate {
    private var controller: CPInterfaceController?
    private var subscriptions = Set<AnyCancellable>()
    private let contacts = CPListTemplate(title: "Kontakte", sections: [])
    private let history = CPListTemplate(title: "Anrufe", sections: [])
    private let status = CPListTemplate(title: "Gespräch", sections: [])
    private var connected = false

    func templateApplicationScene(_ templateApplicationScene: CPTemplateApplicationScene, didConnect interfaceController: CPInterfaceController) {
        controller = interfaceController
        connected = true
        contacts.tabImage = UIImage(systemName: "person.crop.circle")
        history.tabImage = UIImage(systemName: "clock")
        status.tabImage = UIImage(systemName: "phone")
        interfaceController.setRootTemplate(CPTabBarTemplate(templates: [contacts, history, status]), animated: false, completion: nil)
        refreshContacts()
        LocalCallHistory.shared.$entries.sink { [weak self] entries in self?.refreshHistory(entries) }.store(in: &subscriptions)
        ExternalCallCoordinator.shared.dialer.$status.sink { [weak self] text in self?.refreshStatus(text) }.store(in: &subscriptions)
        SIPService.shared.$callStatus.sink { [weak self] text in
            if !text.isEmpty { self?.refreshStatus(text) }
        }.store(in: &subscriptions)
        if let activity = templateApplicationScene.userActivity { ExternalCallCoordinator.shared.continueCall(activity) }
    }

    func templateApplicationScene(_ templateApplicationScene: CPTemplateApplicationScene, didDisconnect interfaceController: CPInterfaceController) {
        connected = false
        subscriptions.removeAll()
        controller = nil
        // Disconnecting the car display must never hang up an ongoing call.
    }

    func scene(_ scene: UIScene, continue userActivity: NSUserActivity) {
        if !ExternalCallCoordinator.shared.continueCall(userActivity) { showError("Anruf konnte nicht gestartet werden. Einrichtung und Rufnummer auf dem iPhone prüfen.") }
    }

    private func refreshContacts() {
        let authorization = CNContactStore.authorizationStatus(for: .contacts)
        guard authorization == .authorized || authorization == .limited else {
            contacts.updateSections([CPListSection(items: [CPListItem(text: "Kontakte auf dem iPhone freigeben", detailText: "CallWebhook benötigt Zugriff auf die gewünschten Kontakte.")])]); return
        }
        // Address-book access can block; keep it off the CarPlay UI thread.
        Task {
            let rows = await Task.detached(priority: .userInitiated) { () -> [(String, String)] in
                let store = CNContactStore()
                let request = CNContactFetchRequest(keysToFetch: [CNContactFormatter.descriptorForRequiredKeys(for: .fullName), CNContactPhoneNumbersKey as CNKeyDescriptor])
                request.sortOrder = .userDefault
                var rows: [(String, String)] = []
                do {
                    try store.enumerateContacts(with: request) { contact, _ in
                        let name = CNContactFormatter.string(from: contact, style: .fullName) ?? "Kontakt"
                        for phone in contact.phoneNumbers {
                            if let number = ExternalCallRequest.number([phone.value.stringValue]) { rows.append((name, number)) }
                        }
                    }
                } catch { return [] }
                return rows
            }.value
            guard connected else { return }
            let items = rows.prefix(CPListTemplate.maximumItemCount).map { name, number in
                callItem(title: name, detail: number, number: number)
            }
            contacts.updateSections([CPListSection(items: items.isEmpty ? [CPListItem(text: "Keine freigegebenen Telefonnummern", detailText: "Kontakte auf dem iPhone prüfen.")] : items)])
        }
    }

    private func refreshHistory(_ entries: [CallRecord]) {
        let items = entries.filter { ExternalCallRequest.number([$0.number]) != nil }.prefix(min(50, CPListTemplate.maximumItemCount)).map { entry in
            callItem(title: entry.number, detail: "\(entry.lineLabel) · \(entry.date.formatted(date: .abbreviated, time: .shortened))", number: entry.number)
        }
        history.updateSections([CPListSection(items: items.isEmpty ? [CPListItem(text: "Noch keine Anrufe", detailText: nil)] : items)])
    }

    private func callItem(title: String, detail: String, number: String) -> CPListItem {
        let item = CPListItem(text: title, detailText: detail)
        item.handler = { [weak self] _, completion in
            self?.chooseLine(number)
            completion()
        }
        return item
    }

    private func chooseLine(_ number: String) {
        let defaults = UserDefaults.standard
        let lines = [1, 2, 3].filter { $0 == 1 || defaults.bool(forKey: "sipLine\($0)Enabled") }
        let actions = lines.map { line in
            CPAlertAction(title: line == 3 ? "Festnetz" : "SIM \(line)", style: .default) { [weak self] _ in
                self?.controller?.dismissTemplate(animated: true) { _, _ in
                    if let error = ExternalCallCoordinator.shared.start(number: number, line: line) { self?.showError(error) }
                }
            }
        } + [CPAlertAction(title: "Abbrechen", style: .cancel) { [weak self] _ in self?.controller?.dismissTemplate(animated: true, completion: nil) }]
        controller?.presentTemplate(CPActionSheetTemplate(title: "Anrufen", message: number, actions: actions), animated: true, completion: nil)
    }

    private func refreshStatus(_ text: String) {
        let info = CPListItem(text: text, detailText: "CallWebhook")
        let hangup = CPListItem(text: "Auflegen", detailText: nil)
        hangup.handler = { _, completion in ExternalCallCoordinator.shared.dialer.hangup(); completion() }
        status.updateSections([CPListSection(items: [info, hangup])])
    }

    private func showError(_ text: String) {
        controller?.presentTemplate(CPAlertTemplate(titleVariants: [text], actions: [CPAlertAction(title: "OK", style: .default) { [weak self] _ in
            self?.controller?.dismissTemplate(animated: true, completion: nil)
        }]), animated: true, completion: nil)
    }
}
