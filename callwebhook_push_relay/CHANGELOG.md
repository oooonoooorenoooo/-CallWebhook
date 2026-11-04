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

