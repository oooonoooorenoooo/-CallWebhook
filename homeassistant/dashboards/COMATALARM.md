# ComatAlarm im Home-Assistant-Dashboard

Der Reiter zeigt gespeicherte Pi-Zustände. Die Ankunftstabelle lädt auf Anforderung eine gemeinsame BER-Tagesliste; daraus können Administratoren Flüge überwachen oder bestehende Flüge vom Pi entfernen. Ereignisse, Push-Versand und Fehler lassen sich direkt kopieren oder als Textdatei herunterladen. Die Exportfunktionen benötigen kein App- oder Add-on-Update, wenn die bisherige Pi-Übersicht bereits funktioniert.

## Einrichten

1. Den aktuellen CallWebhook Push-Dienst und die vorhandene Home-Assistant-Integration verwenden. Bei einer Ersteinrichtung installiert der aktuelle CallWebhook Bootstrap die Integration; den danach angeforderten Home-Assistant-Neustart ausführen. Eine bereits funktionierende Monitoransicht benötigt für Kopieren und Download keine erneute Einrichtung und keine neuen Schlüssel.
2. Das vorhandene `custom:button-card` muss installiert sein. Die Datei [comatalarm.yaml](comatalarm.yaml) enthält genau eine Dashboard-View; sie gehört unter `views:` des vorhandenen Dashboards.
3. Als Home-Assistant-Administrator öffnen. Die Ansicht aktualisiert alle 15 Sekunden. Es wird kein Einrichtungsschlüssel in YAML oder Browser gespeichert.

## Vorhandene Ansicht aktualisieren

Die Dashboard-Konfiguration wird in Home Assistant gespeichert. Ein Update des Push-Dienst-Add-ons, einschließlich 1.2.33, ersetzt diese Konfiguration nicht automatisch. Die neuen Export-Schaltflächen erscheinen erst nach Übernahme der aktualisierten View. Sie können unabhängig vom Add-on-Update verwendet werden.

1. Im vorhandenen Dashboard den YAML-/Rohkonfigurationseditor öffnen und den bisherigen Stand sichern.
2. Die aktuelle [comatalarm.yaml](comatalarm.yaml) öffnen und den vollständigen Inhalt übernehmen.
3. Wenn der Editor die **einzelne View** bearbeitet, deren gesamten Inhalt durch diese Datei ersetzen. Wenn der Editor das **gesamte Dashboard** mit `views:` zeigt, ausschließlich den Listeneintrag mit `path: comatalarm` ersetzen. Der Dateiinhalt bildet einen einzelnen Eintrag dieser Liste; Listenstrich und Einrückung entsprechend den anderen Views beibehalten. Alle übrigen Views bleiben bestehen. Existiert der Reiter noch nicht, diesen Eintrag unter `views:` ergänzen, beispielsweise unmittelbar nach Home.
4. Speichern und den ComatAlarm-Reiter neu laden. Oben muss **Diagnose teilen** mit **Kopieren**, **.txt herunterladen** und **JSON herunterladen** erscheinen.

Im Repository ist die einzelne ComatAlarm-View vorhanden. Eine vollständige, aktuelle Reno-Dashboarddatei liegt hier nicht vor; die View darf deshalb nicht als Ersatz für das gesamte vorhandene Dashboard eingefügt werden.

## Protokolle kopieren und herunterladen

- **Ereignisse & Push-Versand** öffnen. Die beiden Listen haben jeweils eigene Schaltflächen **Kopieren** und **.txt herunterladen**. Ereignisse enthalten keine Push- oder Fehlerzeilen; Push-Versand enthält die erkannten Versandresultate einschließlich fehlgeschlagener oder verworfener Meldungen.
- **Fehlerprotokoll** öffnen und dort kopieren oder herunterladen. Diese Auswahl enthält Dienstfehler und fehlgeschlagenen Push-Versand.
- Für einen bestimmten Tag **Verlauf** öffnen, den Tag wählen und warten, bis dessen Daten geladen sind. Unter **Ereignisse an diesem Tag** kopieren oder herunterladen. Während eines Tageswechsels werden keine alten Daten unter dem neu gewählten Datum exportiert.
- **Diagnose teilen** oben erfasst die angezeigten Flugwerte, die letzten angezeigten Protokolle, Dienst-/API-Status, Push-Zähler und den aktuell gewählten Verlaufstag. **Kopieren** und **.txt herunterladen** liefern eine lesbare Textfassung; **JSON herunterladen** liefert dieselben freigegebenen Daten strukturiert für die technische Auswertung.

Jeder Export nennt Exportzeit, Pi-Datenstand, Empfangszeit im Dashboard, gewählten Zeitraum und Filter sowie die enthaltenen Ereigniszeiten. Zeitangaben enthalten das Datum mit Jahr, Berliner Zeit einschließlich UTC-Abstand und UTC. Eine unterbrochene Verbindung wird als letzter gespeicherter Stand kenntlich gemacht. Die allgemeinen Protokolle umfassen die letzten bis zu 150 angezeigten Einträge; die Tagesauswahl gilt nur für das Tagesjournal. Die Gesamtdiagnose benennt beide Bereiche getrennt. Ein leeres Fehlerprotokoll bedeutet, dass in dieser Auswahl kein gespeicherter Fehler vorliegt; es belegt keinen erfolgreichen Abruf jedes einzelnen Flugwerts.

**Kopieren** versucht zuerst die Browser-Zwischenablage und anschließend die verfügbare Textauswahl-Kopierfunktion. Sind beide gesperrt, erscheint ein markierbares Textfeld zum manuellen Kopieren. Der Download verwendet eine lokale UTF-8-Datei mit festem Namensmuster, zum Beispiel `comatalarm-history-2026-11-20-2026-11-20T08-30-00-000Z.txt`. Die Oberfläche bestätigt den gestarteten Download; in einer App oder einem Browser ohne Downloadfreigabe kann die Kopierfunktion verwendet werden.

