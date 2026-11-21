# Einmalige Einrichtung auf dem HA-Pi des App-Betreibers

## ComatAlarm: Protokolle kopieren und herunterladen (ab 1.2.33)

Die aktualisierte Dashboard-View bietet Kopieren und Textdownload für
Ereignisse, Push-Versand und Fehlerprotokoll. Die Ausgabe verwendet die bereits
geladenen, bereinigten Monitordaten mit Zeitstempeln. Der Export selbst führt
keine zusätzlichen Anbieterabfragen aus.

Neben dem Add-on muss die bestehende ComatAlarm-View im Home-Assistant-Dashboard
durch `homeassistant/dashboards/comatalarm.yaml` aus diesem Stand ersetzt werden.
Die genaue Anleitung steht in `homeassistant/dashboards/COMATALARM.md`.
Ein Add-on-Update ersetzt keine gespeicherte Dashboard-Konfiguration.

## ComatAlarm: zeitnahe Ankunfts- und Landealarme (ab 1.2.33)

Der Pi verarbeitet fällige Ankunftsereignisse vor allgemeinen Flugplanabfragen.
Nach bestätigter Landung nutzt er den 15-Sekunden-Arbeitszyklus für einen
gezielten FR24-Ereignisabgleich. Bis zu 15 Flug-IDs teilen sich eine Abfrage.
Eine erkannte AIBT, ihre Koordinate und der zugeordnete Stand werden gemeinsam
gespeichert; der zugehörige Push wird anschließend direkt bearbeitet.

Ergänzende BER-, FlightStats- und FlightAware-Abfragen laufen in einem begrenzten
Hintergrundcache. Ihre Netzwerkwartezeit blockiert den Alarmdurchlauf nicht.
Der bestehende Arbeitszyklus übernimmt fertig verfügbare Flugplanwerte erst nach
erneuter Prüfung von Gerätefreigabe und Flugzustand. Auch der periodische
API-Statuscheck wartet im Hintergrund. Der 15-Sekunden-Takt ist die Zielkadenz;
die Laufzeit der FR24-Abfrage und die anschließende Push-Übertragung bleiben
zusätzliche Verarbeitungsschritte.

Bei einem plausiblen Endanflug werden aktuelle Flugdaten gezielt alle
30 Sekunden abgeglichen. Der Drei-Minuten-Voralarm benötigt zwei passende
Beobachtungen, eine aktuelle FR24-ETA und einen plausiblen Sinkflug. Ein
fehlendes zusätzliches Bodenstatus-Feld verhindert die Auswertung nicht;
explizite Bodenmeldungen und bestätigte Landungen schließen den Voralarm aus.
Die Frischegrenzen und das gemeinsame Minutenbudget bleiben wirksam.

## ComatAlarm: fehlende AIBT nach bestätigter Landung (ab 1.2.33)

Ist die Landung (ATA) bestätigt, aber die tatsächliche Ankunft am Stand (AIBT)
noch nicht übernommen, fragt der Pi das Ankunftsereignis gezielt weiter ab.
Nach dem bisherigen 25-Minuten-Zeitfenster folgen begrenzte Nachprüfungen mit
Warteabständen bis 24 Stunden nach ATA. Die vom Anbieter gelieferte AIBT und
die zugehörige Position werden gemeinsam gespeichert und im Verlauf verteilt.

Das gilt auch für bereits gespeicherte gemeinsame Archivankünfte. Die passende
iPhone-App übergibt außerdem eigene offene Verlaufseinträge als gesonderten
Nachholauftrag, sobald der Pi diese Funktion unterstützt. Der Auftrag legt
keine neue Überwachung an und löst keine alten Alarme erneut aus. Explizite
Löschungen, die aktuelle Gerätefreigabe und das bestehende API-Budget gelten
auch während der Nachholung. EIBT und ein abgelaufenes Zeitlimit ersetzen die
tatsächliche AIBT nicht.

