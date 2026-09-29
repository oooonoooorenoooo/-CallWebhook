# CallWebhook Push-Dienst

Dieser Dienst wird **einmal vom App-Anbieter** betrieben. App-Nutzer brauchen
weder einen Apple-Account noch eine `.p8`-Datei. Der Einrichtungsassistent und
bereits eingerichtete Apps registrieren sich automatisch, sobald die Dienst-URL
im Build hinterlegt ist. Ohne diese URL zeigt die App den fehlenden Dienst an.
Ein noch nicht betriebener Dienst bedeutet: Hintergrund-Push funktioniert noch nicht.

## Einmalige Inbetriebnahme durch den Betreiber

Benötigt werden ein dauerhaft erreichbarer Docker-Server, ein DNS-Name mit
Port 80/443 und ein **APNs**-Schlüssel des Apple-Teams dieser App. Ein
App-Store-Connect-Uploadschlüssel ist kein APNs-Schlüssel. Die bestehende
Push-Notifications-Capability der App bleibt erhalten.

Auf dem Server, im geklonten Repository:

```sh
export PUSH_HOST=push.example.org
export APNS_TEAM_ID=ABCDEFGHIJ
export APNS_KEY_ID=KLMNOPQRST
export APNS_KEY_FILE=/srv/callwebhook-secrets/apns.p8
docker compose -f push_relay/compose.yaml up -d --build
```

`push.example.org` und die IDs sind Platzhalter. Der Schlüssel wird nur als
Docker-Secret eingebunden. Die Datei muss für UID 10001 im Container lesbar sein
(z. B. Eigentümer 10001 und Modus 0400). Nicht ins Repository kopieren.
Caddy stellt HTTPS bereit; der eigentliche Relay-Port ist nicht veröffentlicht.
Die SQLite-Datenbank liegt im persistenten Volume `relay_data`; Backups dieses
Volumes schützen vor unnötiger Neuregistrierung. Nur eine Relay-Instanz pro
Datenbank betreiben. Mit aktualisiertem Checkout erneut `up -d --build` ausführen.

Danach die GitHub-Repository-**Variable** `CALLWEBHOOK_PUSH_RELAY_URL` auf
`https://push.example.org` setzen und einen neuen App-Build erzeugen. Diese URL
ist öffentlich, kein Secret. Beide iOS-Builds übernehmen sie aus der Variable.
Home Assistant benötigt Backend API 8, installiert über CallWebhook Bootstrap.
Eine Endnutzer-Konfiguration des Dienstes ist nicht vorgesehen.

`GET /healthz` bestätigt lediglich, dass der Prozess bereit ist. Es bestätigt
keine APNs-Berechtigung und keinen zugestellten Anruf. Nach Inbetriebnahme einen
echten Anruf bei gesperrtem iPhone durchführen und Annehmen/Ablehnen testen.

## Registrierung und Grenzen

* Apple App Attest verifiziert Zertifikatskette, App-ID, Umgebung, Schlüssel,
  einmalige Challenge und Signaturzähler. Neue OS-Extensions werden ebenfalls
  geprüft. Die öffentliche Apple-Root-CA liegt im Repository; kein privater Key.
* Neue Anmeldungen verwenden Attestierungen, Tokenwechsel signierte Assertions.
  Alle Registrierungsparameter sind an den Apple-Nachweis gebunden.
* HA bekommt einen zufälligen Zugang nur für ein registriertes iPhone. Auf dem
  Relay liegt ausschließlich dessen Hash. HA speichert den Zugang automatisch
  in `/config/callwebhook/voip.json` mit Modus 0600. Die Datei enthält im Relay-Modus
  bei neuen Installationen **keinen Apple-Schlüssel**. Bestehende direkte
  APNs-Konfigurationen bleiben erhalten.
* Versand erlaubt ausschließlich Anruf-ID und Anruferanzeige, keine frei wählbaren
  Zielgeräte. Deduplizierung, zwölf Anrufe pro Minute und 120 pro Stunde begrenzen
  Fehlaufrufe. Registrierungs-Challenges verfallen nach zwei Minuten.
* Standardmäßig sind nur App-Store-/TestFlight-Produktion zugelassen. Separate
  Entwicklungsdienste können `ALLOW_DEVELOPMENT=1` setzen; der APNs-Schlüssel
  muss dann auch die Sandbox erlauben. Die App nutzt beim Development-Build
  Apples standardmäßige App-Attest-Entwicklungsumgebung, TestFlight Produktion.
* `APP_ID_PREFIX` kann bei vom Team abweichendem App-ID-Präfix explizit gesetzt
  werden. Der Bundle-Identifier ist fest auf diese App beschränkt.
* HTTP-Zugriffslogs sind deaktiviert. Anruferdaten werden nur für den sofortigen
  Versand verarbeitet, nicht in der Datenbank gespeichert. Geräte-Token und
  öffentliche App-Attest-Schlüssel werden für die Registrierung gespeichert.
* `DELETE /v1/registration` mit dem installationsbezogenen Bearer-Zugang widerruft
  diesen Zugang. Erneute App-Anmeldung kann eine neue Registrierung erstellen.
* Das aktuelle HA-/Asterisk-Modell unterstützt **ein iPhone je HA-Installation**.
  Mehrere unabhängige HA-Installationen sind voneinander getrennt.
* Der Dienst weckt das iPhone. Die SIP-/Audioverbindung benötigt weiterhin
  Heimnetz/VPN zu Asterisk. Der Dienst transportiert keine Gespräche.

Die App-Attest-Validierung folgt Apples Dokumentation:
https://developer.apple.com/documentation/devicecheck/validating-apps-that-connect-to-your-server
Root-Zertifikat:
https://www.apple.com/certificateauthority/Apple_App_Attestation_Root_CA.pem

Automatisierte Tests verwenden eigene kurzlebige Testzertifikate und
gemockten APNs-Versand. Ein echter App-Attest-/APNs-Durchlauf mit dem signierten
iPhone-Build bleibt nach Bereitstellung des Servers erforderlich.
