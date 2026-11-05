# ComatAlarm im Home-Assistant-Dashboard

Der Reiter liest ausschließlich gespeicherte Zustände des Pi. Er stößt weder Flugabfragen noch Pushes an. Die ComatAlarm-App benötigt kein Update.

## Einrichten

1. CallWebhook Push-Dienst auf 1.2.25 aktualisieren.
2. CallWebhook Bootstrap auf 1.2.8 aktualisieren und einmal starten. Er installiert das HA-Backend 20 (Integration 1.2.16) und fordert einen Home-Assistant-Neustart an. Bestehende Konfiguration und Schlüssel bleiben erhalten.
3. Die View aus `comatalarm.yaml` unter `views:` des vorhandenen Dashboards einfügen. Das vorhandene `custom:button-card` muss installiert sein. Für Reno wurde außerdem das vollständige bestehende Dashboard mit dem Reiter unmittelbar nach Home vorbereitet.
4. Als Home-Assistant-Administrator öffnen. Die Ansicht aktualisiert alle 15 Sekunden. Es wird kein Schlüssel in YAML oder Browser gespeichert.

## Bedeutung

- **Pi-Zyklus**: Start und Ende des tatsächlichen Hintergrunddurchlaufs; ein abgeschlossener Zyklus allein beweist keinen erfolgreichen Anbieterabruf. Datenalter und Fehler je Flug ebenfalls beachten.
- **API-Status**: derselbe gespeicherte Status, den die Apps erhalten. Öffnen des Reiters ruft keine Anbieter ab.
- **Flüge**: geräteübergreifend nach Flugnummer, Flugtag und Strecke zusammengefasst, einschließlich noch gespeicherter abgeschlossener Flüge. Geräte werden pseudonymisiert. Die Geräteansicht zeigt Ablauf und letzte App-Abfrage. Ein App-Abruf bedeutet eine bediente State-Anfrage, keine Bestätigung der Verarbeitung auf dem iPhone.
- **Position**: Messzeit und Alter separat. Nach 120 Sekunden veraltet; alte Werte bleiben als Diagnose sichtbar, werden aber nicht als aktuelle Fluglage dargestellt. Distanz ist die aus der letzten Position berechnete Luftlinie nach BER; für andere Ziele bleibt sie leer. Höhe und Steigrate werden aus Fuß, Geschwindigkeit aus Knoten umgerechnet.
- **Flugplan**: die auf dem Pi gespeicherten STD/ETD/ATD/STA/ETA/ATA/EIBT/AIBT. Fehlende Daten werden nicht erfunden. Abgelaufene ETA wird als überschritten gekennzeichnet. Die Restzeit ist eine ETA-Differenz, keine aus Geschwindigkeit und Distanz berechnete Flugzeit.
- **Push**: erkannt/vorgemerkt, von Apple angenommen, fehlgeschlagen oder durch Alarmregeln verworfen. APNs-Annahme ist keine iPhone-Zustellbestätigung. Ausstehende Pushes sind 15 Minuten sendefähig; ältere ungesendete Einträge erscheinen als abgelaufen. Zähler beziehen sich auf die bis zu 48 Stunden gespeicherte Outbox.
- **Protokoll**: neue Ereignisse und ausgewählte, sicher bereinigte ComatAlarm-Fehler ab Installation. Keine rückwirkende Übernahme alter Textlogs. 1000 Einträge persistent, letzte 150 angezeigt; Zeit in Europe/Berlin. Rohantworten, Schlüssel und Kontodaten werden nicht exportiert.

## Zugriff

Die HA-Route `/api/callwebhook/comatalarm/monitor` verlangt Anmeldung und Administratorrechte. Sie löst nur das eigene, bereits konfigurierte Add-on auf und verwendet den vorhandenen Einrichtungsschlüssel serverseitig. Der interne Monitor-Endpunkt ist nicht in der öffentlichen Relay-Proxy-Whitelist enthalten. Das Add-on erhält keine zusätzlichen Home-Assistant- oder Supervisor-Rechte.

## Einklappen und Monatsverlauf

Alle Bereiche und Flugdetails starten geschlossen; während automatischer Aktualisierungen bleiben die geöffneten Bereiche erhalten. Die Tagesauswahl verwendet Berliner Zeit und zeigt alle Tage des aktuellen Monats. Ein Punkt kennzeichnet gespeicherte Flüge.

Der Pi archiviert automatisch den letzten bekannten Stand je Flugtag vor dem Entfernen oder Ablauf einer Überwachung und am Ende seiner Abfragezyklen. Die Übersicht muss dafür nicht geöffnet sein. Der aktuelle Monat bleibt über Neustarts erhalten; der vorherige Monat wird beim nächsten Archivierungslauf entfernt. Frühere, bereits gelöschte Flüge sind nicht rekonstruierbar. Das Tagesjournal zeigt nur noch vorhandene Einträge des begrenzten Diagnosejournals.

Wartend bedeutet: kein bestätigter Abflug und keine aktuelle Luftposition. Aktiv umfasst abgeflogene oder gelandete, noch nicht am Stand bestätigte Ankünfte. Abgeschlossen bedeutet AIBT bei Ankünften, ATD bei Abflügen ab BER, Ende des vorhandenen Ankunfts-Nachlaufs ohne AIBT oder Annullierung/Umleitung. Technischer Überwachungsstatus (z. B. Reparatur eines Vorflugs) wird separat angezeigt und kann fortbestehen.

Frontend-Regressionsprüfung: `node homeassistant/dashboards/test-monitor.cjs` (Python mit PyYAML erforderlich).
