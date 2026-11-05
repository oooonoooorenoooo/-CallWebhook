# Einmalige Einrichtung auf dem HA-Pi des App-Betreibers

## ComatAlarm: BER-Abfragen auf dem Pi (ab 1.2.22)

Die Flugabfrage und die gemeinsame API-Statusprüfung senden dieselben
BER-Anfrageangaben wie die App: JSON-Antwortformat, den Verweis auf die
BER-Flugübersicht und eine ausdrücklich als Pi gekennzeichnete
ComatAlarm-Kennung. Flugnummern werden als `LH 194` übergeben. Ohne diese
Header ließ sich HTTP 403 von CloudFront reproduzieren; mit ihnen lieferte
dieselbe Abfrage HTTP 200 mit JSON-Daten.

Nach dem Add-on-Update den Push-Dienst neu starten und in ComatAlarm unter
**API-Status → Prüfen** das gemeinsame Ergebnis abrufen. Die Abfragen bleiben
zentral auf dem Pi; eine neue App-Version oder erneute Einrichtung ist nicht
nötig. Bestehende Standplätze, Lerndaten und Gerätefreigaben bleiben erhalten.

Falls BER weiterhin ablehnt, enthält die Protokollzeile neben HTTP-Status,
Antworttyp und Sperrdauer auch `gateway=CloudFront` oder `gateway=unknown`.
Anbieter-Sperrfristen und `Retry-After` werden weiterhin eingehalten. Eine
verbleibende Zugriffsablehnung bleibt als Fehler sichtbar.

## ComatAlarm: Übernahme aus der Administrator-App (ab 1.2.21)

Sobald die Administrator-App das Standplatzpaket vollständig übertragen hat,
verwendet der Pi diesen gespeicherten Bestand. Der zusätzliche Firebase-Download
mit Paket-Passphrase wird dann automatisch deaktiviert, auch nach Neustarts und
beim Wiederherstellen der verschlüsselten Sicherung. Eine eventuell verbliebene
Passphrase löst keine weiteren Entschlüsselungsversuche aus. Bereits gespeicherte
Standplätze, Referenzpunkte und gelernte Ankünfte bleiben erhalten.

In ComatAlarm zeigt **Einstellungen → Hintergrund-Push** den bestätigten Bestand:
**„Pi: Standplatzpaket bereit · … bestätigte Ankünfte gelernt“**. Dafür ist keine
erneute Veröffentlichung, Passwort-Eingabe oder neue Gerätefreigabe nötig.
Unvollständige App-Übertragungen schalten den Firebase-Import noch nicht ab.

## ComatAlarm: BER-Paket direkt aus Firebase (ab 1.2.20)

Der Pi kann das bereits in Firebase veröffentlichte Standplatzpaket ohne
Übertragung durch die Administrator-App laden. Dafür ist kein neuer App-Build
nötig. Bestehende Push-, Apple- und Firebase-Einstellungen bleiben erhalten.

1. **CallWebhook Push-Dienst auf 1.2.21 oder neuer aktualisieren.**
2. Unter **Konfiguration** das Passwortfeld **`comatalarm_ber_passphrase`**
   mit der vorhandenen **BER-Paket-Passphrase** ausfüllen. Gemeint ist das
   Passwort des verschlüsselten BER-Pakets beziehungsweise Schlüssel-Backups,
   nicht das Apple-Passwort und nicht `comatalarm_setup_key`.
3. **Speichern und den Push-Dienst neu starten.** Das bereits eingerichtete
   `comatalarm_firebase_service_account` wird für den Download verwendet.
4. Im **Protokoll** erscheint bei Erfolg:
   **„BER-Standplatzpaket aus Firebase geladen und verschlüsselt gespeichert“**,
   gefolgt von der Anzahl der Standplätze und Referenzpunkte.

