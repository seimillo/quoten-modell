"""Offline-Test: ersetzt alle Datenquellen durch Beispieldaten und lässt die komplette Pipeline laufen.
Aufruf: python -m pipeline.selftest"""
import json
import os
import tempfile
from datetime import timedelta

from . import common, run
from . import sources as S

NOW = common.now_utc()
T1 = (NOW + timedelta(hours=6)).isoformat().replace("+00:00", "Z")
PAST = (NOW - timedelta(days=1)).isoformat().replace("+00:00", "Z")


def bm(key, markets):
    return {"key": key, "title": key, "markets": [{"key": k, "outcomes": v} for k, v in markets.items()]}


FAKE = {
    "sports": [{"key": "basketball_nba", "active": True, "title": "NBA"},
               {"key": "tennis_atp_finals", "active": True, "title": "ATP Finals"}],
    "basketball_nba": [{
        "id": "nba1", "commence_time": T1, "home_team": "Atlanta Hawks", "away_team": "Detroit Pistons",
        "bookmakers": [
            bm("bet365", {"h2h": [{"name": "Detroit Pistons", "price": 1.72}, {"name": "Atlanta Hawks", "price": 2.12}],
                          "spreads": [{"name": "Detroit Pistons", "price": 1.90, "point": -2.5}, {"name": "Atlanta Hawks", "price": 1.85, "point": 2.5}],
                          "totals": [{"name": "Over", "price": 1.87, "point": 224.5}, {"name": "Under", "price": 1.88, "point": 224.5}]}),
            bm("pinnacle", {"h2h": [{"name": "Detroit Pistons", "price": 1.80}, {"name": "Atlanta Hawks", "price": 2.10}]})]}],
    "tennis_atp_finals": [{
        "id": "ten1", "commence_time": T1, "home_team": "Carlos Alcaraz", "away_team": "Alexander Zverev",
        "bookmakers": [bm("bet365", {"h2h": [{"name": "Carlos Alcaraz", "price": 1.41}, {"name": "Alexander Zverev", "price": 2.75}]})]}],
    "scores_nba": [{"id": "old1", "completed": True, "commence_time": PAST, "home_team": "Atlanta Hawks", "away_team": "Charlotte Hornets",
                    "scores": [{"name": "Atlanta Hawks", "score": "111"}, {"name": "Charlotte Hornets", "score": "97"}]}],
}


def patch():
    S.odds_sports = lambda: FAKE["sports"]
    S.odds_events = lambda k, m="h2h": FAKE.get(k, [])
    S.odds_scores = lambda k, d=3: FAKE["scores_nba"] if k == "basketball_nba" else []
    S.kalshi_events = lambda s: [{"ticker": "KXNBAGAME-26NOV22DETATL", "title": "Detroit vs Atlanta", "markets": [
        {"ticker": "KXNBAGAME-26NOV22DETATL-DET", "title": "Detroit", "price": 54, "volume": 100000},
        {"ticker": "KXNBAGAME-26NOV22DETATL-ATL", "title": "Atlanta", "price": 46, "volume": 96477}]}]
    S.polymarket_events = lambda tag: [{"title": "Zverev vs. Alcaraz", "slug": "atp-zverev-alcaraz", "markets": [
        {"question": "Zverev vs. Alcaraz", "outcomes": json.dumps(["Zverev", "Alcaraz"]), "outcomePrices": json.dumps(["0.34", "0.67"]),
         "conditionId": "0xabc", "volume": "30939"}]}] if tag == "tennis" else []
    S.polymarket_trades = lambda c, limit=500: [
        {"proxyWallet": "0x1234567890abcdef", "side": "BUY", "size": 20000, "price": 0.67, "timestamp": NOW.timestamp(), "outcome": "Alcaraz"},
        {"proxyWallet": "0xfeedbeef00112233", "side": "BUY", "size": 100, "price": 0.34, "timestamp": NOW.timestamp(), "outcome": "Zverev"}]
    S.tennis_elo = lambda tour: {"Carlos Alcaraz": {"elo": 2194.9, "hElo": 2142.2, "cElo": None, "gElo": None},
                                 "Alexander Zverev": {"elo": 2089.6, "hElo": 2030.1, "cElo": None, "gElo": None}} if tour == "atp" else {}
    S.bbref_ratings = lambda season: {
        "Atlanta Hawks": {"g": 15, "ortg_a": 111.41, "drtg_a": 112.11}, "Detroit Pistons": {"g": 14, "ortg_a": 115.22, "drtg_a": 110.58},
        "Charlotte Hornets": {"g": 14, "ortg_a": 102.76, "drtg_a": 114.83}} if season >= run.nba_season(NOW) else {
        "Atlanta Hawks": {"g": 82, "ortg_a": 116.13, "drtg_a": 113.80}, "Detroit Pistons": {"g": 82, "ortg_a": 117.70, "drtg_a": 110.10}}
    S.bbref_pace = lambda season: {"Atlanta Hawks": 102.3, "Detroit Pistons": 96.9}
    S.espn_injuries = lambda: {"Detroit Pistons": [{"player": "Jalen Duren", "status": "Out", "info": "Oberschenkel"}]}
    # Nach dem Patch neu binden, weil run und markets die Funktionen beim Import übernommen haben
    import pipeline.markets as M
    M.polymarket_trades = S.polymarket_trades


def main():
    tmp = tempfile.mkdtemp()
    common.DATA_DIR = tmp
    patch()
    # eine bereits gespielte, offene Prognose zum Auswerten
    common.save_json("picks.json", {"old1": {"id": "old1", "sportKey": "basketball_nba", "date": PAST[:10], "commence": PAST,
                                              "sport": "NBA", "match": "Charlotte Hornets @ Atlanta Hawks", "sideA": "Atlanta Hawks",
                                              "pModelA": 0.7, "pMarketA": 0.68,
                                              "bet": {"market": "Handicap", "pick": "Atlanta Hawks -9.5", "odds": 1.9, "ev": 0.02, "line": -9.5, "side": "home"},
                                              "result": None, "betResult": None}})
    run.main()
    for f in ("nba.json", "tennis.json", "picks.json", "status.json"):
        with open(os.path.join(tmp, f), encoding="utf-8") as fh:
            data = json.load(fh)
        print(f"--- {f}:", json.dumps(data, ensure_ascii=False)[:600])
    nba = json.load(open(os.path.join(tmp, "nba.json")))
    ten = json.load(open(os.path.join(tmp, "tennis.json")))
    picks = json.load(open(os.path.join(tmp, "picks.json")))
    assert nba and nba[0]["kalshi"], "NBA-Spiel oder Kalshi-Zuordnung fehlt"
    assert ten and ten[0]["polymarket"] and ten[0]["whales"]["trades"], "Tennis-Polymarket/Whales fehlen"
    assert picks["old1"]["result"] == "A" and picks["old1"]["betResult"] == "win", "Auswertung falsch"
    assert "nba1" in picks and "ten1" in picks, "neue Prognosen fehlen"
    print("SELBSTTEST OK")


if __name__ == "__main__":
    main()
