## 1.2.33

- Die aktualisierte Home-Assistant-Ansicht kann Ereignisse, Push-Versand und Fehlerprotokoll kopieren und als Textdatei herunterladen. Die Dashboard-View muss zusätzlich zum Add-on aktualisiert werden; gespeicherte Home-Assistant-Dashboards werden nicht automatisch ersetzt.
- Der Ankunftsabgleich protokolliert auch erfolgreiche Ereignisantworten, aus denen keine passende AIBT übernommen wurde. Abfragezeitpunkt, Übernahme, Anbieterfehler und Abfragepausen bleiben unterscheidbar; Rohantworten und Zugangsdaten gelangen nicht ins Diagnosejournal.
- Priorisiert den AIBT-Ereignisabgleich für gelandete Flüge vor allgemeinen Flugplanabfragen. Der schnelle Pfad arbeitet im 15-Sekunden-Arbeitszyklus und bündelt bis zu 15 echte Flug-IDs pro Ereignisabfrage. Zeit, Ereigniskoordinate, Stand und Push werden in demselben Verarbeitungsschritt weitergegeben.
- Ergänzende BER-, FlightStats- und FlightAware-Abfragen sowie der periodische API-Statuscheck halten den Alarmdurchlauf nicht mehr während ihrer Netzwerkwartezeit auf. Fertige Flugplanwerte werden weiterhin durch denselben Arbeitszyklus mit erneuter Freigabe- und Zustandsprüfung übernommen.
- Behebt einen blockierten Drei-Minuten-Voralarm: Eine gültige FR24-Liveantwort muss kein zusätzliches `on_ground`-Feld enthalten. Explizite Bodenmeldungen werden weiterhin ausgeschlossen; Frische, Höhe, Geschwindigkeit, Kurs und Sinkflug bleiben maßgeblich.
- Plausible Endanflüge erhalten einen gebündelten Live-Abgleich alle 30 Sekunden, damit der bisherige 60-Sekunden-Cache das kurze Warnfenster nicht überspringt. Das gemeinsame Limit von acht FR24-HTTP-Anfragen pro Minute bleibt wirksam.
- Schließt die Lücke zwischen bestätigter Landung und noch nicht gespeicherter AIBT. Das Ende der regelmäßigen Abfragen nach 25 Minuten beendet die gezielte Nachholung des Ankunftsereignisses nicht mehr. Sie läuft mit Warteabständen bis 24 Stunden nach ATA weiter.
- Auch bereits archivierte Ankünfte mit ATA, aber ohne AIBT können das tatsächliche FR24-Ankunftsereignis samt Koordinate erhalten. Bereits vorhandene gemeinsame Überwachungen bleiben dafür auf dem Pi erhalten.
- Die passende iPhone-App kann einen eigenen, vom Pi ausdrücklich unterstützten Reparaturauftrag für solche Verlaufseinträge übergeben. Dieser Auftrag erzeugt keine neue aktive Überwachung und spielt keine früheren Alarme erneut ab. Gelöschte Einträge und Gerätefreigaben werden weiterhin geprüft.
- Ein normaler Pi-Flug ohne AIBT wird auf dem iPhone nicht allein wegen des lokalen 25-Minuten-Zeitlimits als ausdrücklicher Pi-Archiveintrag behandelt. Die Anzeige unterscheidet eine noch offene Ankunftsnachholung von einer bestätigten AIBT.
- Der Nachholpfad verwendet die tatsächliche FR24-Flug-ID, Flugtag und Strecke. AIBT wird nicht aus EIBT, Stillstand oder einem Zeitlimit abgeleitet. Das gemeinsame Minutenbudget, Anbietersperren und die Prüfung echter Standplatzkoordinaten bleiben erhalten.

## 1.2.32

