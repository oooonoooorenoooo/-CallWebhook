import Foundation

struct MobileProvider: Identifiable, Hashable {
    let name: String
    var id: String { name }

    // Includes retail brands and legacy names so existing contracts remain searchable.
    // No network inference from a brand or phone prefix: number portability and tariffs vary.
    static let all: [MobileProvider] = [
        "1&1", "Telekom / MagentaMobil", "Vodafone / CallYa", "o2 / Telefónica",
        "ALDI TALK", "AY YILDIZ", "BILDconnect", "BILDmobil", "Blau", "congstar",
        "crash", "DeutschlandSIM", "discoPLUS", "discoTEL", "Dr. SIM", "Drillisch",
        "easybell", "EDEKA smart", "EDEKA mobil (Bestand)", "FONIC", "FONIC mobile",
        "fraenk", "freenet", "freenet FUNK", "freenet FLEX", "freenet Mobile",
        "FYVE", "galaxysim", "GMX FreePhone", "goood", "handyvertrag.de", "helloMobil",
        "HIGH", "hit mobile", "ja! mobil", "kaufland mobil", "klarmobil", "Lebara",
        "LIDL Connect", "Lycamobile", "maXXim", "Mega SIM", "mobilcom-debitel (Bestand)",
        "mobilfunk.de", "MTEL", "NettoKOM", "Netzclub", "NORMA Connect", "otelo",
        "PENNY mobil", "PremiumSIM", "REWE mobil", "Rossmann mobil", "SATURN Tarif",
        "MediaMarkt Tarif", "sim.de", "sim24", "simplytel", "SimDiscount", "SIMon mobile",
        "smartmobil.de", "sparSIM", "speedySIM", "SUPER SELECT", "Tchibo MOBIL",
        "Tele2", "Türk Telekom Mobile (Bestand)", "TürkeiSIM", "Unlimited Mobile",
        "Versatel", "WEB.DE", "winSIM", "EWE", "swb", "M-net", "NetCologne",
        "WOBCOM", "yourfone", "McSIM (Bestand)", "Phonex (Bestand)", "eteleon (Bestand)", "E-Plus / BASE (Bestand)", "ring (Bestand)",
        "sipgate / simquadrat"
    ].map { MobileProvider(name: $0) }.sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending }

    static func matching(_ query: String) -> [MobileProvider] {
        all.filter { query.isEmpty || $0.name.localizedStandardContains(query) }
    }
}

enum MobileForwarding {
    static func normalizedNumber(_ input: String) -> String? {
        let allowed = CharacterSet(charactersIn: "+0123456789 ()-/")
        guard input.unicodeScalars.allSatisfy({ allowed.contains($0) }) else { return nil }
        var number = input.filter { "+0123456789".contains($0) }
        if number.hasPrefix("0049") { number = "+49" + number.dropFirst(4) }
        else if number.hasPrefix("0") && !number.hasPrefix("00") { number = "+49" + number.dropFirst() }
        guard number.hasPrefix("+49"), (9...16).contains(number.count),
              number.dropFirst().allSatisfy({ $0.isASCII && $0.isNumber }),
              number.dropFirst(3).first != "0" else { return nil }
        return number
    }

    static func destination(number: String, areaCode: String) -> String {
        if number.hasPrefix("+") || number.hasPrefix("0") { return number }
        let prefix = areaCode.trimmingCharacters(in: .whitespacesAndNewlines)
        guard prefix.hasPrefix("0"), !prefix.hasPrefix("00"), prefix.allSatisfy({ $0.isASCII && $0.isNumber }) else { return number }
        return prefix + number
    }

    static func activationCode(mobile: String, destination: String, provider: String) -> String? {
        guard !provider.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              let source = normalizedNumber(mobile),
              source.hasPrefix("+4915") || source.hasPrefix("+4916") || source.hasPrefix("+4917"),
              let target = normalizedNumber(destination), source != target,
              !["+4915", "+4916", "+4917", "+4918", "+4919", "+49800", "+49900"].contains(where: { target.hasPrefix($0) }) else { return nil }
        // GSM unconditional diversion. The originating SIM determines the source number.
        return "**21*\(target)#"
    }

    static func isNetworkCode(_ number: String) -> Bool {
        number.hasSuffix("#") && (number.hasPrefix("*") || number.hasPrefix("#"))
    }
}
