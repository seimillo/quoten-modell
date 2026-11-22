"""Tennis-Modell: Belag-Elo (Tennis Abstract), Best-of-5, Quotenbewegung, Markt-Anker mit Pinnacle und Prognosemärkten."""
from .common import devig_power, elo_p, last_name, log, logit, norm, sig
from .markets import match_polymarket, whales
from .sources import BOOK_NAMES, SOFT, book_prices, main_book

CLAY = ("french open", "roland", "madrid", "rome", "italian", "monte carlo", "barcelona", "hamburg", "clay")
GRASS = ("wimbledon", "queen", "halle", "grass", "eastbourne", "s hertogenbosch")
SLAMS = ("australian open", "french open", "wimbledon", "us open")


def surface_of(title):
    t = norm(title)
    if any(k in t for k in CLAY):
        return "cElo", "Sand"
    if any(k in t for k in GRASS):
        return "gElo", "Rasen"
    return "hElo", "Hartplatz"


def find_player(name, table):
    if name in table:
        return table[name]
    n = norm(name)
    for k, v in table.items():
        if norm(k) == n:
            return v
    ln = last_name(name)
    hits = [v for k, v in table.items() if last_name(k) == ln and norm(k)[:1] == n[:1]]
    return hits[0] if len(hits) == 1 else None


def bo3_to_bo5(p):
    lo, hi = 0.0, 1.0
    for _ in range(60):
        m = (lo + hi) / 2
        if m * m * (3 - 2 * m) < p:
            lo = m
        else:
            hi = m
    s = (lo + hi) / 2
    l = 1 - s
    return s ** 3 * (1 + 3 * l + 6 * l * l)


def auto_conf(e_min):
    return 0.5 if e_min >= 1900 else 0.4 if e_min >= 1700 else 0.3 if e_min >= 1500 else 0.25