## ComatAlarm: AIBT-Koordinaten nachbessern (ab 1.2.32)

Der Push-Dienst verarbeitet die FR24-Ereignisposition und die vollständige
Flugspur mit deren jeweiligem Antwortformat. Tatsächliche AIBT-Zeit und
zugehörige Koordinate bleiben zusammen; eine Position eines anderen Legs
oder eines älteren Ankunftsereignisses wird nicht übernommen.

Wurden die Koordinaten bei den ersten Abfragen noch nicht übernommen, prüft der
Pi innerhalb von 24 Stunden nach AIBT mit zunehmenden Abständen weiter.
Das gilt auch für belegte, gespeicherte gemeinsame Archivankünfte, deren
aktive Überwachung bereits beendet wurde. Anbieterlimits und Löschungen
bleiben wirksam; die Nachbesserung erzeugt keine neuen Flugkarten oder
wiederholten Alarme. Der reine Verlaufsdownload selbst führt weiterhin
keine Flugdatenabfragen aus.

Die passende neue iPhone-App übernimmt nachgebesserte Koordinaten auch dann,
wenn ein älterer lokaler Reparaturversuch erfolglos war. Manuell festgelegte
Parkpositionen bleiben erhalten. Eine vorhandene, noch nicht eindeutig
zugeordnete Koordinate wird nicht mehr als fehlende Koordinate bezeichnet.

## ComatAlarm: Pi-Verlauf auf dem iPhone (ab 1.2.31)

Ein Flug, der auf der Pi-Oberfläche übernommen und bei geschlossener iPhone-App
abgeschlossen wurde, wird mit der passenden neuen App beim nächsten Abgleich
direkt in den Verlauf übernommen. Das gilt auch dann, wenn er nicht mehr aktiv
überwacht wird. Vorhandene Flugzeiten, Paketdaten und bestätigte Standnachweise
werden mitgeführt. Wiederholte Abgleiche erzeugen keine zweite Flugkarte und
keine erneute Alarmierung.

Der Pi stellt dafür den gespeicherten aktuellen Kalendermonat in der Zeitzone
Europe/Berlin bereit. Bereits vorhandene Daten werden übernommen, soweit ihre
Herkunft aus einer freigegebenen Überwachung oder einer Pi-Aktion belegt ist.
Frühere, bereits auf dem Pi gelöschte Monatsbestände können daraus nicht
wiederhergestellt werden. Die auf dem iPhone schon gespeicherte Historie wird
durch eine leere Antwort oder den Monatswechsel nicht gelöscht.

Der Abruf verwendet die vorhandene Geräteberechtigung und ausschließlich
gespeicherte Daten. Es entstehen dadurch keine zusätzlichen FR24-Abfragen.
Explizite Löschungen werden auch beim späteren Archivabgleich berücksichtigt.
Für diesen Abgleich sind der Push-Dienst 1.2.31 und die passende neue iOS-App
erforderlich. Nach dem Update die App einmal öffnen und den Verlauf prüfen.

## ComatAlarm: gemeinsame Hintergrundalarme und Landevorwarnung (ab 1.2.29)

Neue Kollegen-Überwachungen werden vom Pi an die bereits registrierten,
weiterhin freigegebenen Geräte verteilt. Die Empfänger müssen ihre App dafür
nicht öffnen. Zentrale Registrierungen werden unabhängig vom App-Kontakt
verlängert; gesperrte und widerrufene Geräte bleiben ausgeschlossen.

Der neue Voralarm meldet eine Landung voraussichtlich in etwa drei Minuten.
Er verlangt zwei frische, konsistente Live-Beobachtungen und prüft ETA, Kurs,
Entfernung, Höhe über dem Zielflughafen und Sinkrate. Eine geplante Ankunftszeit
oder veraltete Position löst keinen Voralarm aus. Bei unzuverlässigen Daten
wird die Vorwarnung ausgelassen; die gewöhnliche bestätigte Landemeldung bleibt
unabhängig. Die Warnung gilt nur kurz und wird vor jedem Zustellversuch geprüft.