- Korrigiert die Verarbeitung der offiziellen FR24-Flugspurantwort. `flight-tracks` liefert eine Liste an der JSON-Wurzel; vorhandene AIBT-Koordinaten aus dieser Antwort werden nicht mehr als ungültiges Antwortformat verworfen.
- AIBT-Zeit und Koordinate werden gemeinsam aus dem passenden Ereignis übernommen. Ein neueres Ankunftsereignis darf keine Koordinate eines älteren Ereignisses erben. Die gemessene, stationäre Ereignisposition hat Vorrang vor ergänzenden Gate-Koordinaten.
- Fehlende Koordinaten werden nach sechs frühen Leerantworten innerhalb des bestehenden 24-Stunden-Zeitraums mit zunehmenden Abständen weiter nachgeprüft. Anbieter-Sperren und das gemeinsame Minutenbudget bleiben wirksam.
- Bereits gespeicherte Archivankünfte aus belegter gemeinsamer Überwachung können auf dem Pi begrenzt nachgebessert werden. Der Verlauf wird dabei nicht wieder zur aktiven Flugüberwachung und erzeugt keine wiederholten Alarme. Freigaben und Löschungen werden vor und nach dem Abruf berücksichtigt.
- Alte, mit dem vorhandenen BER-Index nicht zuordenbare Koordinaten erhalten eine begrenzte Prüfung mit der korrigierten Ereignisauswertung. Standplätze werden weiterhin anhand echter Koordinaten geprüft.
- Die passende iPhone-Version verhindert, dass ein erfolgloser früherer Reparaturversuch später eintreffende gültige Koordinaten und bestätigte Standplätze verdrängt. Die Anzeige unterscheidet fehlende Koordinaten, gesperrte Standplatzdaten und eine noch offene Zuordnung.

## 1.2.31

- Abgeschlossene gemeinsame und über die Pi-Oberfläche übernommene Flüge erhalten einen eigenen, authentifizierten Verlaufsabgleich für die iPhone-App. Die Übernahme funktioniert auch nach Ablauf der aktiven Überwachung.
- Der App-Verlauf bewahrt die ursprünglichen Eintrags-IDs, den Flugtag, die konkrete Flug-ID und vorhandene tatsächliche Zeiten sowie Standnachweise. Derselbe Flug wird beim erneuten Abgleich nicht wieder aktiv und löst keine erneuten Pushes aus.
- Bereits vorhandene, sicher zuordenbare Pi-Daten werden nachgetragen. Der Export bleibt auf den gespeicherten aktuellen Berliner Kalendermonat begrenzt; historische Diagnoseeinträge ohne belegte gemeinsame Herkunft werden nicht pauschal freigegeben.
- Der seitenweise Abruf verwendet ausschließlich gespeicherte Daten, prüft die Gerätefreigabe und erkennt Änderungen zwischen zwei Seiten. Zusätzliche Anbieterabfragen sind dafür nicht erforderlich.
- Löschungen gelten über den gesamten auf dem Pi gespeicherten Archivzeitraum. Alias-IDs verhindern das Wiederauftauchen gelöschter Einträge unter einer neuen Archiv-ID.
- Add-on und passende iOS-Version aktualisieren. Die App übernimmt beim nächsten Abgleich auch abgeschlossene Pi-Flüge direkt in den Verlauf.

## 1.2.30

- Fällige FR24-Abfragen mehrerer zentral überwachter Flüge werden pro Zugang und identischem Abfragefenster gebündelt. Damit verbrauchen fünf, acht oder zwölf verschiedene Flüge nicht mehr jeweils zwei einzelne HTTP-Anfragen und verdrängen sich nicht gegenseitig aus dem Minutenbudget.
- Der bestehende Takt und das Limit von acht FR24-HTTP-Anfragen pro Minute bleiben erhalten. Anbieter-Sperren und Wiederholungsfristen gelten weiterhin. Gebündelte Antworten werden mit den bisherigen Flug-ID-, Strecken- und Tagesprüfungen jedem einzelnen Flug zugeordnet.
- Keine vorsorglichen Zusatzabfragen: Ein Batch wird erst ausgeführt, wenn ein fälliger Flug die entsprechende Abfrage tatsächlich benötigt. Fehler und Teilantworten lösen keine Kette von Einzelabfragen aus.
- Neue Regressionstests verwenden den tatsächlichen Provider mit simulierten HTTP-Antworten für fünf, acht und zwölf unterschiedliche Flüge. Die Bündelung senkt HTTP-Anfragen; FR24-Credits richten sich weiterhin nach den zurückgegebenen Flugdaten.
- Enthält die gemeinsame Hintergrundüberwachung und den späten Drei-Minuten-Voralarm aus 1.2.29. Das Add-on muss auf dem Pi aktualisiert werden.

## 1.2.29

