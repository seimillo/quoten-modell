# Quoten-Modell · Tennis & NBA

Ein Lern- und Analyseprojekt: Ein Python-Programm holt jeden Tag Quoten, Statistiken und Prognosemarkt-Daten,
rechnet für jedes Tennis-Match und NBA-Spiel eine eigene Wahrscheinlichkeit aus und vergleicht sie mit den
Buchmacher-Quoten (Erwartungswert). Eine Trefferliste prüft im Nachhinein, ob das Modell genauer war als der Markt.

**Keine Wettempfehlung. Wetten ist erst ab 18 erlaubt. Hilfe: check-dein-spiel.de · 0800 1 37 27 00**

## Was automatisch passiert
Jeden Tag um 12:17 Uhr (Winterzeit) läuft `.github/workflows/daily.yml`:
1. Quoten von Tipico, Winamax, William Hill & Co. sowie Pinnacle und Betfair (bet365 gibt es bei der Schnittstelle nicht) über **The Odds API**
2. Elo-Werte von **Tennis Abstract**, Team-Ratings und Tempo von **Basketball-Reference**, Verletzungen von **ESPN**
3. Preise von **Kalshi** und **Polymarket**, dazu Großeinsätze ab 1.000 $ auf Polymarket („Großes Geld“)
4. Modelle rechnen, Prognosen speichern, vergangene Spiele auswerten
5. Ergebnisse landen in `docs/data/` – die Website `docs/index.html` zeigt sie an

## Einrichtung (einmalig)
1. **API-Schlüssel eintragen:** Repository → *Settings* → *Secrets and variables* → *Actions* → *New repository secret*.
   Name: `ODDS_API_KEY`, Wert: dein Schlüssel von the-odds-api.com.
2. **Website einschalten:** *Settings* → *Pages* → bei *Source* „Deploy from a branch“, Branch `main`, Ordner `/docs` → *Save*.
   Die Adresse ist dann `https://seimillo.github.io/quoten-modell/`.
3. **Erster Lauf:** Reiter *Actions* → „Tägliches Update“ → *Run workflow*.

## Lokal testen
```bash
pip install -r requirements.txt
python -m pipeline.selftest          # Offline-Test mit Beispieldaten
ODDS_API_KEY=... python -m pipeline.run
```

## Aufbau
| Datei | Aufgabe |
|---|---|
| `pipeline/sources.py` | alle Datenquellen (fällt eine aus, läuft der Rest weiter) |
| `pipeline/tennis.py` | Tennis-Modell (Belag-Elo, Best of 5, Quotenbewegung, Markt-Anker) |
| `pipeline/nba.py` | NBA-Modell (Ratings, Tempo, Heimvorteil, Ruhe & Reise, Markt-Anker) |
| `pipeline/markets.py` | Kalshi/Polymarket zuordnen, Großeinsätze finden |
| `pipeline/tracker.py` | Trefferliste und Auswertung |
| `pipeline/run.py` | Ablauf der täglichen Pipeline |

## API-Credits
Der Gratis-Plan von The Odds API hat 500 Credits pro Monat. Ein Lauf braucht etwa 10–15 Credits
(höchstens 4 Tennis-Turniere, NBA mit drei Märkten, Ergebnisse). Der Stand steht oben auf der Website.
Sinkt das Guthaben unter 15, überspringt die Pipeline weitere Quoten-Abfragen.

## Grenzen
- Challenger-Turniere sind bei The Odds API nicht enthalten.
- NBA-Verletzungen werden über den BPM-Wert der Spieler grob eingerechnet; Kaderwechsel und Minutenverteilung kennt das Modell nicht.
- Die Gewichte im Modell sind begründete Schätzungen und noch nicht an alten Spielen getestet.