Ab 1.2.34 beginnt der 30-Sekunden-Liveabgleich bereits bei einer gemessenen,
plausiblen Annäherung, bevor alle engeren Alarmgrenzen erreicht sind. Auch ohne
Live-ETA darf eine neue Beobachtung angefordert werden. Für die Warnung selbst
bleiben eine verwendbare Live-ETA, höchstens 45 Sekunden alte Daten und zwei
unabhängige, konvergierende Beobachtungen erforderlich. Eine ETA-Korrektur um
höchstens 60 Sekunden verwirft diese Bestätigung nicht allein; größere Sprünge
erfordern eine neue Bestätigung. Gewarnt wird weiterhin ausschließlich bei einem
aktuellen ETA-Vorlauf von mehr als 150 und höchstens 180 Sekunden.

Das Ereignisjournal protokolliert jetzt auch den Entscheidungsgrund für eine
ausbleibende Vorwarnung, einschließlich Live-ETA, Vorlauf, Datenalter und
Bestätigungsstand, soweit diese Werte tatsächlich vorliegen. Veraltete Daten,
Zuordnungsprobleme, größere ETA-Korrekturen, verpasste Zeitfenster und Alarmregeln
werden unterschieden. Wichtige Übergänge erscheinen sofort; normale
Zwischenzustände werden begrenzt, damit sie den Push-Verlauf nicht verdrängen.
Die vorhandenen Dashboard-Aktionen „Ereignisse“ und „Diagnose teilen“ nehmen
diese Einträge automatisch auf. Für diese Ergänzung sind keine neue
Dashboard-YAML und kein weiterer iPhone-Build nach 1250 erforderlich.

Das Add-on muss aktualisiert werden. Die passende neue iOS-Version ergänzt den
Schalter, direkte Flugübergaben, explizite Löschungen und Token-Erneuerung für
Tester. Vorhandene Einrichtungsschlüssel, APNs-Zugänge und Gerätefreigaben
werden weiterverwendet. Die vollständige Zustellprüfung erfolgt mit gesperrten
Geräten und geschlossenen Apps; ein grüner Build oder APNs-Erfolg allein reicht
dafür nicht aus.

Ab 1.2.30 bündelt der Pi fällige FR24-Abfragen verschiedener überwachter Flüge
pro Zugang und identischem Zeitfenster. So bleiben auch bei mehreren Kollegen
genügend HTTP-Anfragen für aktuelle Anflugdaten verfügbar. Das bestehende
Limit von acht Anfragen pro Minute und Anbieter-Sperrfristen bleiben erhalten.
Die Bündelung spart HTTP-Anfragen, nicht automatisch FR24-Credits: Diese hängen
von den zurückgegebenen Flugdaten ab. Es werden keine zusätzlichen Flüge
vorsorglich abgefragt.

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

Die App übergibt Flüge, Alarmregeln und ausschließlich Zeitfenster für erlaubte
Kalendermeldungen (keine Kalendertitel). Flugereignisse werden auf dem Pi ungefähr
minütlich über FR24 abgefragt, Gate und Streichungen auch über BER. Anbieterlimits
und Sperrfristen bleiben wirksam; Abfragen verbrauchen FR24-Kontingent. Die
zentrale Überwachung läuft bei geöffneter und geschlossener App. Abgeschlossene
Flüge und zeitlich veraltete Vorgänge beenden ihre regulären Abfragen.
Kalenderfenster reichen mit der neuen App 14 Tage; Änderungen werden beim
nächsten App-Kontakt übernommen. Nach Ablauf wird ohne neue Fenster nicht
eigenmächtig laut geschaltet. Maximal zehn Geräte pro Dienst.