- Gemeinsam freigegebene Geräte erhalten neue Kollegen-Überwachungen direkt vom Pi, auch wenn ihre App geschlossen ist. Bestehende Alarm- und Kalenderregeln gelten weiterhin je Gerät.
- Zentrale, weiterhin freigegebene Registrierungen werden vom Pi verlängert. 24 Stunden ohne App-Kontakt löschen keine aktive Überwachung mehr; fehlende Freigabe pausiert den Dienst, Widerruf bleibt wirksam.
- Neue Überwachungen wecken den Arbeitszyklus sofort. Bereits erkannte Ereignisse werden vor weiteren Geräteabfragen versendet; ein fehlerhaftes Gerät blockiert andere nicht.
- Neuer standardmäßig aktiver Voralarm „Landung in etwa 3 Minuten“: zwei frische, konsistente Live-Beobachtungen mit plausibler ETA, Kurs, Entfernung, relativer Höhe und Sinkrate. Keine Warnung allein aus Flugplanzeit, veralteter Position oder bestätigter Landung. Unbekannte Zielflughäfen und fehlende Live-ETA lösen keinen Voralarm aus.
- Die Voralarmgrenze wird alle 15 Sekunden anhand vorhandener frischer Daten geprüft, ohne zusätzliche Anbieterabfrage. Ausstehende Warnungen verfallen nach spätestens 30 Sekunden; ETA-Änderung, Durchstarten oder Landung verhindern einen überholten Versand. Kein erneuter Voralarm für denselben Flug nach Neustart.
- Explizite gemeinsame Fluglöschungen samt Wiederanlage-Zeitstempel verhindern das Wiederauftauchen durch alte App-Snapshots. Autorisierte Tester können ihren APNs-Token ohne geöffnete Administrator-App erneuern.
- Kalenderfenster reichen mit der aktualisierten App 14 Tage. Abgelaufene Kalenderdaten bleiben erkennbar und geben Alarme nicht eigenmächtig frei. Bestehende Schichtregeln bleiben erhalten.
- Nebenläufige Geräte-/Fluglöschungen, Tokenwechsel und Pi-Aktualisierungen werden nach asynchronen Freigabeprüfungen erneut geprüft. Neue Regressionstests für geschlossene Kollegen-Apps, Freigaben, Löschungen, Wiederholungen und den Landealarm.
- Add-on aktualisieren; die neue iOS-Version ergänzt Alarmoption, unmittelbare Übergabe und explizite Löschungen. APNs-Annahme bleibt keine Zustellbestätigung des Endgeräts.

## 1.2.28

- AIBT beendet den zentralen Koordinatenabruf nicht mehr vorzeitig. Fehlende AIBT-Koordinaten werden ohne App-Kennzeichen automatisch nachgeladen.
- Wiederherstellung jüngerer AIBT-Ereignisse bis 24 Stunden: sechs Datenversuche, mindestens fünf Minuten Abstand. Anbieterfehler verbrauchen keinen Datenversuch; Sperrfristen bleiben wirksam.
- Wenn die historische Momentaufnahme fehlt, wird zusätzlich die Spur der exakten FR24-Flug-ID geprüft. Nur zeitnahe, stationäre BER-Punkte werden übernommen; keine beliebige Live- oder Rollposition.
- Historische Momentaufnahme nicht mehr auf einen einzelnen Treffer begrenzt. Die FR24-Flug-ID bleibt zwingend geprüft.
- AIBT-Abrufstatus im Ereignisprotokoll, keine erneute AIBT-Alarmierung durch die Reparatur. Folgeflugüberwachung bleibt unabhängig von der Koordinaten-Wartefrist.

## 1.2.27

- BER-Ankunftstabelle täglich um 05:00 Uhr Europe/Berlin zentral laden, mit Sommer-/Winterzeit und Nachholen nach einem Neustart.
- Eigener Arbeitszyklus unabhängig von geöffneten Apps, Dashboard und aktiven Flugüberwachungen; gespeicherte Versuche und BER-Sperrfristen verhindern Abfrageschleifen.
- Neue iOS-App kann die vollständige Liste in begrenzten, revisionsgeprüften Seiten übernehmen und sich für stille Daten-Pushes anmelden.
- Versandstatus und Wiederholungen bleiben nach Pi-Neustarts erhalten. Die 24-Stunden-Frist der Flugüberwachung beschränkt diese Daten-Synchronisierung nicht.
- Daten-Pushes enthalten nur Tag und Version. Die Tabelle wird über die vorhandene authentifizierte HTTPS-Verbindung geladen. iOS kann Hintergrundzustellung verzögern; die App gleicht beim Öffnen ab.

## 1.2.26

