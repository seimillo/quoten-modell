"""Trefferliste: Prognosen bis zum Spielbeginn aktualisieren, danach einfrieren und mit dem Ergebnis auswerten."""
from .common import log, now_utc, parse_time


def update_picks(picks, items):
    """Speichert/aktualisiert Prognosen für Spiele, die noch nicht begonnen haben."""
    now = now_utc()
    for it in items:
        if parse_time(it["commence"]) <= now:
            continue
        rec = picks.get(it["id"], {})
        if rec.get("result"):
            continue
        b = it["best"]
        rec.update({
            "id": it["id"], "sportKey": it.get("sportKey"), "date": it["commence"][:10], "commence": it["commence"], "sport": it["sport"],
            "match": it["pickLabel"], "sideA": it["sideA"],
            "home": it.get("home"), "away": it.get("away"), "a": it.get("a"), "b": it.get("b"),
            "pModelA": it.get("pFinalHome", it.get("pFinalA")), "pMarketA": it.get("pMarketHome", it.get("pMarketA")),
            "bet": {k: b.get(k) for k in ("market", "pick", "odds", "ev", "line", "side")},
            "result": None, "betResult": None, "score": None,
        })
        picks[it["id"]] = rec


def _bet_result(rec, score_a_side, score_b_side, sport):
    """score_a_side = Punkte/Sätze von sideA (NBA: Heim), score_b_side = Gegner."""
    bet = rec.get("bet") or {}
    m = bet.get("market")
    if m == "Sieg":
        if sport == "NBA":
            won_home = score_a_side > score_b_side
            return "win" if (bet.get("side") == "home") == won_home else "loss"
        won_a = score_a_side > score_b_side
        return "win" if (bet.get("side") == "A") == won_a else "loss"
    if m == "Handicap":
        margin_home = score_a_side - score_b_side
        line = float(bet.get("line") or 0)
        val = (margin_home + line) if bet.get("side") == "home" else (-margin_home + line)
        return "push" if val == 0 else ("win" if val > 0 else "loss")
    if m == "Gesamtpunkte":
        total = score_a_side + score_b_side
        line = float(bet.get("line") or 0)
        if total == line:
            return "push"
        return "win" if (total > line) == (bet.get("side") == "over") else "loss"
    return None


def settle(picks, scores_by_id):
    """scores_by_id: {event_id: {Name: Punkte}} aus dem Ergebnis-Endpunkt der Odds API."""
    n = 0
    for pid, rec in picks.items():
        if rec.get("result") or pid not in scores_by_id:
            continue
        sc = scores_by_id[pid]
        a = sc.get(rec["sideA"])
        other = [v for k, v in sc.items() if k != rec["sideA"]]
        if a is None or not other:
            continue
        b = other[0]
        rec["result"] = "A" if a > b else "B"
        rec["score"] = f"{a:g}:{b:g}"
        rec["betResult"] = _bet_result(rec, a, b, rec["sport"])
        n += 1
    log(f"Trefferliste: {n} Prognosen ausgewertet")


def stats(picks):
    done = [r for r in picks.values() if r.get("result")]
    def brier(key):
        if not done:
            return None
        return sum(((r.get(key) or 0) - (1 if r["result"] == "A" else 0)) ** 2 for r in done) / len(done)
    vb = [r for r in picks.values() if r.get("betResult") and (r.get("bet") or {}).get("ev", 0) > 0]
    profit = sum(10 * (r["bet"]["odds"] - 1) if r["betResult"] == "win" else -10 if r["betResult"] == "loss" else 0 for r in vb)
    return {"total": len(picks), "settled": len(done), "brierModel": brier("pModelA"), "brierMarket": brier("pMarketA"),
            "valueBets": len(vb), "profit": round(profit, 2), "roi": round(profit / (10 * len(vb)), 4) if vb else None}
