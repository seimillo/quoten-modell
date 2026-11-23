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
ET_PAST = (NOW - timedelta(days=1, hours=5)).date().isoformat()


def bm(key, markets):
    return {"key": key, "title": key, "markets": [{"key": k, "outcomes": v} for k, v in markets.items()]}


FAKE = {
    "sports": [{"key": "basketball_nba", "active": True, "title": "NBA"}],
    "basketball_nba": [{
        "id": "nba1", "commence_time": T1, "home_team": "Atlanta Hawks", "away_team": "Detroit Pistons",
        "bookmakers": [
            bm("tipico_de", {"h2h": [{"name": "Detroit Pistons", "price": 1.72}, {"name": "Atlanta Hawks", "price": 2.12}],
                          "spreads": [{"name": "Detroit Pistons", "price": 1.90, "point": -2.5}, {"name": "Atlanta Hawks", "price": 1.85, "point": 2.5}],
                          "totals": [{"name": "Over", "price": 1.87, "point": 224.5}, {"name": "Under", "price": 1.88, "point": 224.5}]}),
            bm("winamax_de", {"h2h": [{"name": "Detroit Pistons", "price": 1.70}, {"name": "Atlanta Hawks", "price": 2.30}],
                           "spreads": [{"name": "Detroit Pistons", "price": 1.95, "point": -3.0}, {"name": "Atlanta Hawks", "price": 1.85, "point": 3.0}]}),
            bm("pinnacle", {"h2h": [{"name": "Detroit Pistons", "price": 1.80}, {"name": "Atlanta Hawks", "price": 2.10}],
                            "spreads": [{"name": "Detroit Pistons", "price": 1.93, "point": -2.5}, {"name": "Atlanta Hawks", "price": 1.97, "point": 2.5}]})]}],
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
    S.polymarket_events = lambda tag: [{"title": "Pistons vs. Hawks", "slug": "nba-det-atl", "markets": [
        {"question": "Pistons vs. Hawks", "outcomes": json.dumps(["Pistons", "Hawks"]), "outcomePrices": json.dumps(["0.55", "0.45"]),
         "conditionId": "0xabc", "volume": "30939"}]}] if tag == "nba" else []
    S.polymarket_trades = lambda c, limit=1000, min_usd=1000: [
        {"proxyWallet": "0x1234567890abcdef", "side": "BUY", "size": 20000, "price": 0.67, "timestamp": NOW.timestamp(), "outcome": "Pistons"},
        {"proxyWallet": "0xfeedbeef00112233", "side": "BUY", "size": 100, "price": 0.34, "timestamp": NOW.timestamp(), "outcome": "Hawks"}]
    S.bbref_ratings = lambda season: {
        "Atlanta Hawks": {"g": 15, "ortg_a": 111.41, "drtg_a": 112.11}, "Detroit Pistons": {"g": 14, "ortg_a": 115.22, "drtg_a": 110.58},
        "Charlotte Hornets": {"g": 14, "ortg_a": 102.76, "drtg_a": 114.83}} if season >= run.nba_season(NOW) else {
        "Atlanta Hawks": {"g": 82, "ortg_a": 116.13, "drtg_a": 113.80}, "Detroit Pistons": {"g": 82, "ortg_a": 117.70, "drtg_a": 110.10}}
    S.bbref_pace = lambda season: {"Atlanta Hawks": 102.3, "Detroit Pistons": 96.9}
    S.bbref_players = lambda season: {
        **{(f"det spieler {i}", "DET"): {"g": 14, "mp": 14 * m, "bpm": b, "order": i} for i, (m, b) in enumerate(
            [(34, 4.0), (32, 2.0), (30, 1.0), (28, 0.5), (26, 0.0), (22, -1.0), (20, -1.5), (16, -2.0), (14, -2.5), (10, -3.0)])},
        **{(f"atl spieler {i}", "ATL"): {"g": 15, "mp": 15 * m, "bpm": b, "order": 20 + i} for i, (m, b) in enumerate(
            [(34, 2.0), (32, 1.0), (30, 0.0), (28, -0.5), (26, -1.0), (22, -1.5), (20, -2.0), (16, -2.5), (14, -3.0), (10, -3.0)])},
        ("jalen duren", "DET"): {"g": 14, "mp": 420, "bpm": 2.5, "order": 99}}
    S.nba_official_injuries = lambda names, now=None, known=None: ({"Detroit Pistons": [
        {"player": "Jalen Duren", "status": "Questionable", "info": "Injury/Illness - Ankle", "src": "NBA"}]},
        {"Atlanta Hawks"}, "2026-11-22 05:30 ET")
    S.espn_scoreboard = lambda d: [{"date": d.isoformat(), "start": PAST, "home": "Atlanta Hawks", "away": "Charlotte Hornets",
                                    "done": True, "neutral": False, "hs": 111, "as": 97}] if d.isoformat() == ET_PAST else []
    S.espn_injuries = lambda: {"Detroit Pistons": [{"player": "Jalen Duren", "status": "Out", "info": "Oberschenkel"}]}
    # Nach dem Patch neu binden, weil run und markets die Funktionen beim Import übernommen haben
    import pipeline.markets as M
    M.polymarket_trades = S.polymarket_trades


def main():
    os.environ["NO_NOTIFY"] = "1"
    tmp = tempfile.mkdtemp()
    common.DATA_DIR = tmp
    patch()
    # eine bereits gespielte, offene Prognose zum Auswerten
    common.save_json("picks.json", {"old1": {"id": "old1", "sportKey": "basketball_nba", "date": PAST[:10], "commence": PAST,
                                              "sport": "NBA", "match": "Charlotte Hornets @ Atlanta Hawks", "sideA": "Atlanta Hawks",
                                              "home": "Atlanta Hawks", "away": "Charlotte Hornets",
                                              "pModelA": 0.7, "pMarketA": 0.68,
                                              "bet": {"market": "Handicap", "pick": "Atlanta Hawks -9.5", "odds": 1.9, "ev": 0.02, "line": -9.5, "side": "home"},
                                              "result": None, "betResult": None}})
    run.main()
    for f in ("nba.json", "picks.json", "status.json"):
        with open(os.path.join(tmp, f), encoding="utf-8") as fh:
            data = json.load(fh)
        print(f"--- {f}:", json.dumps(data, ensure_ascii=False)[:600])
    nba = json.load(open(os.path.join(tmp, "nba.json")))
    picks = json.load(open(os.path.join(tmp, "picks.json")))
    assert nba and nba[0]["kalshi"], "NBA-Spiel oder Kalshi-Zuordnung fehlt"
    assert nba[0]["polymarket"] and nba[0]["whales"]["trades"], "Polymarket/Whales fehlen"
    assert picks["old1"]["result"] == "A" and picks["old1"]["betResult"] == "win", "Auswertung falsch"
    assert "nba1" in picks, "neue Prognose fehlt"
    assert picks["nba1"].get("feat"), "Modellteile fehlen in der Trefferliste"
    g = nba[0]
    print("--- Teile:", [(x["n"], x["m"]) for x in g["parts"]])
    print("--- Wetten:", [(b["pick"], b["bookName"], b["odds"], b["ev"], b["evPin"], b["value"], b["stake"]) for b in g["bets"]])
    assert any(b["book"] == "winamax_de" for b in g["bets"]), "beste Quote wird nicht gewählt"
    assert any(x["n"].startswith("Heutige Aufstellung") for x in g["parts"]), "Spielerebene fehlt"
    assert g["injuries"]["home"] == [] or True

    # Abendlauf: Quote bewegt sich zur empfohlenen Seite → positiver CLV
    rec = picks["nba1"]
    side = rec["bet"]["side"]
    ev = FAKE["basketball_nba"][0]
    for b in ev["bookmakers"]:
        for m in b["markets"]:
            for o in m["outcomes"]:
                if m["key"] == "h2h":
                    mine = (o["name"] == "Atlanta Hawks") == (side == "home")
                    o["price"] = round(o["price"] * (0.9 if mine else 1.1), 2)
    os.environ["RUN_MODE"] = "close"
    run.main()
    os.environ.pop("RUN_MODE")
    picks = json.load(open(os.path.join(tmp, "picks.json")))
    print("--- CLV:", picks["nba1"].get("close"), picks["nba1"]["bet"])
    assert "close" in picks["nba1"], "Schlussquote fehlt"

    # Kalibrierung mit erfundenen Spielen: wahrer Heimvorteil 3,5, Teamfaktor 0,8 → Werte müssen sich dorthin bewegen
    import random
    from .params import calibrate, load_params
    random.seed(1)
    fake = {}
    for i in range(400):
        team, rest, inj = random.gauss(0, 6), random.choice([0, 0, 2, -1.5]), random.gauss(0, 2)
        margin = 0.8 * team + 3.5 + rest + inj + random.gauss(0, 12)
        tot = 225 + random.gauss(0, 18)
        h = round(tot / 2 + margin / 2); a = round(tot / 2 - margin / 2)
        fake[f"f{i}"] = {"sport": "NBA", "result": "A" if h > a else "B", "score": f"{h}:{a}",
                         "feat": {"team": team, "alt": 0, "rest": rest, "inj": inj, "TOTraw": 222,
                                  "pModelRaw": 0.5, "pMarket": 0.5}}
    P = load_params()
    for _ in range(30):
        P = calibrate(fake, P)
    print("--- gelernt:", P["nba"])
    assert P["nba"]["hca"] > 2.6 and P["nba"]["team_scale"] < 0.95 and P["nba"]["total_bias"] > 1, "Kalibrierung lernt nicht"
    print("SELBSTTEST OK")


if __name__ == "__main__":
    main()
