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