Die Passphrase nur in diesem Passwortfeld hinterlegen. Der Dienst liest die
Dokumente `ber_package/document` und `ber_package/identity` samt Datenblöcken.
Das vorhandene Dienstkonto benötigt dafür Firestore-Lesezugriff; eine neue
Apple-Anmeldung, Testerprüfung oder Freigabe durch ein geöffnetes iPhone ist
für diesen Betreiber-Import nicht erforderlich. Er erweitert keine Geräterechte.

Beim Start wird geprüft, danach alle sechs Stunden. Unveränderte Pakete werden
nicht erneut heruntergeladen oder entschlüsselt. Bei Fehlern folgt ein neuer
Versuch nach fünf Minuten, mit wachsendem Abstand bis maximal 30 Minuten. Ein
Neustart stößt eine neue Prüfung an. Die bisherigen Standplätze und Lerndaten
bleiben bei falschem Passwort, unvollständigen Paketen und Verbindungsfehlern
erhalten. Flugabfrage und Push-Versand laufen während des Imports weiter.
Dieser direkte Import arbeitet nur, solange noch kein vollständig übertragenes
Standplatzpaket aus der Administrator-App vorliegt; die App-Übernahme hat Vorrang.

Die entschlüsselte PDF und der age-Schlüssel werden nicht als Dateien abgelegt.
Standplätze, Referenzpunkte und Lerndaten liegen verschlüsselt in `/data`, mit
zusätzlicher verschlüsselter Sicherungsdatei. Die Passphrase liegt in der privaten
Add-on-Konfiguration und einer nur für den Dienst lesbaren Datei in `/data`.
Add-on-Backups enthalten diese vertraulichen Daten. Wird das Passwortfeld geleert,
endet der direkte Download; bereits importierte Standplätze und Lerndaten bleiben.

**Lernmodell:** Dieser Download enthält die Standplatzgeometrie, nicht die bisher
auf dem iPhone gespeicherten Beobachtungen. Deren einmalige Übernahme und die
Anzeige der zentralen Prognosen benötigen weiterhin die dafür aktualisierte
ComatAlarm-App. Nach der Übernahme arbeitet das Pi-Modell eigenständig.

## Grundeinrichtung für CallWebhook

Dieses Add-on gehört **nur auf den Home Assistant des CallWebhook-Betreibers**.
Wer die App später herunterlädt, installiert dieses Add-on nicht und bekommt
keinen Apple-Schlüssel. Für den Raspberry Pi 5 wird `aarch64` unterstützt.

1. Den Einrichtungsassistenten der App durchlaufen. Bootstrap installiert die
   aktuelle HA-Komponente (API 10) und wartet auf den vollständigen HA-Neustart.
2. Im Abschluss bei Anruf-Push **Ich betreibe den gemeinsamen Push-Dienst auf
   diesem HA-Pi** wählen. Diese Auswahl erscheint nur, solange noch kein Dienst
   für die App verfügbar ist.
3. Einmalig die gültige **APNs-.p8-Datei** auswählen. Die Key-ID wird aus dem
   Apple-Dateinamen übernommen, die Team-ID aus der App. Beide prüfen. Ein
   App-Store-Connect-Schlüssel ist kein Ersatz. Vorhandene Add-on-Schlüssel
   werden bei erneuter Einrichtung ohne Dateiimport wiederverwendet.
4. **Push-Dienst automatisch einrichten** starten. Der Assistent findet den
   echten Repository-Slug, installiert das Add-on, überträgt die Zugangsdaten,
   aktiviert den Autostart und wartet auf den laufenden Dienst. Kein Store-
   Wechsel und keine manuell anzulegende Datei sind erforderlich.
5. Nabu-Casa-Fernzugriff bzw. eine öffentliche HTTPS-Adresse muss vorhanden sein.
   Der Assistent prüft die Erreichbarkeit, meldet das iPhone per App Attest an
   und richtet Asterisk-Push ein. Anschließend den gesperrten Testanruf machen.

