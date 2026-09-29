import Foundation

/// Reads actual phone-number fields, never account counts or account indexes.
enum FritzPhoneNumbers {
    static func clientMatches(number: String, outgoing: String, incoming: String) -> Bool {
        let expected = number.filter { $0.isNumber }
        guard expected.count >= 3, outgoing.filter({ $0.isNumber }) == expected else { return false }
        // Empty means all calls, which is not the selected one-number assignment.
        let numbers = parse(incoming).map { $0.filter { $0.isNumber } }
        return numbers == [expected]
    }

    static func incomingAssignments(from xml: String) -> [String: String] {
        let reader = NumberReader()
        let parser = XMLParser(data: Data(xml.utf8))
        parser.delegate = reader
        parser.shouldResolveExternalEntities = false
        guard parser.parse() else { return [:] }
        var items = reader.items
        for embedded in reader.embeddedLists {
            let inner = NumberReader()
            let parser = XMLParser(data: Data(embedded.utf8))
            parser.delegate = inner
            parser.shouldResolveExternalEntities = false
            if parser.parse() { items.append(contentsOf: inner.items) }
        }
        func escaped(_ value: String) -> String {
            value.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
                .replacingOccurrences(of: ">", with: "&gt;").replacingOccurrences(of: "\"", with: "&quot;")
        }
        var result: [String: String] = [:]
        for item in items {
            guard let number = item["Number"], number.filter({ $0.isNumber }).count >= 3,
                  let type = item["Type"], ["eVoIP", "eISDN", "ePOTS", "eGSM"].contains(type),
                  let index = item["Index"], let numericIndex = Int(index), numericIndex >= 0 else { continue }
            result[number] = "<List><Item><Number>\(escaped(number))</Number><Type>\(type)</Type><Index>\(numericIndex)</Index><Name>\(escaped(item["Name"] ?? ""))</Name></Item></List>"
        }
        return result
    }

    static func tamResponds(to number: String, configured: String) -> Bool {
        tamMatches(numbers: [number], configured: configured)
    }

    /// Exact per-mailbox assignment. Empty (all numbers), extra numbers and
    /// account indexes never confirm the selected line mapping.
    static func tamMatches(numbers: [String], configured: String) -> Bool {
        let expected = numbers.map { $0.filter { $0.isNumber } }
        guard !expected.isEmpty, expected.allSatisfy({ $0.count >= 3 }) else { return false }
        let actual = configured.split(separator: ",", omittingEmptySubsequences: false)
            .map { $0.filter { $0.isNumber } }
        guard actual.allSatisfy({ $0.count >= 3 }) else { return false }
        return Set(actual) == Set(expected)
    }

    static func parse(_ xml: String) -> [String] {
        let reader = NumberReader()
        let parser = XMLParser(data: Data(xml.utf8))
        parser.delegate = reader
        parser.shouldResolveExternalEntities = false
        guard parser.parse() else { return [] }
        var numbers = reader.numbers
        // FRITZ!OS returns the inner list as escaped XML or CDATA in SOAP.
        for list in reader.embeddedLists {
            let innerReader = NumberReader()
            let innerParser = XMLParser(data: Data(list.utf8))
            innerParser.delegate = innerReader
            innerParser.shouldResolveExternalEntities = false
            if innerParser.parse() { numbers.append(contentsOf: innerReader.numbers) }
        }
        var seen = Set<String>()
        return numbers.filter { !$0.isEmpty && seen.insert($0).inserted }
    }

    private final class NumberReader: NSObject, XMLParserDelegate {
        var numbers: [String] = []
        var embeddedLists: [String] = []
        var items: [[String: String]] = []
        private var item: [String: String] = [:]
        private var stack: [(name: String, text: String)] = []

        func parser(_ parser: XMLParser, didStartElement elementName: String, namespaceURI: String?, qualifiedName qName: String?, attributes attributeDict: [String: String]) {
            if elementName == "Item" { item = [:] }
            stack.append((elementName.split(separator: ":").last.map(String.init) ?? elementName, ""))
        }
        func parser(_ parser: XMLParser, foundCharacters string: String) {
            guard !stack.isEmpty else { return }
            stack[stack.count - 1].text += string
        }
        func parser(_ parser: XMLParser, foundCDATA CDATABlock: Data) {
            self.parser(parser, foundCharacters: String(decoding: CDATABlock, as: UTF8.self))
        }
        func parser(_ parser: XMLParser, didEndElement elementName: String, namespaceURI: String?, qualifiedName qName: String?) {
            guard let element = stack.popLast() else { return }
            let value = element.text.trimmingCharacters(in: .whitespacesAndNewlines)
            if stack.last?.name == "Item" { item[element.name] = value }
            if element.name == "Item" { items.append(item) }
            if ["Number", "VoIPNumber", "NewVoIPNumber"].contains(element.name) {
                numbers.append(value)
            } else if ["NewNumberList", "NewX_AVM-DE_VoIPAccountList", "NewX_AVM-DE_InComingNumbers"].contains(element.name), !value.isEmpty {
                embeddedLists.append(value)
            }
        }
    }
}
