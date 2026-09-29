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

Der Schlüssel und die Registrierungsdaten liegen ausschließlich im privaten
Add-on-Verzeichnis `/data`. Ein Add-on-Backup enthält diese vertraulichen Daten.
Wenn Pi, Internetverbindung, HA oder Nabu-Casa-Fernzugriff ausfallen, ist der
gemeinsame Push-Dienst für die verbundenen Nutzer nicht erreichbar.
