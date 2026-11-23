"""OddsPapi (API v4): zusätzliche deutsche Buchmacher, vor allem bet365.de und bwin.de.

Gratis-Stufe: 250 Abfragen im Monat, pro Abfrage EIN Buchmacher für die ganze NBA (odds-by-tournaments).
Pro Lauf also 2 Abfragen (bet365.de, bwin.de) – mittags und abends = rund 120 im Monat.
Teilnehmer- und Marktliste werden einmal geholt und zwischengespeichert.
Ein Monatsbudget (BUDGET) verhindert, dass das Kontingent ausgeht.
Die Quoten werden ins Format von The Odds API umgebaut und an deren Spiele angehängt.
"""
import os
import time

from .common import get, load_json, log, now_utc, parse_time, save_json

BASE = "https://api.oddspapi.io/v4"
NBA_TOURNAMENT = 132
SPORT_BASKETBALL = 11
BOOKS = {"bet365.de": "bet365", "bwin.de": "bwin"}     # Slug → Schlüssel-Endung (op_bet365, op_bwin)
BUDGET = 230                                          # von 250 im Monat; Rest als Reserve


def _key():
    return os.environ.get("ODDSPAPI_KEY", "").strip()


def _usage():
    u = load_json("oddspapi_usage.json", {})
    month = now_utc().strftime("%Y-%m")
    if u.get("month") != month:
        u = {"month": month, "requests": 0}
    return u


def _get(path, **params):
    u = _usage()
    if u["requests"] >= BUDGET:
        raise RuntimeError(f"Monatsbudget erreicht ({u['requests']} Abfragen)")
    params["apiKey"] = _key()
    u["requests"] += 1
    save_json("oddspapi_usage.json", u)
    time.sleep(1.1)                                   # OddsPapi verlangt ~1 s Pause zwischen Abfragen
    data, _ = get(f"{BASE}/{path}", params, tries=1, timeout=60)   # jede Wiederholung kostet eine Abfrage
    return data


def participants():
    """Teilnehmer-ID → Teamname (einmal holen, dann zwischenspeichern; neue IDs lösen einen neuen Abruf aus)."""
    cache = load_json("oddspapi_participants.json", {})
    return cache


def _refresh_participants():
    try:
        data = _get("participants", sportId=SPORT_BASKETBALL)
    except Exception as e:  # noqa: BLE001
        log(f"OddsPapi Teilnehmerliste fehlgeschlagen: {e}")
        return load_json("oddspapi_participants.json", {})
    items = data if isinstance(data, list) else data.get("participants", data.get("data", data))
    out = {}
    if isinstance(items, dict):                       # Form {id: name}
        out = {str(k): (v if isinstance(v, str) else v.get("participantName") or v.get("name")) for k, v in items.items()}
    else:
        for p in items:
            if isinstance(p, dict):
                pid = p.get("participantId") or p.get("id")
                name = p.get("participantName") or p.get("name")
                if pid and name:
                    out[str(pid)] = name
    from .nba import TEAMS, canon
    out = {k: canon(v) for k, v in out.items() if canon(v) in TEAMS}   # nur NBA-Teams behalten
    save_json("oddspapi_participants.json", out)
    log(f"OddsPapi Teilnehmerliste: {len(out)} NBA-Teams")
    return out


def markets():
    """Marktdefinitionen Basketball (ganzes Spiel inkl. Verlängerung): marketId → Typ, Linie, Ausgänge."""
    cache = load_json("oddspapi_markets.json", {})
    if cache.get("markets"):
        return cache["markets"]
    try:
        data = _get("markets", sportId=SPORT_BASKETBALL)
    except Exception as e:  # noqa: BLE001
        log(f"OddsPapi Marktliste fehlgeschlagen: {e}")
        return {}
    items = data if isinstance(data, list) else data.get("markets", data.get("data", []))
    mk = {}
    for m in items:
        if not isinstance(m, dict) or m.get("playerProp") or str(m.get("period")) != "result":
            continue
        t = str(m.get("marketType") or "").lower()
        if t not in ("moneyline", "spreads", "spread", "handicap", "totals"):
            continue
        mk[str(m["marketId"])] = {"type": t, "line": m.get("handicap"), "name": m.get("marketName"),
                                  "outcomes": {str(o["outcomeId"]): str(o.get("outcomeName")) for o in m.get("outcomes", [])}}
    save_json("oddspapi_markets.json", {"markets": mk})
    log(f"OddsPapi Marktliste: {len(mk)} Märkte (Sieg, Handicap, Gesamtpunkte)")
    return mk


