import Foundation

/// Reads actual phone-number fields, never account counts or account indexes.
enum FritzPhoneNumbers {
    static func tamResponds(to number: String, configured: String) -> Bool {
        let expected = number.filter { $0.isNumber }
        // A line index or account count is never a public telephone number.
        guard expected.count >= 3 else { return false }
        if configured.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return true }
        return configured.split(separator: ",").contains { $0.filter { $0.isNumber } == expected }
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
        private var stack: [(name: String, text: String)] = []

        func parser(_ parser: XMLParser, didStartElement elementName: String, namespaceURI: String?, qualifiedName qName: String?, attributes attributeDict: [String: String]) {
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
            if ["Number", "VoIPNumber", "NewVoIPNumber"].contains(element.name) {
                numbers.append(value)
            } else if ["NewNumberList", "NewX_AVM-DE_VoIPAccountList"].contains(element.name), !value.isEmpty {
                embeddedLists.append(value)
            }
        }
    }
}