- Verlaufskacheln: „Abgeschlossen · im Verlauf“, ohne Live-Bewegungswerte, Positionsalter, Registrierungsablauf oder Geräte-/Push-Details. Flugplan, Stand, Gate, Pakete und Vor-/Folgeflug bleiben sichtbar.
- Administratoren können Flüge auf dem Pi löschen, einschließlich festhängender Überwachungen. Alle Gerätekopien und noch offene Pushes werden entfernt. Ein persistenter Vermerk verhindert das Wiederanlegen durch veraltete App-Übergaben; andere Flugtage bleiben erhalten.
- BER-Ankunftstabelle mit Airline-Gruppen einschließlich Eurowings und „Überwachen“. Gemeinsamer Tagescache, manueller Refresh frühestens nach fünf Minuten, Pagination, Fehleranzeige und bestehende BER-Anbietersperre.
- Neue Überwachungen nutzen die eingerichteten Pi-Zugänge und Alarmregeln der freigegebenen, verbundenen Geräte. Sie überstehen App-Abgleiche und laufen bis zu 24 Stunden ohne geöffnete App.
- Flugaktionen nur über HA-Administratorkonto und internen Monitor; keine Freigabe im öffentlichen Proxy. Bootstrap 1.2.9 einmal starten (Backend 21 / Integration 1.2.17), danach aktualisierte Dashboard-YAML übernehmen. Kein TestFlight-/App-Update.

## 1.2.25

- Dashboard lädt Antworten sofort sichtbar, ohne Reiterwechsel; Zeitüberschreitung und Verbindungsfehler werden angezeigt und automatisch erneut versucht.
- API-Status, wartende/aktive/abgeschlossene Flüge, einzelne Flugkacheln, Ereignisse, Fehler und Geräte sind standardmäßig eingeklappt. Geöffnete Bereiche bleiben bei Aktualisierung erhalten.
- Neuer Monatsverlauf mit Tagesauswahl (Europe/Berlin). Der Pi speichert den letzten bekannten Flugstand unabhängig vom geöffneten Dashboard, vor Entfernung und Ablauf einer Überwachung; Neustarts erhalten den Verlauf. Beim Monatswechsel wird der alte Flugverlauf entfernt. Bereits gelöschte frühere Flüge können nicht rekonstruiert werden.
- Tagesabrufe sind rein lokal, weiterhin nur für HA-Administratoren. Keine zusätzlichen Anbieterabfragen. Bestehendes Fehlerjournal bleibt auf 1000 Einträge begrenzt.
- Für die Tagesauswahl Bootstrap 1.2.8 einmal starten (Backend 20 / Integration 1.2.16) und das aktualisierte Dashboard übernehmen. Kein App-Update.

## 1.2.24

- Neuer schreibgeschützter ComatAlarm-Monitor für das Home-Assistant-Dashboard: gemeinsamer API-Status, alle übergebenen Flüge, Messzeit/Datenalter, Flugplan, Position, Pakete, Vor-/Folgeflug und Standplatzprognose.
- Pi-Zyklusbeginn und -abschluss sowie letzter Datenabruf jeder App werden getrennt angezeigt. Der Monitor löst keine Anbieterabfragen aus und verändert keine Überwachungsregistrierung.
- Persistentes Diagnosejournal: Ereigniserkennung, APNs-Annahme/Fehlschlag und sichere Fehler aus dem Protokoll mit Uhrzeit. Bis zu 1000 Einträge über Neustarts, neueste 150 im Dashboard. Historische Logs vor dem Update werden nicht importiert. APNs-Annahme ist keine Zustellbestätigung.
- Nur Home-Assistant-Administratoren können die neue HA-Ansicht lesen. Der bestehende Einrichtungsschlüssel bleibt auf dem Server; keine Tokens, E-Mail-Adressen, Firebase-IDs oder Rohprotokolle im Browser.
- Einmalig CallWebhook Bootstrap 1.2.7 starten (installiert Backend 19 / Integration 1.2.15 und startet HA neu). Danach das ergänzte Dashboard laden. Kein ComatAlarm-App-Update erforderlich.
- Diese Version macht die laufende Überwachung sichtbar; sie behauptet keine Behebung der noch ungeklärten verspäteten Hintergrund-Pushes.

## 1.2.23

