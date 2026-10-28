# CallWebhook für macOS – ausschließlich Festnetzleitung 3

Eigenständiges SwiftUI-Projekt für macOS 13+, Apple Silicon und Intel. Die Dateien der iOS-App, deren XcodeGen-Projekt und die laufenden HA-/Push-Dienste bleiben unverändert. Nur die CI-Erkennung wurde ergänzt, damit reine Mac-Änderungen keinen neuen iPhone-TestFlight-Upload auslösen.

## Build

```sh
python3 macOS/bootstrap_sdk.py
xcodegen generate --spec macOS/project.yml
xcodebuild -project macOS/CallWebhookMac.xcodeproj -scheme CallWebhookMac \
  -configuration Debug -destination 'platform=macOS' \
  -derivedDataPath macOS/.build CODE_SIGNING_ALLOWED=NO build
```

Die App liegt danach unter `macOS/.build/Build/Products/Debug/CallWebhookMac.app`. Zum regulären Start in Xcode ein Development Team und automatische Signierung wählen. Für Verteilung außerhalb Xcode sind Developer-ID-Signierung und Notarisierung separat nötig. Keine Änderung an iOS-Signing oder Produktionszertifikaten.

Das Bootstrap-Skript lädt das offizielle [Linphone macOS SDK 5.4.128](https://download.linphone.org/releases/macos/sdk/spm/linphone-sdk-swift-macos-5.4.128/) mit festgehaltenen SHA-256-Prüfsummen. SwiftPM prüft die Binärarchive gegen das unveränderte offizielle Manifest. Die ignorierten `.sdk`- und `.build`-Verzeichnisse werden nicht eingecheckt. Das macOS-SDK ist ein anderes Paket als das iOS-SDK. Linphones Lizenzbedingungen gelten für die SDK-Nutzung; die mitgelieferte Lizenz steht nach Bootstrap in `.sdk/linphone/LICENSE.txt`.

## Asterisk: zunächst nur Konfiguration vorbereiten

Der Mac darf **niemals** `callwebhook-ios` verwenden: dessen AOR hat nur einen Kontakt und könnte die iPhone-Registrierung entfernen. Der neue Zugang heißt `callwebhook-mac`, hat ein eigenes starkes Passwort und einen eigenen Kontext `from-callwebhook-mac`. Dieser erlaubt nur normale Nummern und FRITZ-interne `**`-Nummern, jeweils über `fritz3-endpoint`. Keine Includes, Mobilfunkpräfixe oder Easybell-Routen.

1. Aktuelle, tatsächlich verwendete `pjsip.conf` und `extensions.conf` aus dem bestehenden Asterisk-Add-on sichern. Voraussetzung ist die im Repository vorhandene vollständige Push-Konfiguration mit `CW_LINE` und `callwebhook-push-header`.
2. Mit den gesicherten Kopien ausführen:

   ```sh
   python3 macOS/prepare_asterisk.py --pjsip backup/pjsip.conf \
     --extensions backup/extensions.conf --output prepared-mac
   ```

   Das neue Mac-Passwort verdeckt eingeben (24–128 Zeichen aus Buchstaben, Ziffern, `_`, `-`). Das Skript schreibt ausschließlich neue Kopien in ein neues Verzeichnis mit eingeschränkten Zugriffsrechten. Es ruft weder Home Assistant noch Asterisk auf, ersetzt keine Quelldateien und startet keinen Dienst neu.
3. Diff der Kopien prüfen. Die iPhone-SIP-Sektionen bleiben bytegleich erhalten. Eingehend wird erst nach dem bisherigen iPhone-Push-Handshake bei `CW_LINE=3` der registrierte Mac zu demselben Asterisk-`Dial()` hinzugefügt. Bei Leitung 1/2 bleibt das Ziel das iPhone. Leere Mac-/iPhone-Kontaktlisten werden berücksichtigt. Beantwortet ein Gerät, beendet Asterisk den anderen Anrufzweig. Die gemeinsame Anruf-ID und Leitung werden beiden Geräten als SIP-Header übergeben.
4. Erst in einem gesonderten Wartungsfenster die geprüften Dateien installieren und Asterisk neu laden. **Dieser Schritt ist nicht automatisiert und wurde hier nicht ausgeführt.** Für Rollback die gesicherten Originaldateien wiederherstellen und neu laden. iPhone-Neueinrichtung/Route-Reparatur kann die generierten Asterisk-Dateien überschreiben; danach Mac-Erweiterung erneut aus aktuellen Originalkopien vorbereiten. Nicht blind eine alte Gesamtkonfiguration zurückspielen.

Es ist kein neuer Push-Endpunkt nötig: der laufende Mac registriert direkt bei Asterisk. iPhone-Push-Token, Relay-Registrierung und bestehende Backend-APIs werden nicht verändert. Gemeinsames Klingeln setzt die installierte Serverergänzung und eine gültige iPhone-Konfiguration voraus; es beginnt nach dem bisherigen Push-Handshake (ggf. dessen Timeout).

## App einrichten

- Asterisk-Host mit optionalem Port und das neue Mac-SIP-Passwort eingeben. Für UDP-SIP/RTP Heimnetz oder VPN verwenden.
- Optional Home-Assistant-Basisadresse und HA-Zugriffstoken für die bestehende Mailbox-API eingeben. Geheimnisse landen im separaten macOS-Schlüsselbund, keine iCloud-/iPhone-Keychain-Gruppe. Leere Passwortfelder behalten gespeicherte Werte.
- Die technische, bei 0 beginnende FRITZ!Box-TAM-ID eintragen, die **ausschließlich Leitung 3** bedient. Die API liefert alle konfigurierten Mailboxen; der Mac zeigt nur diese TAM an. Bei gemeinsam genutzter TAM ist keine sichere Leitungstrennung möglich: dann vor Nutzung eine separate Leitung-3-TAM in der bestehenden Einrichtung zuweisen.
- Mikrofon-, Kontakt- und Benachrichtigungszugriff erlauben. Kontakte werden erst auf „Kontakte laden“ gelesen. Nummern können direkt, aus Kontakten oder per Rückruf gewählt werden.

Enthalten: ausgehende/eingehende SIP-Audiogespräche, Annehmen/Ablehnen/Auflegen, DTMF, macOS-Kontakte, persistenter lokaler Festnetzverlauf (max. 1000 Einträge), Mailboxliste und authentifizierte Audiowiedergabe, Klingelton und macOS-Banner. Klick auf ein Banner öffnet die App; angenommen wird dort. Audio wird ausschließlich über die konfigurierte HA-Instanz geladen, ohne Redirects und ohne externen URLs aus Mailboxdaten zu vertrauen. Mailbox-Löschen/Archivieren ist bewusst nicht Teil dieser ersten Mac-Oberfläche.

Eingehende Telefonie erfordert eine laufende App, einen wachen Mac und Netzwerk/VPN. Die App bleibt beim Schließen des Fensters aktiv; Cmd-Q beendet sie. Es gibt keinen macOS-Push-Wakeup im Ruhezustand und keine gemeinsame iOS-Systemanrufliste. Der Verlauf enthält die vom Mac beobachteten Anrufe; „am anderen Gerät angenommen“ wird bei entsprechender SIP-Signalisierung separat erfasst.

## Prüfung

```sh
python3 -m unittest discover -s macOS/tests -v
swiftc macOS/CallWebhookMac/MacConfiguration.swift macOS/tests/ConfigurationTests.swift -o /tmp/mac-config-tests
/tmp/mac-config-tests
```

Zusätzlich bestehen die 68 vorhandenen Python-Backend-Tests (mit PyYAML) und alle 16 Swift-Testprogramme aus dem bestehenden iOS-Workflow. Der unsigned Debug-Build wurde lokal unter Xcode 27.1 erfolgreich kompiliert. Die neue GitHub-Actions-Datei baut ausschließlich die Mac-App, ohne Deployment oder Zugriff auf Produktionsgeheimnisse.

Noch am echten System zu prüfen: Registrierung beider separaten Konten; ein-/ausgehend Leitung 3 mit Audio in beide Richtungen und DTMF; Leitung 1/2 klingelt nur am iPhone; Annahme auf jedem Gerät beendet das Klingeln des anderen; Ablehnen am Mac lässt das iPhone weiterklingeln; Mac offline verhindert iPhone-Annahme nicht; Mikrofon verweigert; VPN-/Netzverlust und Wake; Mailbox-TAM-Zuordnung und Audiowiedergabe. Es wurden keine produktiven Anrufe oder HA-Konfigurationsänderungen vorgenommen.
