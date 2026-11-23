"""Rücktest mit echten historischen Quoten (OddsPapi historical-odds) – nur Sieg-Wetten.

Für bis zu MAX_GAMES vergangene Spiele dieser Saison:
- Modell-Chance wie im Rücktest (Stärke vom Vortag, Heimvorteil, Ruhe, Spielplan, Form; gelernte Faktoren),
  gemischt mit dem Markt wie im echten Betrieb (Gewicht CONF).
- „Entscheidungs-Quote“: letzte Quote von bet365.de/Tipico DECISION_HOURS vor Spielbeginn (≈ Mittagslauf).
- Schlussquote: letzte Pinnacle-Quote vor Spielbeginn, ohne Marge (fair).
Ausgewertet: Brier-Wert (Modell, Markt mittags, Pinnacle zum Schluss), alle ✓-Wetten nach denselben Regeln
wie live (EV ≥ 3 % und gegen Pinnacle nicht schlechter) mit Gewinn und CLV.
Jedes Spiel kostet eine Abfrage; die Antworten werden gespeichert, ein zweiter Lauf kostet nichts.
"""
import math
from datetime import timedelta

from . import oddspapi as OP
from .backtest import CARRY, KEYS, SEASONS, features, fetch_season
from .common import devig_power, load_json, log, now_utc, parse_time, phi, save_json
from .nba import canon

MAX_GAMES = 100
DECISION_HOURS = 10
BOOKS = "pinnacle,bet365.de,tipico"
MONEYLINE = "111"            # Basketball „Winner (incl. overtime)“: Ausgang 111 = Teilnehmer 1, 112 = Teilnehmer 2
CONF = 0.3
MIN_EV = 0.03


def _fixtures(start, end):
    cache = load_json("hist_fixtures.json", {})
    if cache.get("to", "") >= end:
        return cache["fixtures"]
    data = OP._get("fixtures", tournamentId=OP.NBA_TOURNAMENT, sportId=OP.SPORT_BASKETBALL, **{"from": start, "to": end})
    fx = data if isinstance(data, list) else data.get("data", [])
    fx = [{"id": f["fixtureId"], "p1": f.get("participant1Id"), "p2": f.get("participant2Id"), "start": f.get("startTime")}
          for f in fx if isinstance(f, dict) and f.get("fixtureId")]
    save_json("hist_fixtures.json", {"from": start, "to": end, "fixtures": fx})
    log(f"Historische Quoten: {len(fx)} NBA-Spiele bei OddsPapi gefunden")
    return fx


def _history(fid):
    """Quotenverlauf eines Spiels (gespeichert, damit jede Abfrage nur einmal bezahlt wird)."""
    store = load_json("hist_odds.json", {})
    if fid in store:
        return store[fid]
    data = OP._get("historical-odds", fixtureId=fid, bookmakers=BOOKS)
    books = (data or {}).get("bookmakers", {}) if isinstance(data, dict) else {}
    slim = {}
    for slug, b in books.items():
        m = ((b or {}).get("markets") or {}).get(MONEYLINE) or {}
        for oid, o in (m.get("outcomes") or {}).items():
            snaps = ((o.get("players") or {}).get("0")) or []
            slim.setdefault(slug, {})[oid] = [(s.get("createdAt"), s.get("price")) for s in snaps
                                              if s.get("price") and s.get("active", True) is not False]
    store[fid] = slim
    save_json("hist_odds.json", store)
    return slim


def _price_at(snaps, t):
    """Letzte Quote vor Zeitpunkt t."""
    best = None
    for ts, p in snaps or []:
        try:
            if parse_time(ts) <= t and (best is None or parse_time(ts) >= parse_time(best[0])):
                best = (ts, p)
        except Exception:  # noqa: BLE001
            continue
    return best[1] if best else None


