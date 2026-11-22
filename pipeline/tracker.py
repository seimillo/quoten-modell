"""Trefferliste: Prognosen bis zum Spielbeginn aktualisieren, danach einfrieren und mit dem Ergebnis auswerten."""
from statistics import NormalDist

from .common import devig_power, log, now_utc, parse_time, phi
from .sources import SHARP, book_prices, main_book

_N = NormalDist()


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
        bet = {k: b.get(k) for k in ("market", "pick", "odds", "ev", "line", "side", "book", "bookName", "value", "stake", "evPin")}
        bet["book"] = bet.get("book") or it.get("bookKey") or (it.get("book") or {}).get("key")
        if rec.get("bet") != bet:
            rec.pop("close", None)                       # neue Empfehlung → alte Schlussquote gilt nicht mehr
        rec.update({
            "id": it["id"], "sportKey": it.get("sportKey"), "date": it["commence"][:10], "commence": it["commence"], "sport": it["sport"],
            "match": it["pickLabel"], "sideA": it["sideA"],
            "home": it.get("home"), "away": it.get("away"), "a": it.get("a"), "b": it.get("b"),
            "pModelA": it.get("pFinalHome", it.get("pFinalA")), "pMarketA": it.get("pMarketHome", it.get("pMarketA")),
            "pModelRaw": it.get("pModelHome", it.get("pModelA")), "feat": it.get("feat"),
            "bet": bet, "result": None, "betResult": None, "score": None,
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
    def is_value(r):
        b = r.get("bet") or {}
        return b["value"] if "value" in b else b.get("ev", 0) > 0     # alte Einträge: EV > 0
    vb = [r for r in picks.values() if r.get("betResult") and is_value(r)]
    ret = lambda r, st: st * (r["bet"]["odds"] - 1) if r["betResult"] == "win" else -st if r["betResult"] == "loss" else 0
    profit = sum(ret(r, 10) for r in vb)
    kb = [r for r in vb if (r.get("bet") or {}).get("stake")]
    kelly = sum(ret(r, r["bet"]["stake"]) for r in kb)            # in Anteilen der Kasse
    clv_v = [r["close"]["clv"] for r in picks.values() if r.get("close") and is_value(r)]
    clv = [r["close"]["clv"] for r in picks.values() if r.get("close")]
    return {"total": len(picks), "settled": len(done), "brierModel": brier("pModelA"), "brierMarket": brier("pMarketA"),
            "brierRaw": brier("pModelRaw") if all(r.get("pModelRaw") is not None for r in done) else None,
            "clvN": len(clv), "clvAvg": round(sum(clv) / len(clv), 4) if clv else None,
            "clvPos": round(sum(1 for c in clv if c > 0) / len(clv), 3) if clv else None,
            "calibration": calibration(picks),
            "clvValueN": len(clv_v), "clvValueAvg": round(sum(clv_v) / len(clv_v), 4) if clv_v else None,
            "valueBets": len(vb), "profit": round(profit, 2), "roi": round(profit / (10 * len(vb)), 4) if vb else None,
            "kellyBets": len(kb), "kellyGrowth": round(kelly, 4)}


# ---------------- Schlussquote (Closing Line Value) ----------------

def _fair_pair(prices, n1, n2, book):
    """De-vigte Wahrscheinlichkeit für n1 – bevorzugt Pinnacle/Betfair, sonst der Buchmacher der Wette."""
    for key in SHARP + [book]:
        out = prices.get(key) or []
        o1 = next((o for o in out if o["name"] == n1), None)
        o2 = next((o for o in out if o["name"] == n2), None)
        if o1 and o2:
            return devig_power(o1["price"], o2["price"]), o1, key
    return None, None, None


def _z(p):
    return _N.inv_cdf(min(0.999, max(0.001, p)))


def close_prob(rec, ev, sd_margin=12.5, sd_total=18.0):
    """Faire Schluss-Wahrscheinlichkeit für genau die empfohlene Wette (bei verschobener Linie umgerechnet)."""
    bet = rec.get("bet") or {}
    book = bet.get("book")
    m, side = bet.get("market"), bet.get("side")
    if rec.get("sport") == "NBA":
        home, away = ev["home_team"], ev["away_team"]
        if m == "Sieg":
            p, _, _ = _fair_pair(book_prices(ev, "h2h"), home, away, book)
            return None if p is None else (p if side == "home" else 1 - p)
        if m == "Handicap":
            spr = book_prices(ev, "spreads")
            p, oh, _ = _fair_pair(spr, home, away, book)
            if p is None or oh.get("point") is None:
                return None
            f = lambda lh: phi(_z(p) + (lh - oh["point"]) / sd_margin)
            line = float(bet.get("line") or 0)
            return f(line) if side == "home" else 1 - f(-line)
        if m == "Gesamtpunkte":
            p, oo, _ = _fair_pair(book_prices(ev, "totals"), "Over", "Under", book)
            if p is None or oo.get("point") is None:
                return None
            p_over = phi(_z(p) - (float(bet.get("line") or 0) - oo["point"]) / sd_total)
            return p_over if side == "over" else 1 - p_over
        return None
    a, b = ev["home_team"], ev["away_team"]
    p, _, _ = _fair_pair(book_prices(ev, "h2h"), a, b, book)
    if p is None:
        return None
    if rec.get("a") and rec["a"] != a:                   # Reihenfolge gedreht
        p = 1 - p
    return p if side == "A" else 1 - p


def record_close(picks, events, params=None):
    now = now_utc()
    nba_p = (params or {}).get("nba", {})
    by_id = {e["id"]: e for e in events}
    n = 0
    for pid, rec in picks.items():
        ev = by_id.get(pid)
        if not ev or rec.get("result") or parse_time(rec["commence"]) <= now or not rec.get("bet"):
            continue
        p = close_prob(rec, ev, nba_p.get("sd_margin", 12.5), nba_p.get("sd_total", 18.0))
        if p is None:
            continue
        odds = rec["bet"]["odds"]
        rec["close"] = {"pFair": round(p, 4), "fairOdds": round(1 / p, 3), "clv": round(odds * p - 1, 4),
                        "ts": now.isoformat(timespec="minutes")}
        n += 1
    log(f"Schlussquoten: {n} Prognosen mit CLV")


def calibration(picks):
    """Sagt das Modell 60 %, gewinnt die Seite dann auch etwa 60 %?  Fünf Stufen."""
    done = [r for r in picks.values() if r.get("result") and r.get("pModelA") is not None]
    out = []
    for lo in (0.0, 0.3, 0.45, 0.55, 0.7):
        hi = {0.0: 0.3, 0.3: 0.45, 0.45: 0.55, 0.55: 0.7, 0.7: 1.01}[lo]
        rows = [r for r in done if lo <= r["pModelA"] < hi]
        if rows:
            out.append({"from": lo, "to": min(hi, 1.0), "n": len(rows),
                        "pred": round(sum(r["pModelA"] for r in rows) / len(rows), 3),
                        "real": round(sum(1 for r in rows if r["result"] == "A") / len(rows), 3)})
    return out
