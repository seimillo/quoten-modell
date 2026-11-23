"""OddsPapi: zusätzliche Buchmacher (bet365 DE, bwin DE, Winamax DE, Betway DE, 888sport DE …).

Sparsam: pro Lauf EINE Abfrage für alle NBA-Spiele (/fixtures/odds/main mit tournamentId),
dazu einmalig die Buchmacher- und Marktlisten (werden zwischengespeichert).
Die Quoten werden in das Format von The Odds API umgebaut und an deren Spiele angehängt,
sodass das Modell sie wie alle anderen Buchmacher behandelt.
"""
import os
import re

from .common import get, load_json, log, now_utc, parse_time, save_json

BASE = "https://v5.oddspapi.io/en"
NBA_TOURNAMENT = 132
SPORT_BASKETBALL = 11
# gewünschte Marken → wir suchen die deutsche Version (Slug/Name mit „de“), sonst die Hauptversion
WANTED = ["bet365", "bwin", "winamax", "betway", "888sport", "betano", "tipico", "interwetten", "leovegas", "pinnacle"]
USED = {"requests": 0}


def _key():
    return os.environ.get("ODDSPAPI_KEY", "").strip()


def _get(path, params=None):
    p = dict(params or {})
    p["apiKey"] = _key()
    USED["requests"] += 1
    data, _ = get(f"{BASE}/{path}", p, tries=1, timeout=60)   # jede Wiederholung kostet eine Abfrage
    return data


def bookmakers():
    """Slugs der gewünschten Buchmacher (deutsche Version bevorzugt). Liste wird 30 Tage zwischengespeichert."""
    cache = load_json("oddspapi_books.json", {})
    if not cache.get("all") or cache.get("ts", "") < (now_utc().date().isoformat()[:8] + "00"):
        try:
            data = _get("bookmakers")
            items = data if isinstance(data, list) else data.get("bookmakers", data.get("data", []))
            cache = {"ts": now_utc().date().isoformat(),
                     "all": [{"slug": b.get("slug") or b.get("bookmaker") or b.get("id"), "name": b.get("bookmakerName") or b.get("name", "")}
                             for b in items if isinstance(b, dict)]}
            save_json("oddspapi_books.json", cache)
        except Exception as e:  # noqa: BLE001
            log(f"OddsPapi Buchmacher-Liste fehlgeschlagen: {e}")
            if not cache.get("all"):
                return {}
    chosen = {}
    for brand in WANTED:
        cands = [b for b in cache["all"] if b["slug"] and brand in str(b["slug"]).lower().replace(".", "")]
        de = [b for b in cands if re.search(r"(^|[^a-z])de($|[^a-z])|\.de\b|_de\b|-de\b", str(b["slug"]).lower() + " " + str(b["name"]).lower())]
        pick = (de or [b for b in cands if str(b["slug"]).lower().replace(".", "") == brand] or cands[:1])
        if pick:
            chosen[pick[0]["slug"]] = brand
    return chosen


def markets():
    """Marktdefinitionen Basketball: marketId → (Typ, Linie, Zeitraum, {outcomeId: Name})."""
    cache = load_json("oddspapi_markets.json", {})
    if not cache.get("markets"):
        try:
            data = _get("markets", {"sportId": SPORT_BASKETBALL})
            items = data if isinstance(data, list) else data.get("markets", data.get("data", []))
            cache = {"markets": {str(m["marketId"]): {"type": m.get("marketType"), "line": m.get("handicap"),
                                                      "period": m.get("period"), "name": m.get("marketName"),
                                                      "prop": m.get("playerProp"),
                                                      "outcomes": {str(o["outcomeId"]): o.get("outcomeName")
                                                                   for o in m.get("outcomes", [])}}
                                 for m in items if isinstance(m, dict) and "marketId" in m}}
            save_json("oddspapi_markets.json", cache)
        except Exception as e:  # noqa: BLE001
            log(f"OddsPapi Marktliste fehlgeschlagen: {e}")
            return {}
    return cache["markets"]


def _participants(fx):
    ps = fx.get("participants") or {}
    if isinstance(ps, dict):
        h = ps.get("participant1Name") or (ps.get("1") or {}).get("name") or (ps.get("home") or {}).get("name")
        a = ps.get("participant2Name") or (ps.get("2") or {}).get("name") or (ps.get("away") or {}).get("name")
    else:
        h = (ps[0] or {}).get("name") if ps else None
        a = (ps[1] or {}).get("name") if len(ps) > 1 else None
    return h or fx.get("participant1Name"), a or fx.get("participant2Name")


def _full_game(m):
    per = str(m.get("period") or "").lower()
    name = str(m.get("name") or "").lower()
    return (per in ("", "result", "ft", "fulltime", "full time", "match", "0", "game", "fullgame", "full_game")
            or "incl. overtime" in name or "incl overtime" in name) and not m.get("prop")


