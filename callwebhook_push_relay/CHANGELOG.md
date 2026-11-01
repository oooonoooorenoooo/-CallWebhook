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