def model_matches(sport_events, elo_atp, elo_wta, pm_events, openings, P=None):
    scale = (P or {}).get("conf_scale", 1.0)
    out = []
    for sport_key, title, events in sport_events:
        wta = "_wta_" in sport_key
        table = elo_wta if wta else elo_atp
        col, surf_name = surface_of(title)
        bo5 = (not wta) and any(s in norm(title) for s in SLAMS)
        for ev in events:
            a, b = ev["home_team"], ev["away_team"]
            h2h = book_prices(ev, "h2h")
            bk, b365 = main_book(h2h)
            if not b365:
                continue
            oa = next((o for o in b365 if o["name"] == a), None)
            ob = next((o for o in b365 if o["name"] == b), None)
            if not oa or not ob:
                continue
            ea, eb = find_player(a, table), find_player(b, table)
            book_a = devig_power(oa["price"], ob["price"])
            sharp_a = None
            for key in ("pinnacle", "betfair_ex_eu"):
                s = h2h.get(key)
                if s:
                    sa = next((o for o in s if o["name"] == a), None)
                    sb = next((o for o in s if o["name"] == b), None)
                    if sa and sb:
                        sharp_a = devig_power(sa["price"], sb["price"])
                        break
            parts = []
            if ea and eb and ea.get("elo") and eb.get("elo"):
                sa_, sb_ = ea.get(col) or ea["elo"], eb.get(col) or eb["elo"]
                p = elo_p(sa_, sb_)                      # Belag-Elo ist bei Tennis Abstract schon 50:50 gemischt
                parts.append({"n": f"Elo {surf_name}", "pp": None, "info": f"{sa_:.0f} vs. {sb_:.0f}"})
                base = p
                conf = auto_conf(min(sa_, sb_))
                kind = "elo"
            else:
                p = sharp_a or book_a
                base = p
                conf = 0.1
                kind = "markt"
                parts.append({"n": "Basis", "pp": None, "info": "keine Elo für beide → Start beim Markt"})
            L = logit(p)
            # Quotenbewegung seit dem ersten Abruf
            op = openings.setdefault(ev["id"], {"qa": oa["price"], "qb": ob["price"], "book": bk})
            if op.get("book", bk) != bk:              # anderer Buchmacher als beim ersten Abruf → neu starten
                op.update({"qa": oa["price"], "qb": ob["price"], "book": bk})
            open_a = devig_power(op["qa"], op["qb"])
            if abs(open_a - book_a) > 0.002:
                before = sig(L)
                L += 0.5 * (logit(book_a) - logit(open_a))
                parts.append({"n": "Quotenbewegung", "pp": round(sig(L) - before, 4), "info": f"{op['qa']:.2f}/{op['qb']:.2f} → {oa['price']:.2f}/{ob['price']:.2f}"})
            p = sig(L)
            if bo5:
                before = p
                p = bo3_to_bo5(p)
                parts.append({"n": "Best of 5", "pp": round(p - before, 4), "info": "Favorit profitiert"})
            p_model = p
            target = sharp_a if sharp_a is not None else book_a
            pm = match_polymarket(pm_events, last_name(a), last_name(b))
            if pm:
                target = 0.5 * target + 0.5 * pm["pA"] / (pm["pA"] + pm["pB"])
            conf_base = conf
            conf = max(0.02, min(0.85, conf_base * scale))     # gelernter Faktor aus der Kalibrierung
            p_final = conf * p_model + (1 - conf) * target
            parts.append({"n": "Markt-Anker", "pp": round(p_final - p_model, 4), "info": f"Modell {conf:.0%}, Markt {1 - conf:.0%}" + (" (inkl. Polymarket)" if pm else "")})
            from .nba import judge
            bets = []
            for name, main_q, pf, pm_, pin in ((a, oa["price"], p_final, p_model, sharp_a),
                                              (b, ob["price"], 1 - p_final, 1 - p_model,
                                               None if sharp_a is None else 1 - sharp_a)):
                offers = [(k, o["price"]) for k in SOFT for o in (h2h.get(k) or []) if o["name"] == name and o.get("price")]
                book_q, q = max(offers, key=lambda x: x[1]) if offers else (bk, main_q)
                bets.append(judge({"market": "Sieg", "pick": name, "odds": q, "book": book_q,
                                   "bookName": BOOK_NAMES.get(book_q, book_q), "mainOdds": main_q,
                                   "mainBook": BOOK_NAMES.get(bk, bk), "nBooks": len(offers),
                                   "p": round(pf, 4), "pModel": round(pm_, 4),
                                   "ev": round(pf * q - 1, 4), "evRaw": round(pm_ * q - 1, 4),
                                   "evPin": round(pin * q - 1, 4) if pin is not None else None,
                                   "side": "A" if name == a else "B"}))
            vals = [x for x in bets if x["value"]]
            best = max(vals or bets, key=lambda x: x["ev"])
            out.append({
                "id": ev["id"], "sportKey": sport_key, "sport": "Tennis", "tour": "WTA" if wta else "ATP", "event": title,
                "surface": surf_name, "bo5": bo5, "commence": ev["commence_time"], "a": a, "b": b,
                "eloA": ea, "eloB": eb, "base": round(base, 4), "kind": kind, "conf": conf,
                "pModelA": round(p_model, 4), "pMarketA": round(target, 4), "pFinalA": round(p_final, 4),
                "book": {"qa": oa["price"], "qb": ob["price"], "pA": round(book_a, 4), "key": bk, "name": BOOK_NAMES.get(bk, bk)},
                "sharpA": round(sharp_a, 4) if sharp_a is not None else None,
                "polymarket": ({"pA": pm["pA"], "pB": pm["pB"], "volume": round(pm["volume"])} if pm else None),
                "whales": whales(pm, a, b) if pm else {"trades": [], "usdA": 0, "usdB": 0},
                "parts": parts, "bets": bets, "best": best, "hasValue": bool(vals), "sideA": a, "pickLabel": f"{a} – {b}",
                "feat": {"pModelRaw": round(p_model, 4), "pMarket": round(target, 4), "confBase": conf_base},
            })
    out.sort(key=lambda m: (-m["hasValue"], -m["best"]["ev"]))
    log(f"Tennis: {len(out)} Matches bewertet")
    return out
