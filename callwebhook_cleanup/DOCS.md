# CallWebhook vollständig entfernen

Dieses Werkzeug funktioniert unabhängig davon, ob die CallWebhook-Integration
noch funktioniert. Es ist für Home Assistant OS/Supervised vorgesehen.

## Bedienung

1. In Home Assistant **Einstellungen → Apps → App-Store** öffnen und nach einem
   Store-Neuladen **CallWebhook vollständig entfernen** aus dem vorhandenen
   CallWebhook-Repository installieren.
2. **Starten** drücken. Damit wird die vollständige Löschung ausgelöst.

Home Assistant ist während der Bereinigung kurz nicht erreichbar und wird danach
wieder gestartet. Das Werkzeug entfernt sich zum Schluss selbst. Bei einem Fehler
bleibt es installiert; im Protokoll steht der letzte abgeschlossene Schritt.

## Löschumfang

- Asterisk aus dem TECH7Fox-Repository, CallWebhook Bootstrap und CallWebhook
  Push-Dienst: jeweils das gesamte Add-on inklusive privater Daten und
  Konfigurationsordner (`remove_config: true`). Ein auf diesem Pi betriebener
  gemeinsamer Push-Dienst steht anschließend auch anderen Nutzern nicht mehr zur
  Verfügung; seine APNs-Schlüssel und Registrierungen werden gelöscht.
- `/config/callwebhook` einschließlich Aufnahmen, Archiv, SIP-Aufträgen,
  Push-Schlüsseln und Einstellungen.
- `/config/custom_components/callwebhook` einschließlich Python-Cache.
- Der CallWebhook-Eintrag in `configuration.yaml` sowie die beiden
  `fritz_callwebhook_user`/`fritz_callwebhook_password`-Einträge in `secrets.yaml`.
- Der Standard-Anrufstatus-Helfer `input_boolean.iphone_call_active`, einschließlich
  seiner Speicher- und Registereinträge. Andere Helfer werden erhalten.

Die Bearbeitung gemeinsamer Dateien erfolgt ausschließlich bei gestopptem HA.
Andere YAML-Einträge, Geheimnisse und Integrationen bleiben erhalten. Verknüpfungen
(Symlinks) und nicht sicher bearbeitbare YAML-Strukturen führen zum Abbruch.

Gemeinsame HA-Backups, Logdateien und die Verlaufsdatenbank werden nicht gelöscht.
Auch selbst erstellte Automationen mit Verweisen auf CallWebhook sowie manuell in
andere YAML-Dateien ausgelagerte Konfigurationen werden nicht verändert. Die
Store-Repository-Einträge bleiben zum erneuten Installieren bestehen.
FRITZ!Box, Mobilfunk-Rufumleitungen und die iPhone-App werden nicht verändert.

Für einen erneuten Test von null anschließend den Assistenten in der iPhone-App
neu starten. Das Werkzeug niemals als automatischen Start beim HA-Boot aktivieren.
