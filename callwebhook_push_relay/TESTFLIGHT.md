# ComatAlarm: externe Tester und Standplatz-Push

Benötigt Push-Dienst 1.2.0 und Home-Assistant-Komponente CallWebhook 1.2.13. Nach dem Update Home Assistant neu starten. Bestehende APNs- und ComatAlarm-Einstellungen bleiben erhalten.

Für die optionale Testeraufnahme die drei zusätzlichen Add-on-Optionen ausfüllen:

| Add-on-Option | Bestehendes ComatAlarm-GitHub-Secret |
| --- | --- |
| `asc_issuer_id` | `APP_STORE_CONNECT_ISSUER_ID` |
| `asc_key_id` | `APP_STORE_CONNECT_KEY_ID` |
| `asc_private_key` | Inhalt von `APP_STORE_CONNECT_PRIVATE_KEY` (vollständige .p8-Datei) |

GitHub gibt gespeicherte Secrets nicht zum Auslesen zurück. Die ursprünglichen Werte verwenden. Der Schlüssel benötigt Zugriff auf ComatAlarm und die Verwaltung von TestFlight-Testern. Der APNs-Schlüssel ist ein anderer Schlüssel. Alle drei Felder leer lassen, wenn die Testeraufnahme nicht benötigt wird; Push bleibt verfügbar.

Danach Add-on neu starten. Auf dem Master-iPhone: Einstellungen → Externe TestFlight-Tester → Testgruppen laden → externe Gruppe auswählen → E-Mail → Tester hinzufügen. Es werden ausschließlich externe Gruppen von ComatAlarm angeboten und serverseitig akzeptiert. Testeraufnahme benötigt Geräte-Zugang plus Master-Einrichtungsschlüssel. Normale freigegebene Geräte besitzen diesen Schlüssel nicht. Die App zeigt nur den von Apple bestätigten Aufnahmeerfolg; verfügbare Builds und Einladung werden von TestFlight verwaltet. Die Firebase-Freigabe bleibt ein separater Schritt.

Standplatz-Push enthält Flugnummer, Kennzeichen und bestätigten Stand, z. B. `LH172 · D-AIDL · Stand B05`. Neue App einmal öffnen und BER-Liste entsperren: der reine Standplatzindex wird über HTTPS an den eingerichteten eigenen Push-Dienst übertragen und dort verschlüsselt gespeichert. Haltebalken/Rollwegdaten werden nicht übertragen. Ein kurzfristig gesperrtes iPhone löscht den bereits übertragenen Index nicht.

Der Dienst verwendet `historic/flight-events/light`, wertet die AIBT-Koordinate `details.gate_lat/gate_lon` aus und gleicht sie mit dem Index ab. Das Feld `gate_ident` wird nicht als Stand übernommen. Ohne eindeutigen Treffer wird ausdrücklich `Stand nicht ermittelt` gemeldet, keine Position erfunden. Die AIBT-Koordinate geht mit dem gespeicherten Ergebnis zurück an die App; abgeschlossene Flüge werden nicht weiter abgefragt. Die Tests benutzen feste Antworten, keine kostenpflichtigen FR24-Aufrufe und keine echten Testereinladungen.