- Hintergrunddiagnose: Der Pi protokolliert alle fünf Minuten seinen Überwachungszyklus, gültige/abgelaufene Registrierungen und gespeicherte Abfrageergebnisse. Fehler des Zyklus werden nicht mehr still verschluckt; protokolliert wird nur der Fehlertyp, ohne Zugangsdaten oder Antwortinhalte.
- Fehlgeschlagene Firebase-Gerätefreigabeprüfungen werden als Ursache einer Abfragepause gespeichert und sichtbar protokolliert. Freigaben werden weiterhin geprüft; keine Umgehung bei Netzwerkfehlern.
- Erkannte Flugereignisse nennen ihr Alter und die Alarmfreigabe; erfolgreiche Übergaben an APNs nennen den Versandzeitpunkt. Damit lassen sich verspätete Erkennung und verspätete Zustellung unterscheiden. APNs-Annahme ist keine Zustellbestätigung des Telefons.
- Regressionstest: zwei Geräte, vier Stunden ohne erneuten App-Kontakt, zentrale Abfragen und jeweils einmalige Abflug-, Lande- und Standmeldungen.
- Diese Version ergänzt die Fehlerdiagnose; die Ursache eines konkreten verspäteten Pushs muss anhand des Pi-Protokolls bestätigt werden.

## 1.2.22

- BER-Flugabfrage und gemeinsame API-Statusprüfung verwenden wieder die vollständigen Anfrage-Header der App, mit eigener ComatAlarm-Pi-Kennung. Behebt die reproduzierte CloudFront-403-Antwort auf die bisherigen Standard-Anfragen.
- Flugnummern werden im BER-Format mit Leerzeichen übergeben, beispielsweise `LH 194`; Datum und Tagesgrenzen richten sich nach der Berliner Zeitzone.
- Gemeinsame Sperrfristen, Retry-After und zentrale Flugabfragen bleiben erhalten. Bei weiteren BER-Fehlern nennt das Protokoll zusätzlich, ob CloudFront erkennbar ist, ohne Antwortinhalte oder Zugangsdaten zu protokollieren.
- Kein neues ComatAlarm-App-Build und keine erneute Einrichtung erforderlich.

## 1.2.21
- Ein vollständig aus der Administrator-App übernommenes BER-Standplatzpaket hat Vorrang. Der zusätzliche Firebase-Paketimport wird automatisch deaktiviert, auch nach Neustarts und Wiederherstellung der verschlüsselten Sicherung.
- Bereits laufende Firebase-Downloads können eine inzwischen abgeschlossene App-Übernahme nicht überschreiben oder anschließend eine irreführende Entschlüsselungswarnung auslösen.
- Leere, entfernte und ausschließlich aus Leerzeichen bestehende Passphrase-Einstellungen deaktivieren den direkten Import und entfernen die bisherige private Passwortdatei. Bereits übernommene Standplätze und Lerndaten bleiben erhalten.
- Kein neues ComatAlarm-App-Build erforderlich; die Übernahme mit Build 1241 bleibt kompatibel.

## 1.2.20

- ComatAlarm: Das BER-Standplatzpaket kann direkt über das vorhandene Firebase-Dienstkonto geladen werden, unabhängig von einer geöffneten Administrator-App. Neue optionale Passwort-Option `comatalarm_ber_passphrase`.
- Unterstützt das bestehende verschlüsselte PDF mit passwortgeschütztem age-Schlüssel-Backup sowie direkt passwortverschlüsselte PDFs. Prüft Paketgröße, Datenblöcke, Prüfsummen und die bekannten Standplatztabellen; Referenzpunkte bleiben getrennt von tatsächlichen Parkpositionen.
- Import im Hintergrund beim Start, anschließend Prüfung alle sechs Stunden. Unveränderte Pakete werden nicht erneut übertragen oder entschlüsselt. Fehler führen zu begrenzten Wiederholungen und überschreiben weder Standplätze noch Lerndaten.
- Speicherung weiterhin verschlüsselt und dauerhaft im privaten Add-on-Verzeichnis. Keine Klartext-PDF, keine neuen öffentlichen Endpunkte und keine geänderten Gerätefreigaben.
- Für den direkten Paketimport ist kein App-Update erforderlich. Die Übernahme bisheriger iPhone-Lerndaten und die zentrale Prognoseanzeige benötigen weiterhin die aktualisierte ComatAlarm-App.

## 1.2.19

