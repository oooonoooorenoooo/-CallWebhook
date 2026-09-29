# Einmalige Einrichtung auf dem HA-Pi des App-Betreibers

Dieses Add-on gehört **nur auf den Home Assistant des CallWebhook-Betreibers**.
Wer die App später herunterlädt, installiert dieses Add-on nicht und bekommt
keinen Apple-Schlüssel. Für den Raspberry Pi 5 wird `aarch64` unterstützt.

1. Im bereits vorhandenen CallWebhook-Repository den Add-on-Store aktualisieren
   und **CallWebhook Push-Dienst** installieren.
2. Unter **Konfiguration** einmalig die Team-ID, die Key-ID und den vollständigen
   Inhalt des gültigen **APNs**-Schlüssels (`.p8`) hinterlegen. Der Schlüssel gehört
   zum Entwicklerteam von CallWebhook. Den vorhandenen TestFlight-/App-Store-
   Connect-Schlüssel nicht dafür verwenden. Die Datei nicht in GitHub hochladen.
   Mehrzeiliges PEM, eingefügte Leerzeichen oder wörtliche `\n` werden unterstützt.
3. Speichern und das Add-on starten. **Beim Booten starten** eingeschaltet lassen.
4. **CallWebhook Bootstrap** starten, damit die HA-Komponente API 9 installiert
   wird; den HA-Neustart abwarten. Nabu-Casa-Fernzugriff muss eingeschaltet sein.
5. In der App den Abschluss des Assistenten öffnen bzw. die eingerichtete App neu
   starten. Auf diesem Betreiber-HA erkennt die App den Dienst und dessen
   öffentliche HTTPS-Adresse automatisch. Sie prüft den öffentlichen Health-Endpunkt
   und meldet das iPhone per App Attest an. Danach wird Asterisk aktualisiert.

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