Die Schaltflächen verwenden nur den bereits autorisiert geladenen Monitorstand. Sie starten keine zusätzlichen Anbieterabfragen und senden nichts an Dritte. Exportiert werden ausdrücklich freigegebene Anzeigefelder; Gerätepseudonyme bleiben erhalten, Zugangsdaten, Firebase-UIDs, APNs-Tokens, Request-Header und rohe Anbieterantworten bleiben ausgeschlossen. Eine APNs-Annahme bleibt auch im Export eine Annahme durch Apple, keine Zustell- oder Lesebestätigung des iPhones.

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

Wartend bedeutet: kein bestätigter Abflug und keine aktuelle Luftposition. Aktiv umfasst abgeflogene oder gelandete, noch nicht am Stand bestätigte Ankünfte. Abgeschlossen bedeutet AIBT bei Ankünften, ATD bei Abflügen ab BER, Ende des vorhandenen Ankunfts-Nachlaufs ohne AIBT oder Annullierung/Umleitung. Archivierte und abgeschlossene Kacheln heißen „Abgeschlossen · im Verlauf“. Sie enthalten nur Flugidentität, Flugplanzeiten, Parkposition/Gate, Pakete und gegebenenfalls Vor-/Folgeflug. Live-Telemetrie, Positionsalter und Registrierungs-/Gerätestatus entfallen. Push-Informationen stehen ausschließlich im Ereignisbereich. Noch laufende heutige Flüge werden nicht zusätzlich als abgeschlossen im Verlauf dargestellt.

Frontend-Regressionsprüfungen: `node homeassistant/dashboards/test-monitor.cjs` und `node homeassistant/dashboards/test-monitor-exports.cjs` (Python mit PyYAML erforderlich). Die Exportprüfung verwendet synthetische Daten und prüft Abschnittsfilter, Berliner Tagesgrenzen, Datenalter, ausgeschlossene Zugangsdaten, Zwischenablage-Fallback, manuelle Textauswahl und UTF-8-Dateien.

Interaktionen werden zusätzlich mit der echten `button-card` im Browser geprüft: `BUTTON_CARD_BUNDLE=/pfad/button-card.js CHROMIUM_PATH=/pfad/chromium node homeassistant/dashboards/test-monitor-browser.cjs` (Playwright und PyYAML erforderlich). Der Test umfasst Maus, Tastatur, Touch, verschachtelte Flugdetails, Zustandserhalt bei Aktualisierung, Tagesauswahl, Kopieren sowie TXT-/JSON-Downloads. Der Inhaltsbereich aktiviert eigene Pointer-Ereignisse und trennt seine Bedienelemente vom Action-Handler der umgebenden Karte, damit dieser das Aufklappen oder die Textauswahl nicht verhindert. Die Prüfung umfasst auch das Hinzufügen einer Überwachung, Bestätigen/Abbrechen des Löschens und bereinigte Archivkacheln.


## Ankunftstabelle und Löschen

Die Tabelle verwendet denselben BER-Ankunftsvertrag und dieselbe Airline-Auswahl wie die App, einschließlich Eurowings. Nach Airline eingeklappte Zeilen zeigen Flugnummer, Herkunft, Plan und erwartete Ankunft. Codeshares desselben ausführenden Flugs werden zusammengefasst. Gelandete, annullierte und bereits vergangene Ankünfte werden nicht neu hinzugefügt. Eine abgelaufene Tagesliste wird niemals als aktuelle Liste ausgegeben.

Beim ersten Öffnen wird die Tagesliste einmal zentral geladen und über Neustarts gespeichert. „Tabelle aktualisieren“ erlaubt einen erneuten Abruf, frühestens nach fünf Minuten. Gleichzeitige Aufrufe teilen dieselbe Aufgabe; die gemeinsame BER-Anbietersperre wird respektiert. Fehler und der Zeitpunkt des letzten erfolgreichen Abrufs bleiben sichtbar. Es gibt keine zusätzlichen Historien-/Prognoseabfragen für alle Tabellenzeilen.

„Überwachen“ benötigt mindestens eine freigegebene, gültige Geräteregistrierung mit eingerichteter zentraler Pi-Abfrage und API-Zugang. Der Flug wird in die Pi-Watchlisten dieser Geräte aufgenommen. Ihre Alarm- und Kalenderregeln gelten weiter; ihre Pi-Registrierung wird durch diese ausdrückliche Aktion um 24 Stunden verlängert. Ein erneuter App-Abgleich erhält den Pi-Eintrag samt bereits ermittelten Flugwerten. Die App muss für die Abfragen und den Versand nicht geöffnet bleiben. Diese Aktion erstellt keinen neuen lokalen Flugdatensatz in der iOS-App; ihre Anzeige-Synchronisierung wird hier nicht geändert.

„Flug vom Pi löschen“ gibt es in jeder Flugkachel, auch im Verlauf. Nach Bestätigung werden alle Pi-Gerätekopien dieses Flugs, sein Archivdatensatz und noch nicht gesendete Meldungen entfernt. Ein persistenter Löschvermerk verhindert das Wiederanlegen durch eine alte App-Watchliste. Andere Flugtage bleiben erhalten. Aus einer aktuellen Ankunftstabelle kann der Administrator den Flug ausdrücklich erneut aufnehmen. Bereits an Apple übergebene Pushes und Einträge im separaten Diagnosejournal bleiben erhalten. Ein in der App gespeicherter Flug wird durch diese Pi-Aktion nicht aus deren lokalem Verlauf gelöscht.
