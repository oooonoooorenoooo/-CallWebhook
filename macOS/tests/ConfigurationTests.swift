import Foundation

@main
struct ConfigurationTests {
    static func main() throws {
        for host in ["asterisk.local", "192.168.1.10:5060", "[::1]:5060"] {
            var configuration = MacConfiguration()
            configuration.host = host
            try configuration.validate()
            precondition(configuration.username == "callwebhook-mac")
        }
        for host in ["", "user@host", "host;transport=tcp", "host/path", "host?query", "host\ncontext=evil", "host:0", "host:65536"] {
            var configuration = MacConfiguration()
            configuration.host = host
            do { try configuration.validate(); fatalError("Accepted invalid host: \(host)") }
            catch { }
        }
        var configuration = MacConfiguration()
        configuration.host = "asterisk.local"
        for base in ["https://ha.example", "http://homeassistant.local:8123"] {
            configuration.homeAssistant = base
            try configuration.validate()
        }
        for base in ["file:///tmp/ha", "https://user:secret@ha.example", "https://ha.example?token=secret"] {
            configuration.homeAssistant = base
            do { try configuration.validate(); fatalError("Accepted invalid HA URL") }
            catch { }
        }
        print("Mac configuration validation passed")
    }
}
