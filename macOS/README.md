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

Eingehende Telefonie erfordert eine laufende App, einen wachen Mac und Netzwerk/VPN. Das Telefon-Symbol in der Menüleiste zeigt den Verbindungsstatus und bietet Öffnen, Annehmen/Auflegen, Hintergrundbetrieb und Beenden. „Bei Anmeldung im Hintergrund starten“ verwendet macOS SMAppService; bei einem Anmeldestart wird SIP ohne Fenster gestartet. Der Schalter zeigt den tatsächlichen macOS-Status einschließlich einer eventuell nötigen Freigabe in den Systemeinstellungen. Ein normaler manueller Start oder Klick auf das Dock-Symbol öffnet das Fenster. Der rote Schließen- und der gelbe Minimieren-Knopf blenden nur das Fenster aus. Dabei läuft derselbe SIP-Prozess ohne Dock-Fensterminiatur weiter. Über das Menüleistensymbol oder das Dock lässt sich das Fenster wieder öffnen. Nur „Beenden“ oder Cmd-Q beendet die App. Ein Hintergrundstart lässt sich ohne Ab-/Anmeldung mit `open -a /Applications/CallWebhookMac.app --args --background` nach vollständigem Beenden prüfen. Es gibt keinen macOS-Push-Wakeup im Ruhezustand und keine gemeinsame iOS-Systemanrufliste. Der Verlauf enthält die vom Mac beobachteten Anrufe; „am anderen Gerät angenommen“ wird bei entsprechender SIP-Signalisierung separat erfasst.

## Prüfung

```sh
python3 -m unittest discover -s macOS/tests -v
swiftc macOS/CallWebhookMac/MacConfiguration.swift macOS/tests/ConfigurationTests.swift -o /tmp/mac-config-tests
/tmp/mac-config-tests
```

Zusätzlich bestehen die 68 vorhandenen Python-Backend-Tests (mit PyYAML) und alle 16 Swift-Testprogramme aus dem bestehenden iOS-Workflow. Der unsigned Debug-Build wurde lokal unter Xcode 27.1 erfolgreich kompiliert. Die neue GitHub-Actions-Datei baut ausschließlich die Mac-App, ohne Deployment oder Zugriff auf Produktionsgeheimnisse.

Noch am echten System zu prüfen: Registrierung beider separaten Konten; ein-/ausgehend Leitung 3 mit Audio in beide Richtungen und DTMF; Leitung 1/2 klingelt nur am iPhone; Annahme auf jedem Gerät beendet das Klingeln des anderen; Ablehnen am Mac lässt das iPhone weiterklingeln; Mac offline verhindert iPhone-Annahme nicht; Mikrofon verweigert; VPN-/Netzverlust und Wake; Mailbox-TAM-Zuordnung und Audiowiedergabe. Es wurden keine produktiven Anrufe oder HA-Konfigurationsänderungen vorgenommen.


## Rufende und Klingellatenz (Korrektur nach Testanruf)

Der Mac verarbeitet Rufzustände direkt im SDK-Callback. Bei `End`/`Error` endet das Klingeln sofort; erst `Released` schreibt den endgültigen Verlauf. Als Annahme anderswo gelten der SDK-Status `AcceptedElsewhere`, `Reason: SIP;cause=200` oder `Reason: Q.850;cause=26` (auch in verketteten ErrorInfo-Daten). Ein CANCEL, SIP/487 oder Q.850/16 allein bleibt verpasst. Lokales Ablehnen hat Vorrang. Die Anzeige lautet „Auf iPhone/anderem Gerät angenommen“; das Signal identifiziert das annehmende Gerät nicht genauer. Bereits falsch gespeicherte Alteinträge werden ohne zuverlässige nachträgliche Signalisierung nicht umklassifiziert.