Der FR24-Token wird per HTTPS an den eigenen Dienst übertragen und dort im
privaten Add-on-Verzeichnis gespeichert; niemals öffentlich in GitHub.
Gerätezugänge, Flugzustand und bereits gesendete Ereignisse bleiben bei einem
Add-on-Neustart erhalten. Nur ungebundene ältere Registrierungen behalten den
bisherigen Ablauf nach 24 Stunden. **Verbindung trennen** löscht den eigenen
Gerätezugang und dessen Flugauftrag.
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


### Ankunftstabelle täglich um 05:00 Uhr (ab 1.2.27)

Der Pi lädt die BER-Tagesliste täglich um **05:00 Uhr Europe/Berlin**. Sommer- und
Winterzeit werden berücksichtigt. Der eigene Arbeitszyklus benötigt weder ein
geöffnetes Dashboard noch eine offene App oder aktive Flugüberwachungen. War der
Pi um 05:00 Uhr aus, holt er die Liste nach dem Start nach. Bei BER-Fehlern wird
frühestens nach fünf Minuten und erst nach Ablauf einer Anbieter-Sperrfrist erneut
versucht. Eine bereits erfolgreich geladene Tagesliste bleibt erhalten.

Die aktualisierte iOS-App übernimmt die Liste aus dem Pi-Cache und meldet sich dabei
für tägliche Daten-Pushes an. Dazu einmal nach dem App-Update mit bestehender
Pi-Verbindung öffnen. Es sind keine neuen Schlüssel oder zusätzlichen HA-Proxy-Routen
nötig. Bestehende Flugüberwachungen und Alarmregeln bleiben unverändert.

Der stille APNs-Push enthält Tag und Revision; anschließend holt die App die vollständige
Liste über den vorhandenen, mit Geräteberechtigung geschützten HTTPS-Zugang. Dieser
Download fragt BER nicht erneut ab. Die App ersetzt ihren dauerhaften Cache erst,
wenn alle Seiten derselben Revision vollständig vorliegen. Ein erfolgreicher Versand
an APNs wird unter Ereignisse & Push-Versand protokolliert; er bestätigt die Annahme
bei Apple, nicht die Ausführung auf dem iPhone. Apple kann stille Pushes verzögern oder
verwerfen (insbesondere nach manuellem Beenden der App). Beim nächsten Öffnen gleicht
die App deshalb zusätzlich ihre Revision mit dem Pi ab.


### AIBT-Koordinaten nachladen (ab 1.2.28)

Ein bestätigter AIBT-Zeitpunkt beendet den Koordinatenabruf nicht mehr. Der Pi
übernimmt zunächst die Koordinate aus dem FR24-Gate-Arrival-Ereignis. Falls dort
noch keine verwertbare BER-Koordinate vorliegt, prüft er die historische
Momentaufnahme und anschließend bei Bedarf die Spur der exakten FR24-Flug-ID.
Spurpunkte müssen innerhalb von 60 Sekunden um AIBT, nach ATA und vor einem
gegebenenfalls bekannten Folgeabflug liegen, in BER sein und höchstens 5 km/h
Bodengeschwindigkeit haben. Fremde Flug-IDs und beliebige aktuelle Positionen
werden nicht als Standplatzbeleg verwendet.

Für im Pi gespeicherte, noch registrierte Flüge mit AIBT jünger als 24 Stunden
läuft die Wiederherstellung ohne Öffnen der App. Bis zu sechs erfolgreiche
Datenabfragen ohne Treffer, mindestens fünf Minuten auseinander; Anbieterfehler
und Tarif-/Minutensperren werden mit Wartefrist behandelt und zählen nicht als
fehlende Daten. Alte Wiederherstellungszähler blockieren diese Korrektur nicht.
AIBT-Abrufstatus und erfolgreiche Übernahme erscheinen im Ereignisprotokoll.
Die zusätzliche Spurabfrage fällt nur bei fehlender verwertbarer Momentaufnahme
an und verbraucht FR24-Credits. Das bestehende App-Build kann die nachgeladenen
Koordinaten über den bisherigen Pi-Abgleich übernehmen.
