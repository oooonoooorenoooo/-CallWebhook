# Einmalige Einrichtung auf dem HA-Pi des App-Betreibers

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
