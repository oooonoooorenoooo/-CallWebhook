import Contacts
import Combine
import Foundation

// Keep contact lookup off the UI thread and load once per refresh, not per row.
private actor CallContactReader {
    func read() async -> [String: String] {
        let store = CNContactStore()
        if CNContactStore.authorizationStatus(for: .contacts) == .notDetermined {
            guard (try? await store.requestAccess(for: .contacts)) == true else { return [:] }
        }
        let status = CNContactStore.authorizationStatus(for: .contacts)
        guard status == .authorized || status == .limited else { return [:] }
        let request = CNContactFetchRequest(keysToFetch: [
            CNContactFormatter.descriptorForRequiredKeys(for: .fullName),
            CNContactOrganizationNameKey as CNKeyDescriptor,
            CNContactPhoneNumbersKey as CNKeyDescriptor
        ])
        request.sortOrder = .userDefault
        var names: [String: String] = [:]
        do {
            try store.enumerateContacts(with: request) { contact, _ in
                let fullName = CNContactFormatter.string(from: contact, style: .fullName)?
                    .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
                let name = fullName.isEmpty ? contact.organizationName : fullName
                guard !name.isEmpty else { return }
                for phone in contact.phoneNumbers {
                    guard let key = CallContactNumber.key(phone.value.stringValue) else { continue }
                    if names[key] == nil { names[key] = name }
                }
            }
            return names
        } catch { return [:] }
    }
}

enum CallContactNumber {
    static func key(_ value: String) -> String? {
        guard let number = SIPDialNumber.normalized(value),
              number.allSatisfy({ $0.isASCII && $0.isNumber }) else { return nil }
        // German domestic and international notation must identify the same number.
        // Never match only a suffix: unrelated subscribers can share those digits.
        if number.hasPrefix("0049") { return "0" + number.dropFirst(4) }
        return number
    }
}

@MainActor
final class CallContactNames: ObservableObject {
    @Published private var names: [String: String] = [:]
    private let reader = CallContactReader()
    private var loading = false

    func name(for number: String) -> String? {
        guard let key = CallContactNumber.key(number) else { return nil }
        return names[key]
    }

    func refresh() async {
        guard !loading else { return }
        loading = true
        names = await reader.read()
        loading = false
    }
}