Die vorbereitete Serverkonfiguration verwendet ausdrücklich `Q(ANSWERED_ELSEWHERE)` (siehe [Asterisk Dial-Dokumentation](https://docs.asterisk.org/Latest_API/API_Documentation/Dialplan_Applications/Dial/)). Dies ist bereits Asterisks Standard beim Abbruch der übrigen Zweige nach Annahme. **Nicht** die Dial-Option `c` verwenden: Diese würde auch andere Abbrüche als anderswo angenommen markieren. Die Änderung wird nur in vorbereitete Kopien geschrieben; die Live-Telefonanlage wird nicht neu geladen.

Die Ringanzeige und der Ton werden vor der Verlaufsspeicherung ausgelöst. `CallTiming`-Logs enthalten Unix-Zeitpunkte, die gemeinsame Ruf-ID und die SIP-Call-ID, aber keine Rufnummern oder Zugangsdaten:

```sh
log stream --style compact --predicate 'subsystem == "de.reno.CallWebhookMac" AND category == "CallTiming"'
```

`incoming_received` markiert den SDK-Callback, **nicht** den Paketempfang. `ring_requested` markiert die Tonanforderung. `ring_view_appeared` markiert SwiftUIs onAppear bei offenem Fenster, nicht die physische Bildschirmausgabe; bei verborgenem Fenster fehlt dieser Eintrag erwartungsgemäß. `ring_stopped` und `terminal_reason` erlauben die Zuordnung zum CANCEL. Für eine echte Asterisk-INVITE-Messung muss dessen zeitgestempelter SIP-Trace anhand derselben Call-ID korreliert werden; die Rechneruhren müssen synchronisiert sein.

Der Server erzeugt den Mac-INVITE weiterhin erst **nach** dem bestehenden iPhone-Push-Handshake mit bis zu 15 Sekunden HTTP-Timeout. Diese Zeit liegt vor der hier gemessenen INVITE-zu-Anzeige-Strecke. Ein Test am realen Anschluss und eine Änderung dieser gemeinsamen Rufsteuerung wurden auf Wunsch nicht durchgeführt; die Ursache der dort beobachteten deutlichen Verzögerung ist damit noch nicht abschließend bestätigt.

### Reproduzierbarer lokaler SIP-Test

Der Test baut denselben `MacSIPClient` mit dem echten Linphone-SDK in einem separaten App-Bundle. Registrierung und Anrufe laufen ausschließlich über Loopback mit Dummy-Zugangsdaten. Ein eigenes Bundle und dessen eigener Verlauf vermeiden Eingriffe in produktive Einstellungen und Schlüsselbunddaten. Ein kleines Testfenster prüft SwiftUI-onAppear. Die Test-App wird nach jedem Fall beendet.

```sh
mkdir -p work/sip-regression
xcodegen generate --spec macOS/tests/SIPRegression/project.yml
xcodebuild -project macOS/tests/SIPRegression/SIPRegression.xcodeproj \
  -scheme SIPRegression -configuration Debug -destination 'platform=macOS' \
  -derivedDataPath work/ring-build CODE_SIGNING_ALLOWED=NO build
python3 macOS/tests/SIPRegression/run.py
```

Abgedeckt: Q.850/26 und SIP/200 → anderswo angenommen; CANCEL ohne Reason, Q.850/16 und SIP/487 → verpasst; lokales Ablehnen → abgelehnt. Jeder Fall prüft das Ende des Klingelzustands. Die JSON-Ergebnisse landen unter `work/sip-regression/results.json`. Beim lokalen Lauf nach der Korrektur lag INVITE-Versand bis SwiftUI-onAppear bei 25–72 ms und CANCEL-Versand bis Ende des Klingelzustands bei 41–55 ms. Dies sind Loopback-Messungen mit Testfenster, keine Messung am produktiven Asterisk und kein garantierter Grenzwert. Der macOS-Debug-Build mit bestehender Entwicklungssignatur war erfolgreich; die korrigierte App wurde auf dem Mac mini installiert. Die iPhone-App bleibt unverändert.