- Zeitstempel der vorhandenen FR24-Positionsantworten werden auch im ISO-8601-Format für Rollweg- und Referenzmerkmale verarbeitet. Damit arbeitet das zentrale Standplatzlernen mit beiden vom bisherigen Client unterstützten Zeitformaten.
- Enthält die zentrale Standplatzverwaltung und Lernmodell-Übernahme aus 1.2.18.

## 1.2.18

- Der Pi speichert das vollständige BER-Standplatzpaket mit Referenzpunkten und das Standplatz-Lernmodell verschlüsselt und dauerhaft in /data. Eine zusätzliche verschlüsselte Sicherung bleibt nach Neustarts und abgelaufenen Geräteanmeldungen erhalten.
- Die aktualisierte Administrator-App überträgt vorhandene bestätigte Lerndaten automatisch in wiederaufnehmbaren Teilen. Doppelte Ankünfte werden zusammengeführt; ein erneuter Import erhöht die Gewichtung nicht.
- Standplatzprognosen entstehen zentral aus den bereits abgefragten Gate- und Positionsdaten. Keine zusätzlichen Flugdaten-Abfragen und kein offenes iPhone für Berechnung oder Lernen erforderlich. Prognosen werden an freigegebene Geräte sowie mit den bestehenden Flugereignis-Pushes an Widgets übertragen.
- Zugriff über die serverseitig freigegebene Firebase-Geräte-ID; keine erneute Apple-, E-Mail- oder Private-Relay-Prüfung. Nur der Administrator darf Paket und bestehende Lerndaten importieren.
- App-Update erforderlich. Nach Installation des Add-ons die aktualisierte Administrator-App geöffnet lassen, bis unter Hintergrund-Push das Pi-Standplatzpaket und die übernommenen Ankünfte angezeigt werden. Die bestehende Home-Assistant-Komponente bleibt kompatibel.
- Neue Regressionstests für Übertragung, Neustart/Sicherung, gemeinsame Prognose, Lern-Deduplizierung und Geräteberechtigungen.

## 1.2.17

- ComatAlarm: Gate-Angaben aus den bereits laufenden FlightStats- und FlightAware-Abfragen wieder an die Apps übertragen. Bei einer BER-Sperrfrist stehen diese Angaben als Grundlage der Standplatzprognose bereit.
- Die Gate-Quelle bleibt erhalten; aktuelle BER-Angaben haben Vorrang vor FlightStats und FlightAware. Fehlende Gate-Werte überschreiben keine gültige Alternative.
- Ältere Gerätedaten können ein vom Pi ermitteltes Gate nicht mehr beim erneuten Anmelden der Flugüberwachung löschen. Alle Geräte nutzen weiterhin dieselbe Abfrage.
- Regressionstests für BER 403, Quellenreihenfolge, Ankunft/Abflug und geräteübergreifende Speicherung ergänzt.

## 1.2.16

- Korrigiert die Zeitparameter für FR24 und FlightAware: UTC auf ganze Sekunden im jeweiligen Anbieterformat, ohne lokale Zeitzonenoffsets und Nachkommastellen. Gilt für die gemeinsame Statusprüfung sowie Flug-, Vorflug- und Weiterflug-Abfragen.
- Die Statusprüfung verwendet ein vollständiges 24-Stunden-Fenster, auch um Mitternacht. Sommerzeitwechsel werden berücksichtigt.
- Statusprüfung und Flugüberwachung teilen sich die Sperrfristen für BER, FlightStats, FlightAware und FR24. Eine abgelehnte Abfrage wird nicht durch den jeweils anderen Abfrageweg erneut ausgelöst; Retry-After bleibt wirksam.
- HTTP 400, 401, 402, 403 und 429 werden verständlich unterschieden. Sichere Protokollzeilen nennen Anbieter, HTTP-Status, Antworttyp und Sperrdauer, ohne Zugangsdaten oder Antwortinhalte.
- Eine tatsächliche BER-Zugriffsablehnung bleibt als Fehler sichtbar. Dieses Update hebt keine anbieterseitige Sperre auf.
- Keine neue App-Version oder erneute Einrichtung erforderlich.

## 1.2.15