Nach vollständigem Löschen des Betreiber-Add-ons muss dessen APNs-Schlüssel
neu importiert werden. Apple stellt einen gelöschten privaten Schlüssel nicht
über die App oder GitHub-Secrets wieder bereit. Die Originaldatei aufbewahren.

Die öffentliche Adresse steht im Abschluss unter Anruf-Push. Sie hat die Form:
`https://DEIN-HOST.ui.nabu.casa/api/callwebhook/push-relay`.
Für **andere Nutzer** diese Adresse einmal als GitHub-Repository-Variable
`CALLWEBHOOK_PUSH_RELAY_URL` hinterlegen und einen neuen App-Build erstellen.
Erst dieser Build kennt den gemeinsamen Betreiber-Dienst auch auf fremden
HA-Installationen. Keine Schlüsseldatei wird verteilt.

Es werden keine Routerports geöffnet und keine Add-on-Ports im Heimnetz
veröffentlicht. Die HA-Komponente leitet ausschließlich die festen Relay-
Endpunkte weiter. App Attest bzw. der Gerätezugang prüfen die Berechtigung;
HA- und Supervisor-Zugangsdaten werden nicht weitergereicht. Der Relay-Container
erhält keine HA-Konfigurationsdateien und keine Supervisor-API-Berechtigung.

Der interne Zugriff und der öffentliche HTTPS-Zugriff werden separat geprüft.
Eine grüne Erreichbarkeitsprüfung bestätigt noch keine Annahme durch Apple.
Den ersten echten eingehenden Anruf bei gesperrtem iPhone testen. SIP/Audio
benötigen weiterhin Heimnetz oder VPN zum jeweiligen Asterisk.

Für die signierte Developer-IPA in der Konfiguration
`allow_development: true` setzen und den Dienst neu starten. TestFlight und
App Store verwenden die Produktionsumgebung; Developer-IPAs die APNs-Sandbox.
Der Apple-Schlüssel muss die jeweils verwendete Umgebung unterstützen.
App Attest und die Prüfung der App-ID bleiben auch im Entwicklungsmodus aktiv.
Nach der Installation die App öffnen und die Push-Anmeldung erneut ausführen.

Der Schlüssel und die Registrierungsdaten liegen ausschließlich im privaten
Add-on-Verzeichnis `/data`. Ein Add-on-Backup enthält diese vertraulichen Daten.
Wenn Pi, Internetverbindung, HA oder Nabu-Casa-Fernzugriff ausfallen, ist der
gemeinsame Push-Dienst für die verbundenen Nutzer nicht erreichbar.


## ComatAlarm: vorhandenen Dienst erweitern

Benötigt Push-Add-on **1.1.0**, CallWebhook-HA-Komponente **1.2.12** und
Bootstrap **1.2.5**. Im Add-on-Store nach Updates suchen, Push Relay und Bootstrap
aktualisieren. Bootstrap einmal starten: Es installiert die neue HA-Komponente
und fordert einen HA-Neustart an. Vorhandene APNs-Optionen bleiben erhalten.

1. Neue ComatAlarm-TestFlight-Version öffnen, Mitteilungen erlauben und unter
   **Einstellungen → Hintergrund-Push** die eigene öffentliche HA-HTTPS-Adresse
   eingeben. Die App ergänzt `/api/callwebhook/push-relay` automatisch.
2. **Einrichtungsschlüssel erzeugen und kopieren** wählen. Im vorhandenen
   **CallWebhook Push Relay → Konfiguration** als `comatalarm_setup_key`
   speichern (32–256 Zeichen), Add-on neu starten und in ComatAlarm **Verbinden**.
   Dies ist ein eigener Einrichtungsschlüssel, kein Apple-Private-Key und kein
   GitHub-Secret. `apns_private_key`, `apns_key_id`, `apns_team_id` wiederverwenden.
3. **Echten Push-Test senden**. Die Annahme durch Apple und die tatsächlich
   sichtbare Meldung sind zwei getrennte Prüfungen. Anschließend mit gesperrtem
   iPhone einen Flugalarm testen; Fokusmodus und Mitteilungseinstellungen prüfen.