def run():
    now = now_utc()
    if not OP._key():
        log("Historische Quoten: kein ODDSPAPI_KEY")
        return None
    names = OP.participants() or OP._refresh_participants()
    # Modellzeilen der laufenden Saison (Wissen vom Vortag)
    prior = {}
    for season, (s0, e0) in sorted(SEASONS.items()):
        _, prior = features(fetch_season(season, s0, e0), {}, 150)
    cur = [g for g in load_json("games.json", []) if g.get("done")]
    rows, _ = features(cur, {t: CARRY * v for t, v in prior.items()}, 0)
    bt = load_json("backtest.json", {})
    coef = {k: (bt.get("coef", {}).get(k, {}) or {}).get("value", 0.0) for k in KEYS}
    sd = bt.get("sd") or 13.5
    by_key = {(r["date"], r["home"], r["away"]): r for r in rows}

    start = min(r["date"] for r in rows) if rows else (now - timedelta(days=40)).date().isoformat()
    fx = _fixtures(start, (now - timedelta(days=1)).date().isoformat())
    matched = []
    for f in fx:
        a, b = names.get(str(f["p1"])), names.get(str(f["p2"]))
        if not a or not b or not f.get("start"):
            continue
        st = parse_time(f["start"])
        if st > now - timedelta(hours=6):
            continue
        d_et = (st - timedelta(hours=5)).date().isoformat()
        r = by_key.get((d_et, a, b)) or by_key.get((d_et, b, a))
        if r:
            matched.append((f, r, a == r["home"], st))
    matched.sort(key=lambda x: x[3], reverse=True)
    matched = matched[:MAX_GAMES]
    log(f"Historische Quoten: {len(matched)} Spiele zugeordnet, frage höchstens {MAX_GAMES} ab")

    games, bets = [], []
    for f, r, p1_home, st in matched:
        try:
            h = _history(f["id"])
        except Exception as e:  # noqa: BLE001
            log(f"Historische Quoten abgebrochen: {e}")
            break
        o_home, o_away = ("111", "112") if p1_home else ("112", "111")
        t_dec, t_close = st - timedelta(hours=DECISION_HOURS), st - timedelta(minutes=1)
        pin = h.get("pinnacle", {})
        ph_c, pa_c = _price_at(pin.get(o_home), t_close), _price_at(pin.get(o_away), t_close)
        ph_d, pa_d = _price_at(pin.get(o_home), t_dec), _price_at(pin.get(o_away), t_dec)
        if not (ph_c and pa_c and ph_d and pa_d):
            continue
        fair_close = devig_power(ph_c, pa_c)
        fair_dec = devig_power(ph_d, pa_d)
        m = sum(coef[k] * r[k] for k in KEYS) + r["alt"]
        p_model = phi(m / sd)
        p_mix = CONF * p_model + (1 - CONF) * fair_dec
        win = r["win"]
        games.append({"pModel": p_model, "pMix": p_mix, "pDec": fair_dec, "pClose": fair_close, "win": win})
        for slug in ("bet365.de", "tipico"):
            bk = h.get(slug, {})
            for side, oid, p, pc, pd in (("home", o_home, p_mix, fair_close, fair_dec),
                                         ("away", o_away, 1 - p_mix, 1 - fair_close, 1 - fair_dec)):
                q = _price_at(bk.get(oid), t_dec)
                if not q:
                    continue
                ev, ev_pin = p * q - 1, pd * q - 1
                if ev >= MIN_EV and ev_pin >= 0:
                    won = (win == 1.0) == (side == "home")
                    bets.append({"date": r["date"], "game": f"{r['away']} @ {r['home']}", "side": side, "book": slug,
                                 "odds": q, "ev": round(ev, 4), "clv": round(q * pc - 1, 4),
                                 "profit": round((q - 1) if won else -1.0, 3)})
    if not games:
        log("Historische Quoten: keine auswertbaren Spiele")
        return None

    def brier(k):
        return round(sum((g[k] - g["win"]) ** 2 for g in games) / len(games), 4)

    # nur die beste ✓-Wette pro Spiel (wie live)
    best = {}
    for b in bets:
        k = (b["date"], b["game"])
        if k not in best or b["ev"] > best[k]["ev"]:
            best[k] = b
    vb = sorted(best.values(), key=lambda b: b["date"], reverse=True)
    rep = {"date": now.date().isoformat(), "games": len(games), "decisionHours": DECISION_HOURS,
           "brier": {"model": brier("pModel"), "mix": brier("pMix"), "marketDecision": brier("pDec"), "pinnacleClose": brier("pClose")},
           "bets": len(vb), "profit": round(sum(b["profit"] for b in vb), 2),
           "roi": round(sum(b["profit"] for b in vb) / len(vb), 4) if vb else None,
           "clvAvg": round(sum(b["clv"] for b in vb) / len(vb), 4) if vb else None,
           "clvPos": round(sum(1 for b in vb if b["clv"] > 0) / len(vb), 3) if vb else None,
           "list": vb[:40], "requests": OP._usage()["requests"]}
    bt["odds"] = rep
    save_json("backtest.json", bt)
    log(f"Historische Quoten: {len(games)} Spiele · {len(vb)} ✓-Wetten · ROI {rep['roi']} · CLV {rep['clvAvg']}")
    return rep
