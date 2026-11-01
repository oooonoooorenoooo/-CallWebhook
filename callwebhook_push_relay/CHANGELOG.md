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