Der APNs-Schlüssel muss das Topic `de.comatalarm.app.ios.U98PKCA4W7` in der
Produktionsumgebung unterstützen. Ein auf eine andere App begrenzter Schlüssel
kann dafür nicht verwendet werden. CI aktiviert Push für die bestehende Bundle-ID
und erstellt bei Bedarf ein passendes Profil mit den vorhandenen Zertifikaten.
Es werden keine Zertifikate oder bestehenden Profile widerrufen.

Die App übergibt bis zu zwei Flüge, Alarmregeln und ausschließlich Zeitfenster
für erlaubte Kalendermeldungen (keine Kalendertitel). Flugereignisse werden im
Hintergrund ungefähr minütlich über FR24 abgefragt, Gate und Streichungen auch
über BER. Bei FR24-Tarif-/Zugangsfehlern werden FR24-Abfragen fünf Minuten pausiert.
Abfragen verbrauchen FR24-Kontingent. Bei geöffneter App pausiert das Add-on nach
der Übergabe; nach beendetem Flug endet dessen Abfrage. Ohne erneutes Öffnen der
App endet der Auftrag nach 24 Stunden. Kalenderänderungen während geschlossener
App werden erst beim nächsten Öffnen übernommen. Maximal zehn Geräte pro Dienst.

Der FR24-Token wird per HTTPS an den eigenen Dienst übertragen und dort im
privaten Add-on-Verzeichnis gespeichert; niemals öffentlich in GitHub.
Gerätezugänge, Flugzustand und bereits gesendete Ereignisse bleiben bei einem
Add-on-Neustart erhalten. Abgelaufene Aufträge löschen den FR24-Token aus dem
aktiven Datensatz. **Verbindung trennen** löscht Gerätezugang und Flugauftrag.
Der Apple-Schlüssel bleibt ausschließlich im Betreiber-Add-on. Der neue
ComatAlarm-Zugang wird getrennt von CallWebhook-App-Attest/VoIP und HATTS geprüft.

Hintergrundmeldungen: Abflug, Landung, Gateänderung, bestätigtes Gate-Arrival,
Streichung und Umleitung, soweit die jeweiligen Quellen Ereignisse liefern.
Eine Passagier-Gate-Angabe wird niemals als bestätigter BER-Standplatz ausgegeben.
Die lokale GPS-Standplatzprüfung läuft weiter in der App. Kein Standortverlauf
oder entschlüsseltes BER-Standplatzpaket wird dafür an das Add-on übertragen.

## Interne ComatAlarm-Tester ab Version 1.2.1

Im Master-iPhone unter **Einstellungen → Interne TestFlight-Tester** eine
vorhandene interne Gruppe auswählen, Vorname, Nachname und E-Mail eintragen
und **Intern einladen und aktuellen Build zuweisen** wählen. Die Namen braucht
Apple für neue App-Store-Connect-Benutzer.

Neue Benutzer erhalten ausschließlich `MARKETING`, Zugriff nur auf ComatAlarm
und keinen Zugriff auf Zertifikate oder Provisioning-Profile. Marketing erlaubt
auch die Bearbeitung von App-Store-Metadaten; Apple bietet keinen internen
TestFlight-Zugang ohne App-Store-Connect-Rolle. Bestehende Rollen und App-Rechte
werden nicht verändert. Die Firebase-Freigabe erfolgt wie bisher separat.

Die Person nimmt zuerst die Apple-Benutzer-Einladung an. Der Dienst prüft offene
Zuweisungen etwa alle fünf Minuten, ordnet den neuesten verarbeiteten und nicht
abgelaufenen Build der gewählten internen Gruppe zu und fügt den Tester hinzu.
Danach nimmt die Person die TestFlight-Einladung an. Bereits bestehende
Gruppenzuordnungen werden wiederverwendet. Einladungsstatus und Fehler erscheinen
beim Aktualisieren der Testgruppen. Offene Aufträge bleiben bei Neustarts
bestehen und enden nach sieben Tagen; automatische Prüfungen verschicken keine
weiteren Benutzer-Einladungen.