- Fehlende BER-Parkpositionen abgehender Flüge werden auch nach dem eigenen Abflug anhand des tatsächlichen Vorflugs weiter gesucht. Ein ATD beendet diesen Abgleich nicht mehr vorzeitig.
- Bereits vorzeitig abgeschlossene Einträge werden bei fehlenden Vorflug-Daten wieder aufgenommen. Nach Abflug erfolgen höchstens vier erfolgreiche Nachsuchen, frühestens alle 15 Minuten und nur innerhalb von 24 Stunden; bestehende API-Limits bleiben aktiv.
- Ein teilweise bekannter Vorflug ohne AIBT darf später ergänzt werden. Vorübergehende API-Fehler verbrauchen den Nachsuchzähler nicht und bleiben zeitlich begrenzt.
- Die Nachsuche fragt nur die Vorflug-Daten ab, nicht die spätere Ankunft des abgehenden Flugs. AIBT wird weiterhin ausschließlich aus dem tatsächlichen FR24-Gate-Ereignis übernommen.
- App-Update erforderlich, damit auch archivierte Abflüge mit unvollständigem Vorflug zur Nachsuche angemeldet bleiben.

## 1.2.14

- Bei Abflügen ab BER ermittelt der Pi den unmittelbar vorhergehenden Flug derselben Registrierung mit tatsächlicher Landung in BER. Keine Zuordnung nach Flugnummer minus eins.
- AIBT und GPS-Koordinate des Vorfluges liefern den BER-Standplatz. Start, Ankunft und Abschluss des eingegebenen Abfluges bleiben getrennt; der BER-Vorgang endet mit dessen ATD.
- Berücksichtigt Nachtstopps innerhalb von 48 Stunden. Flugzeugwechsel, Zwischenflüge, Umleitungen und unklare Daten führen zu keiner Übernahme eines alten Standplatzes.
- Vorflugprüfung frühestens alle fünf Minuten; bestätigte AIBT-Koordinaten werden wiederverwendet. Fehlende ältere Ereignisse erhalten höchstens zwei Wiederholungsversuche im Abstand von 15 Minuten.
- Ergebnisse werden zwischen Geräten geteilt und bei Geräte-Synchronisierung erhalten. Standplatz-Push enthält den Vorflug und die Paketzahlen; die neue App aktualisiert damit auch das Widget ohne einen falschen Ankunftsstatus.
- Für die Anzeige des Vorfluges und des Abflug-Standplatzes die zugehörige neue App installieren.

## 1.2.13

- Neue App-Versionen übergeben die laufenden Flugabfragen dauerhaft an den Pi, auch bei geöffneten Apps. Bestehende Clients behalten bis zum App-Update das bisherige Übergabeverhalten.
- Gemeinsame Flugplan- und Positionsdaten für die Geräte: FR24, BER sowie FlightStats und FlightAware. Start, Landung und AIBT werden weiterhin serverseitig ausgewertet und per APNs gemeldet.
- Pi-Ergebnisse bleiben bei erneuter Geräte-Synchronisierung erhalten. Weiterflüge werden auch nach AIBT des ankommenden Fluges überwacht.
- Fehlende AIBT-Koordinaten jüngerer Verlaufseinträge können zentral nachgeholt werden: höchstens zwei Versuche im Abstand von mindestens 15 Minuten.
- Gemeinsame Abfrageergebnisse, Anbieter-Sperrfristen, FR24-Minutenbudget und die bestehende Registrierungsgültigkeit von 24 Stunden bleiben erhalten.
- Nach dem Dienst-Update die neue App auf den beteiligten Geräten installieren und einmal öffnen.

## 1.2.12

- Gemeinsamer API-Status für FR24, FlightStats, FlightAware, BER, ADSB.lol und OpenSky statt separater Statusabfragen jedes Geräts.
- Automatische Prüfung alle 15 Minuten bei gültiger Registrierung; manuelle Prüfwünsche werden zusammengefasst und auf einen Durchlauf je Minute begrenzt.
- Ergebnis und Prüfzeitpunkt werden gespeichert und über den bestehenden Geräteabgleich verteilt. Die aktualisierte App kennzeichnet veraltete Ergebnisse.
- FR24-Minutenbudget und Anbieter-Sperrfristen werden berücksichtigt. Keine neue HA-Proxy-Route erforderlich.
- Zusätzliche API-Zugangsdaten werden von der aktualisierten App über die bestehende geschützte Verbindung übergeben und nicht an andere Geräte ausgegeben.

## 1.2.11

