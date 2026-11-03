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