def to_events(fixtures, mk, books):
    """OddsPapi-Spiele → Format von The Odds API (h2h / spreads / totals je Buchmacher)."""
    out = []
    for fx in fixtures:
        home, away = _participants(fx)
        if not home or not away:
            continue
        rot = {}
        bms = []
        for slug, quotes in (fx.get("odds") or {}).items():
            if slug not in books:
                continue
            rotated = ((fx.get("bookmakers") or {}).get(slug) or {}).get("participantsRotated")
            by_market = {}
            for q in (quotes.values() if isinstance(quotes, dict) else quotes):
                if not q.get("active", True) or q.get("marketActive") is False or not q.get("price") or q.get("playerId") not in (None, 0, "0"):
                    continue
                m = mk.get(str(q.get("marketId")))
                if not m or not _full_game(m):
                    continue
                by_market.setdefault(str(q["marketId"]), {})[str(q["outcomeId"])] = (float(q["price"]), bool(q.get("mainLine")))
            h2h, spreads, totals = [], [], []
            for mid, outs in by_market.items():
                m = mk[mid]
                ids = sorted(m["outcomes"], key=int)
                if len(ids) != 2 or not all(i in outs for i in ids):
                    continue
                o1, o2 = outs[ids[0]][0], outs[ids[1]][0]
                main = outs[ids[0]][1] or outs[ids[1]][1]
                t = str(m["type"] or "").lower()
                first_home = not rotated
                if t in ("moneyline", "12", "winner"):
                    h2h = [{"name": home, "price": o1 if first_home else o2}, {"name": away, "price": o2 if first_home else o1}]
                elif t in ("spreads", "handicap", "asian handicap") and main and m["line"] is not None:
                    L = float(m["line"])
                    hp, ap = (o1, o2) if first_home else (o2, o1)
                    spreads = [{"name": home, "price": hp, "point": L if first_home else -L},
                               {"name": away, "price": ap, "point": -L if first_home else L}]
                elif t in ("totals", "total", "over/under") and main and m["line"] is not None:
                    n1 = str(m["outcomes"][ids[0]] or "").lower()
                    over, under = (o1, o2) if n1.startswith("o") or "over" in n1 else (o2, o1)
                    totals = [{"name": "Over", "price": over, "point": float(m["line"])},
                              {"name": "Under", "price": under, "point": float(m["line"])}]
            markets_ = [{"key": k, "outcomes": v} for k, v in (("h2h", h2h), ("spreads", spreads), ("totals", totals)) if v]
            if markets_:
                bms.append({"key": "op_" + books[slug], "title": books[slug], "markets": markets_})
        st = fx.get("startTime")
        if isinstance(st, (int, float)):
            from datetime import datetime, timezone
            st = datetime.fromtimestamp(st / 1000 if st > 1e11 else st, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        out.append({"home_team": home, "away_team": away, "commence_time": st, "bookmakers": bms})
    return out


def nba_events():
    if not _key():
        log("Kein ODDSPAPI_KEY gesetzt – OddsPapi aus.")
        return []
    books = bookmakers()
    mk = markets()
    if not books or not mk:
        return []
    try:
        data = _get("fixtures/odds/main", {"tournamentId": NBA_TOURNAMENT, "bookmakers": ",".join(books)})
    except Exception as e:  # noqa: BLE001
        log(f"OddsPapi Quoten fehlgeschlagen: {e}")
        return []
    fixtures = data if isinstance(data, list) else data.get("data", [])
    if fixtures and not load_json("debug_oddspapi.json", None):
        save_json("debug_oddspapi.json", {"books": books, "sample": fixtures[0]})   # einmalig zur Kontrolle
    evs = to_events(fixtures, mk, books)
    n_b = sum(len(e["bookmakers"]) for e in evs)
    log(f"OddsPapi: {len(evs)} Spiele, {n_b} Buchmacher-Einträge ({', '.join(sorted(set(books.values())))}), "
        f"{USED['requests']} Abfragen")
    return evs


def merge(events, extra):
    """Hängt die OddsPapi-Buchmacher an die passenden Spiele von The Odds API (gleiche Teams, ±12 Stunden)."""
    from .nba import canon
    n = 0
    for e in events:
        for x in extra:
            if canon(x["home_team"]) == canon(e["home_team"]) and canon(x["away_team"]) == canon(e["away_team"]):
                try:
                    if abs((parse_time(x["commence_time"]) - parse_time(e["commence_time"])).total_seconds()) > 12 * 3600:
                        continue
                except Exception:  # noqa: BLE001
                    pass
                have = {b["key"] for b in e.get("bookmakers", [])}
                for b in x["bookmakers"]:
                    if b["key"] not in have:
                        e.setdefault("bookmakers", []).append(_rename(b))
                n += 1
                break
    log(f"OddsPapi: {n} von {len(events)} Spielen zugeordnet")
    return events


def _rename(b):
    from .nba import canon
    for m in b["markets"]:
        for o in m["outcomes"]:
            if o["name"] not in ("Over", "Under"):
                o["name"] = canon(o["name"])
    return b