def _book_markets(bo, mk, p1_is_home, home, away):
    """Ein Buchmacher-Eintrag einer Partie → h2h/spreads/totals im Format von The Odds API (nur Hauptlinien)."""
    h2h, spreads, totals = [], [], []
    for mid, m in (bo.get("markets") or {}).items():
        d = mk.get(str(mid))
        if not d or m.get("marketActive") is False:
            continue
        prices, main = {}, False
        for oid, o in (m.get("outcomes") or {}).items():
            pl = ((o.get("players") or {}).get("0")) or {}
            if pl.get("active") is False or not pl.get("price"):
                continue
            prices[d["outcomes"].get(str(oid), str(oid))] = float(pl["price"])
            main = main or bool(pl.get("mainLine"))
        if len(prices) != 2:
            continue
        if d["type"] == "moneyline" and {"1", "2"} <= set(prices):
            ph, pa = (prices["1"], prices["2"]) if p1_is_home else (prices["2"], prices["1"])
            h2h = [{"name": home, "price": ph}, {"name": away, "price": pa}]
        elif d["type"] in ("spreads", "spread", "handicap") and main and {"1", "2"} <= set(prices) and d["line"] is not None:
            L = float(d["line"])                                 # Linie gilt für Teilnehmer 1
            if p1_is_home:
                spreads = [{"name": home, "price": prices["1"], "point": L}, {"name": away, "price": prices["2"], "point": -L}]
            else:
                spreads = [{"name": home, "price": prices["2"], "point": -L}, {"name": away, "price": prices["1"], "point": L}]
        elif d["type"] == "totals" and main and {"Over", "Under"} <= set(prices) and d["line"] is not None:
            totals = [{"name": "Over", "price": prices["Over"], "point": float(d["line"])},
                      {"name": "Under", "price": prices["Under"], "point": float(d["line"])}]
    return [{"key": k, "outcomes": v} for k, v in (("h2h", h2h), ("spreads", spreads), ("totals", totals)) if v]


def attach(events):
    """Holt bet365.de und bwin.de für alle NBA-Spiele und hängt sie an die Spiele von The Odds API."""
    if not _key():
        log("Kein ODDSPAPI_KEY gesetzt – OddsPapi aus.")
        return events
    if not events:
        return events
    mk = markets()
    if not mk:
        return events
    names = participants()
    from .nba import canon
    by_teams = {}
    for e in events:
        by_teams[frozenset((canon(e["home_team"]), canon(e["away_team"])))] = e
    added, lines_ok, lines_bad, refreshed = 0, 0, 0, False
    for slug, key in BOOKS.items():
        try:
            data = _get("odds-by-tournaments", tournamentIds=NBA_TOURNAMENT, bookmaker=slug, oddsFormat="decimal")
        except Exception as e:  # noqa: BLE001
            log(f"OddsPapi {slug} fehlgeschlagen: {e}")
            continue
        fixtures = data if isinstance(data, list) else data.get("data", [])
        missing = {str(f.get(k)) for f in fixtures for k in ("participant1Id", "participant2Id")} - set(names)
        if missing and not refreshed:
            names, refreshed = _refresh_participants(), True
        for fx in fixtures:
            p1, p2 = names.get(str(fx.get("participant1Id"))), names.get(str(fx.get("participant2Id")))
            e = by_teams.get(frozenset((p1, p2))) if p1 and p2 else None
            bo = (fx.get("bookmakerOdds") or {}).get(slug)
            if not e or not bo or bo.get("suspended"):
                continue
            try:
                if abs((parse_time(fx["startTime"]) - parse_time(e["commence_time"])).total_seconds()) > 12 * 3600:
                    continue
            except Exception:  # noqa: BLE001
                pass
            home, away = canon(e["home_team"]), canon(e["away_team"])
            mkts = _book_markets(bo, mk, p1 == home, e["home_team"], e["away_team"])
            if not mkts:
                continue
            # Plausibilität: Handicap-Linie wie bei den anderen Buchmachern?
            sp = next((m for m in mkts if m["key"] == "spreads"), None)
            ref = next((o.get("point") for b in e.get("bookmakers", []) for m in b.get("markets", [])
                        if m["key"] == "spreads" for o in m["outcomes"] if o["name"] == e["home_team"]), None)
            if sp and ref is not None:
                mine = next(o["point"] for o in sp["outcomes"] if o["name"] == e["home_team"])
                if abs(mine - ref) <= 3:
                    lines_ok += 1
                else:
                    lines_bad += 1
                    mkts = [m for m in mkts if m["key"] != "spreads"]   # Vorzeichen unklar → Handicap weglassen
            e.setdefault("bookmakers", []).append({"key": "op_" + key, "title": key, "markets": mkts,
                                                    "link": bo.get("fixturePath")})
            added += 1
    log(f"OddsPapi: {added} Buchmacher-Einträge an {len(events)} Spiele gehängt (bet365.de, bwin.de) · "
        f"Handicap-Linien plausibel {lines_ok}, verworfen {lines_bad} · {_usage()['requests']} von 250 Abfragen diesen Monat")
    return events