Der Betreiber benötigt `asc_issuer_id`, `asc_key_id` und `asc_private_key` eines
App-Store-Connect-Schlüssels mit Benutzerverwaltungs- und TestFlight-Rechten im
Add-on. Der APNs-Schlüssel ist dafür kein Ersatz. Die Apple-Schlüssel bleiben
im Add-on. Ein regulärer Gerätezugang reicht nicht zur Tester-Verwaltung:
zusätzlich muss der Master-Einrichtungsschlüssel vorliegen.

Alte App-Versionen dürfen über diesen Endpunkt keine neuen internen Benutzer
anlegen. Neue App und Add-on müssen deshalb gemeinsam aktualisiert werden.

## Version 1.2.2 – gemeinsame Flugabfragen

Mehrere COMATAlarm-Geräte mit demselben FR24-Token teilen sich die Abfrage eines Fluges für 60 Sekunden, auch bei unterschiedlichen lokalen Eintrags-IDs oder Erstellungszeiten. Benachrichtigungen und Alarm-Einstellungen bleiben pro Gerät getrennt. Ein aktiver API-Leader liefert seine synchronisierten Ereignisse an Hintergrundgeräte; ein geöffnetes Folgegerät blockiert die Hintergrundabfrage nicht.

Für die vollständige Korrektur auch COMATAlarm auf allen Tester-Geräten aktualisieren: Die App führt doppelte Flugeinträge und Verlaufseinträge zusammen und erhält deren alte IDs für spätere Synchronisierung und Löschung.

## Version 1.2.7 – Apple-Testerfreigabe

Zuerst Bootstrap 1.2.6 aktualisieren und einmal starten, um HA-Komponente 1.2.14 zu installieren. Dabei wird Home Assistant neu gestartet. Dann Push-Dienst 1.2.7 installieren. Bestehende APNs- und App-Store-Connect-Optionen bleiben erhalten.

Im Google-Cloud-Projekt `comat-ber-alarm` ein eigenes Dienstkonto mit `roles/firebaseauth.viewer` und `roles/datastore.user` erstellen. Dessen JSON-Schlüssel ausschließlich in die geheime Add-on-Option `comatalarm_firebase_service_account` eintragen (vollständiges JSON, kein Base64). Nicht in GitHub, Chat oder die App eintragen. Optionen speichern und Push-Dienst neu starten.

Das Dienstkonto kann Firestore-Daten lesen und schreiben. Der Server prüft vor jeder Freigabe eine bestätigte Apple-E-Mail, die angenommene App-Store-Connect-Mitgliedschaft, den Zugriff auf ComatAlarm, eine interne TestFlight-Gruppe und eine mögliche Gerätesperre. Private Relay-Adressen werden nicht freigegeben. Bestehende Apple-Zugangsdaten brauchen Lesezugriff auf Benutzer, Apps und Testgruppen.

Erst nach erfolgreichem Test in der neuen Master-App die strengen BER-Zugriffsregeln aktivieren. Die neue Anmeldung und automatische Freigabe sind durch das Add-on-Update allein noch nicht aktiv. Ohne Dienstkonto bleibt nur die neue Freigabe-Route gesperrt; bisherige Push-Funktionen benötigen diesen Schlüssel nicht.


### ComatAlarm-Dashboard ab 1.2.26

Bereinigter Verlauf, Fluglöschung und BER-Ankunftstabelle mit direkter Pi-Überwachung: Bootstrap 1.2.9 einmal starten und die aktuelle Dashboard-YAML übernehmen. Details in `homeassistant/dashboards/COMATALARM.md`. Bestehende Zugangsdaten werden verwendet; keine neue Schlüsseleinrichtung.