- Entfernt die Anzahlgrenze aktiver Flüge je Gerät. Die neue App übergibt alle aktiven Flüge innerhalb des bestehenden Zeitfensters.
- Gemeinsame Abfragen identischer Flugvorgänge und 24-Stunden-Gültigkeit bleiben erhalten.
- Bei ausgeschöpftem API-Budget erhalten noch nicht erfolgreich abgefragte Flüge im nächsten Durchlauf Vorrang; der Minutenschutz bleibt aktiv.
- Tests prüfen zwölf gemeinsame Flüge auf fünf Geräten, größere Übergaben und faire Fortsetzung nach dem Minutenlimit.

## 1.2.10

- Bis zu vier aktive Flüge je Gerät statt zwei; die 24-Stunden-Gültigkeit bleibt erhalten.
- Identische Flugvorgänge mit demselben API-Zugang werden weiterhin gemeinsam abgefragt. Unterschiedliche Flüge weiterer Geräte werden zusätzlich überwacht, ohne globale Vier-Flüge-Grenze.
- Tests decken vier Flüge auf fünf geschlossenen Geräten, zusätzliche Flüge weiterer Geräte und die Grenze inklusive abgeschlossener Flüge ab.
- Für die Übergabe von vier Flügen ist die aktualisierte ComatAlarm-App erforderlich.

## 1.2.9

- Bewahrt den gespeicherten FR24-Schlüssel, wenn eine Hintergrund-Übergabe der App einen leeren Schlüssel enthält. Damit unterbricht ein vorübergehender lokaler Lesefehler nicht die serverseitigen Flugabfragen.
- Tests sichern Flugabfrage und einmalige Push-Zustellung bei geschlossener App sowie die automatische Übernahme nach Ablauf der Vordergrund-Frist ab.
- Bestehende Einrichtung bleibt erhalten; keine neuen Zugangsdaten erforderlich.

## 1.2.8

- Apple-Testerprüfung liest die Mitglieder direkt aus den internen ComatAlarm-Testgruppen. Zusätzliche Abgleiche mit den globalen Benutzer- und Testerlisten entfallen.
- Weiterhin nur bestätigte Apple-E-Mail und echte interne Gruppenzugehörigkeit; keine Administrator-Ausnahme oder automatische Einladung.
- Unvollständige Apple-Antworten werden als Prüffehler behandelt, nicht als fehlende Mitgliedschaft.

## 1.2.7

- Prüft die bestätigte Apple-Anmeldung und bestehende interne TestFlight-Mitgliedschaft für ComatAlarm serverseitig.
- Neue optionale geheime Konfiguration `comatalarm_firebase_service_account`; ohne diese bleiben bisherige Push-Funktionen unverändert nutzbar.
- Benötigt für die neue Apple-Prüfung CallWebhook-HA-Komponente 1.2.14 (Bootstrap 1.2.6) und die neue ComatAlarm-App.

## 1.2.6

- Mit kompatibler ComatAlarm-App ein einziger Meldeweg über den Push-Dienst, auch bei geöffneter App. Ältere Apps behalten ihr bisheriges Verhalten.
- Persistente Zustellsperre je iPhone-Token und Flugereignis; gleichzeitige Wiederholungen und doppelte Geräteanmeldungen lösen keine zweite erfolgreiche Zustellung aus.
- Vordergrund-Ereignisse einschließlich bereits archiviertem AIBT werden übernommen, ohne zusätzliche Flug-API-Abfragen. Maximal zwei laufende Flugabfragen bleiben bestehen.

# 1.2.5

- ComatAlarm-Pushs enthalten bestätigte ATD-, ATA- und AIBT-Zeiten für die Widget-Aktualisierung durch die neue iOS-Mitteilungserweiterung.
- Die Erweiterung benötigt dieselbe App Group wie App und Widget. Vorherige App-Versionen empfangen weiterhin die normale Mitteilung.
- Keine zusätzlichen Flug-API-Abfragen; bestehende Alarm- und Kalender-Einstellungen bleiben wirksam.

# 1.2.4

- Parkpositions-Mitteilungen nennen IN-Pakete zum Abholen und OUT-Pakete zum Abgeben. Leere Angaben und Nullmengen entfallen.

# 1.2.3

- ComatAlarm beendet automatische Ankunftsabfragen nach 25 Minuten ohne Parkereignis. Es wird keine Parkzeit oder Parkposition erfunden.
- Bei bekanntem gelandeten Flug wird das FR24-Parkereignis direkt abgefragt; die zusätzliche Zusammenfassung entfällt.
- FR24-Minutenbudget und Retry-After-Sperren für FR24 und BER begrenzen weitere Versuche nach 403/429.

