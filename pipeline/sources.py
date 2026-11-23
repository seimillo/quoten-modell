"""Datenquellen. Jede Funktion gibt im Fehlerfall leere Daten zurück und schreibt eine Meldung ins Log,
damit ein ausgefallener Dienst nie die ganze Pipeline stoppt."""
import io
import os
import re

import pandas as pd

from .common import UA, get, log, now_utc

ODDS_BASE = "https://api.the-odds-api.com/v4"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA_BASE = "https://gamma-api.polymarket.com"
PM_DATA_BASE = "https://data-api.polymarket.com"
# bet365 gibt es bei The Odds API nicht (nur in Australien, kostenpflichtig).
# Bis zu 10 Buchmacher kosten so viel wie eine Region.
SHARP = ["pinnacle", "betfair_ex_eu"]              # nur als Vergleichsmarkt, in Deutschland nicht wettbar
# Nur Anbieter mit deutscher Lizenz (GGL-Whitelist). Betano, Betway, LeoVegas und 888sport liefert die Schnittstelle
# nur als UK-/SE-Version – deren Quoten können leicht von der deutschen Seite abweichen (5 % Wettsteuer).
SOFT = ["tipico_de", "winamax_de", "betano_uk", "betway", "leovegas_se", "sport888"]
BOOKS = ",".join(SHARP + SOFT)                     # 8 Buchmacher = Kosten wie eine Region
BOOK_NAMES = {"tipico_de": "Tipico", "winamax_de": "Winamax", "betano_uk": "Betano", "betway": "Betway",
              "leovegas_se": "LeoVegas", "sport888": "888sport", "pinnacle": "Pinnacle", "betfair_ex_eu": "Betfair"}
CREDITS = {"remaining": None, "used_now": 0}


# ---------------- The Odds API (Quoten deutscher Buchmacher + Pinnacle, Ergebnisse) ----------------

def _key():
    return os.environ.get("ODDS_API_KEY", "").strip()


def _track_credits(resp):
    rem = resp.headers.get("x-requests-remaining")
    last = resp.headers.get("x-requests-last")
    if rem is not None:
        CREDITS["remaining"] = float(rem)
    if last is not None:
        CREDITS["used_now"] += float(last)


def credits_ok(minimum=15):
    return CREDITS["remaining"] is None or CREDITS["remaining"] >= minimum


def odds_sports():
    """Aktive Sportarten (kostet keine Credits)."""
    if not _key():
        log("Kein ODDS_API_KEY gesetzt – Quoten werden übersprungen.")
        return []
    try:
        data, r = get(f"{ODDS_BASE}/sports", {"apiKey": _key()})
        _track_credits(r)
        return data
    except Exception as e:  # noqa: BLE001
        log(f"The Odds API /sports fehlgeschlagen: {e}")
        return []


def odds_events(sport_key, markets="h2h"):
    if not _key() or not credits_ok():
        return []
    try:
        data, r = get(f"{ODDS_BASE}/sports/{sport_key}/odds",
                      {"apiKey": _key(), "bookmakers": BOOKS, "markets": markets, "oddsFormat": "decimal"})
        _track_credits(r)
        with_books = sum(1 for e in data if e.get("bookmakers"))
        log(f"Quoten {sport_key}: {len(data)} Events, {with_books} mit Buchmacher-Quoten")
        return data
    except Exception as e:  # noqa: BLE001
        log(f"Quoten für {sport_key} fehlgeschlagen: {e}")
        return []


def odds_scores(sport_key, days=3):
    if not _key() or not credits_ok():
        return []
    try:
        data, r = get(f"{ODDS_BASE}/sports/{sport_key}/scores", {"apiKey": _key(), "daysFrom": days})
        _track_credits(r)
        return data
    except Exception as e:  # noqa: BLE001
        log(f"Ergebnisse für {sport_key} fehlgeschlagen: {e}")
        return []


def main_book(prices):
    """Wettanbieter, gegen den der EV gerechnet wird: erster verfügbarer aus SOFT, sonst ein Sharp-Buch."""
    for k in SOFT + SHARP:
        if prices.get(k):
            return k, prices[k]
    return None, None


def book_prices(event, market):
    """{bookmaker: [outcomes]} für einen Markt eines Events."""
    out = {}
    for b in event.get("bookmakers", []):
        for m in b.get("markets", []):
            if m.get("key") == market:
                out[b["key"]] = m.get("outcomes", [])
    return out


# ---------------- Basketball-Reference (Team-Ratings, Tempo) ----------------

def _flatten(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [str(c[-1]).strip() for c in df.columns]
    else:
        df.columns = [str(c).strip() for c in df.columns]
    return df


def bbref_ratings(season):
    """Gegnerbereinigte Ratings: {Teamname: {g, ortg_a, drtg_a}}."""
    url = f"https://www.basketball-reference.com/leagues/NBA_{season}_ratings.html"
    try:
        html, _ = get(url, as_json=False)
        df = _flatten(pd.read_html(io.StringIO(html))[0])
        out = {}
        for _, r in df.iterrows():
            team = str(r.get("Team", "")).strip()
            if not team or team in ("Team", "nan"):
                continue
            try:
                out[team] = {"g": float(r["W"]) + float(r["L"]), "ortg_a": float(r["ORtg/A"]), "drtg_a": float(r["DRtg/A"])}
            except Exception:  # noqa: BLE001
                continue
        log(f"Basketball-Reference Ratings {season}: {len(out)} Teams")
        return out
    except Exception as e:  # noqa: BLE001
        log(f"Basketball-Reference Ratings {season} fehlgeschlagen: {e}")
        return {}


def bbref_pace(season):
    url = f"https://www.basketball-reference.com/leagues/NBA_{season}.html"
    try:
        html, _ = get(url, as_json=False)
        html = html.replace("<!--", "").replace("-->", "")   # Tabellen stecken teils in Kommentaren
        out = {}
        for t in pd.read_html(io.StringIO(html)):
            t = _flatten(t)
            if "Pace" in t.columns and "Team" in t.columns:
                for _, r in t.iterrows():
                    team = re.sub(r"\*$", "", str(r["Team"]).strip())
                    try:
                        out[team] = float(r["Pace"])
                    except Exception:  # noqa: BLE001
                        pass
                if len(out) >= 25:
                    break
        log(f"Basketball-Reference Tempo {season}: {len(out)} Teams")
        return out
    except Exception as e:  # noqa: BLE001
        log(f"Basketball-Reference Tempo fehlgeschlagen: {e}")
        return {}


BBREF_ABBR = {"BRK": "BKN", "CHO": "CHA", "PHO": "PHX"}


def player_key(name):
    import unicodedata
    n = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode().lower()
    n = re.sub(r"[^a-z ]", "", n.replace("-", " "))
    return " ".join(w for w in n.split() if w not in ("jr", "sr", "ii", "iii", "iv"))


def bbref_players(season):
    """Fortgeschrittene Spielerwerte: {(Name, Kürzel): {g, mp, bpm}}."""
    url = f"https://www.basketball-reference.com/leagues/NBA_{season}_advanced.html"
    import time
    time.sleep(3)                                        # Basketball-Reference erlaubt nur wenige Abrufe pro Minute
    try:
        html, _ = get(url, as_json=False)
        html = html.replace("<!--", "").replace("-->", "")
        df = max((_flatten(t) for t in pd.read_html(io.StringIO(html))), key=lambda t: ("BPM" in t.columns, len(t)))
        tcol = "Team" if "Team" in df.columns else "Tm"
        out = {}
        for _, r in df.iterrows():
            team = str(r.get(tcol, "")).strip()
            if not re.fullmatch(r"[A-Z]{3}", team):          # 2TM/3TM-Summenzeilen, Kopfzeilen, Liga-Schnitt
                continue
            try:
                g, mp, bpm = float(r["G"]), float(r["MP"]), float(r["BPM"])
            except Exception:  # noqa: BLE001
                continue
            out[(player_key(r["Player"]), BBREF_ABBR.get(team, team))] = {"g": g, "mp": mp, "bpm": bpm,
                                                                            "name": str(r["Player"]), "order": len(out)}
        log(f"Basketball-Reference Spieler {season}: {len(out)} Einträge")
        return out
    except Exception as e:  # noqa: BLE001
        log(f"Basketball-Reference Spieler {season} fehlgeschlagen: {e}")
        return {}


# ---------------- Offizieller NBA-Verletzungsbericht (PDF, alle 15 Minuten) ----------------
NBA_STATUS = ("Out", "Doubtful", "Questionable", "Probable", "Available")


def _flip_name(n):
    """„Jaquez Jr., Jaime“ → „Jaime Jaquez Jr.“"""
    if "," in n:
        last, first = n.split(",", 1)
        return f"{first.strip()} {last.strip()}"
    return n.strip()


SUFFIX = ("Jr.", "Sr.", "II", "III", "IV", "V")
PARTICLES = ("de", "da", "van", "von", "der", "la", "le", "di", "del", "st.")


def parse_nba_report(text, team_names, known=None):
    """Text des PDFs (oft ein Wort pro Zeile) → {Teamname: [Einträge]}, dazu Teams mit „NOT YET SUBMITTED“.
    known: Spielerschlüssel von Basketball-Reference, um mehrteilige Nachnamen richtig abzugrenzen."""
    t = " ".join(text.split())
    t = re.sub(r"(\w)-\s+(\w)", r"\1-\2", t)                      # „Gilgeous- Alexander“ → „Gilgeous-Alexander“
    t = re.sub(r"Injury Report: \S+ \S+ [AP]M Page \d+ of \d+", " ", t)
    t = re.sub(r"Game Date Game Time Matchup Team Player Name Current Status Reason", " ", t)
    t = " ".join(t.split())
    events = []
    for name in team_names:
        for m in re.finditer(re.escape(name) + r"\b", t):
            events.append((m.start(), m.end(), "team", name))
    for m in re.finditer(r"NOT YET SUBMITTED", t):
        events.append((m.start(), m.end(), "nys", None))
    st = "|".join(NBA_STATUS)
    for m in re.finditer(r"([A-Za-z'.\-]+), ((?:[A-Za-z'.\-]+ ){1,2}?)(" + st + r")\b", t):
        events.append((m.start(), m.end(), "player", m))
    events.sort(key=lambda e: (e[0], -e[1]))
    # Teamnamen, die in einem längeren Treffer stecken (z. B. Spielername), verwerfen
    clean, last_end = [], -1
    for e in events:
        if e[0] < last_end:
            continue
        clean.append(e)
        last_end = e[1]
    out, missing, team = {}, set(), None
    for i, (s0, e0, kind, val) in enumerate(clean):
        if kind == "team":
            team = val
            continue
        if team is None:
            continue
        if kind == "nys":
            missing.add(team)
            continue
        m = val
        first = m.group(2).strip()
        base = m.group(1)
        prev = t[:s0].split()[-3:]
        cands = [[base]] + [prev[-k:] + [base] for k in (1, 2, 3) if len(prev) >= k]
        chosen = [base]
        if known:
            for c in cands:
                full = f"{first} {' '.join(c)}"
                if player_key(full) in known:
                    chosen = c
        if chosen == [base]:
            if base in SUFFIX and prev:
                chosen = prev[-1:] + [base]
            elif prev and prev[-1].lower() in PARTICLES:
                chosen = prev[-1:] + [base]
        if chosen != [base] and out.get(team) and out[team][-1]["info"].endswith(" ".join(chosen[:-1])):
            out[team][-1]["info"] = out[team][-1]["info"][: -len(" ".join(chosen[:-1]))].strip()
        nxt = clean[i + 1][0] if i + 1 < len(clean) else len(t)
        info = t[e0:nxt].strip()
        info = re.sub(r"\s*\d{2}/\d{2}/\d{4}.*$|\s*\d{2}:\d{2} \(ET\).*$", "", info)
        name = f"{first} {' '.join(chosen)}"
        if any(x["player"] == name for x in out.get(team, [])):
            continue                                     # Spieler steht schon beim früheren Spiel
        out.setdefault(team, []).append({"player": name, "status": m.group(3), "info": info, "src": "NBA"})
    return out, missing


def nba_official_injuries(team_names, now=None, known=None):
    """Neuester Bericht: von jetzt (US-Ostküstenzeit) in 15-Minuten-Schritten rückwärts suchen."""
    import requests
    from datetime import timedelta
    from pypdf import PdfReader
    now = now or now_utc()
    et = now - timedelta(hours=5)
    et = et.replace(minute=et.minute - et.minute % 15, second=0, microsecond=0)
    for i in range(24):
        t = et - timedelta(minutes=15 * i)
        url = ("https://ak-static.cms.nba.com/referee/injury/Injury-Report_"
               f"{t:%Y-%m-%d}_{t:%I}_{t:%M}{t:%p}.pdf")
        try:
            r = requests.get(url, headers=UA, timeout=20)
        except Exception:  # noqa: BLE001
            continue
        if r.status_code != 200 or not r.content.startswith(b"%PDF"):
            continue
        try:
            text = "\n".join(pg.extract_text() or "" for pg in PdfReader(io.BytesIO(r.content)).pages)
            out, missing = parse_nba_report(text, team_names, known)
            if not out and not missing:                  # Format unbekannt → Rohtext zur Fehlersuche ablegen
                log("NBA-Verletzungsbericht: Format unbekannt – 0 Spieler erkannt")
                from .common import DATA_DIR
                with open(os.path.join(DATA_DIR, "debug_injury.txt"), "w", encoding="utf-8") as fh:
                    fh.write(repr(text[:4000]))
            log(f"NBA-Verletzungsbericht {t:%d.%m. %H:%M} ET: {sum(len(v) for v in out.values())} Spieler, "
                f"{len(missing)} Teams noch nicht gemeldet")
            return out, missing, f"{t:%Y-%m-%d %H:%M} ET"
        except Exception as e:  # noqa: BLE001
            log(f"NBA-Verletzungsbericht nicht lesbar: {e}")
            break
    log("NBA-Verletzungsbericht nicht gefunden – nur ESPN")
    return {}, set(), None


# ---------------- ESPN-Spielplan (Ergebnisse für Spielplan-Faktoren und Auswertung) ----------------

def espn_scoreboard(day):
    """Alle NBA-Spiele eines Tages (US-Datum)."""
    url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
    try:
        data, _ = get(url, {"dates": day.strftime("%Y%m%d")})
    except Exception as e:  # noqa: BLE001
        log(f"ESPN-Spielplan {day} fehlgeschlagen: {e}")
        return None
    games = []
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        cs = {c.get("homeAway"): c for c in comp.get("competitors", [])}
        if "home" not in cs or "away" not in cs:
            continue
        done = bool(((ev.get("status") or {}).get("type") or {}).get("completed"))
        games.append({"date": day.isoformat(), "start": ev.get("date"), "eid": ev.get("id"), "home": cs["home"]["team"]["displayName"],
                      "away": cs["away"]["team"]["displayName"], "done": done, "neutral": bool(comp.get("neutralSite")),
                      "hs": int(cs["home"].get("score") or 0) if done else None,
                      "as": int(cs["away"].get("score") or 0) if done else None})
    return games


def espn_boxscore(eid):
    """Gespielte Minuten je Spieler eines Spiels: {Teamname: {Spielername: Minuten}}."""
    url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"
    try:
        data, _ = get(url, {"event": eid})
    except Exception as e:  # noqa: BLE001
        log(f"ESPN-Boxscore {eid} fehlgeschlagen: {e}")
        return None
    out = {}
    for team in (data.get("boxscore") or {}).get("players", []):
        name = (team.get("team") or {}).get("displayName")
        for st in team.get("statistics", [])[:1]:
            labels = [str(x).upper() for x in (st.get("labels") or st.get("names") or [])]
            if "MIN" not in labels:
                continue
            i = labels.index("MIN")
            for a in st.get("athletes", []):
                if a.get("didNotPlay") or not a.get("stats"):
                    continue
                raw = str(a["stats"][i])
                try:
                    m = float(raw.split(":")[0]) + (float(raw.split(":")[1]) / 60 if ":" in raw else 0)
                except ValueError:
                    continue
                if m > 0:
                    out.setdefault(name, {})[(a.get("athlete") or {}).get("displayName", "")] = round(m, 1)
    return out


# ---------------- ESPN (NBA-Verletzungen) ----------------

def espn_injuries():
    url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
    try:
        data, _ = get(url)
        out = {}
        for team in data.get("injuries", []):
            name = team.get("displayName", "")
            out[name] = [{"player": i.get("athlete", {}).get("displayName", "?"),
                          "status": i.get("status", ""),
                          "info": (i.get("shortComment") or i.get("details", {}).get("type") or "")[:140]}
                         for i in team.get("injuries", [])]
        log(f"ESPN Verletzungen: {sum(len(v) for v in out.values())} Einträge")
        return out
    except Exception as e:  # noqa: BLE001
        log(f"ESPN Verletzungen fehlgeschlagen: {e}")
        return {}


# ---------------- Kalshi (Prognosemarkt) ----------------

def _cents(m, field):
    v = m.get(field + "_dollars")
    if v not in (None, ""):
        try:
            return float(v) * 100
        except Exception:  # noqa: BLE001
            pass
    v = m.get(field)
    return float(v) if v not in (None, "") else None


def kalshi_events(series):
    """Offene Events einer Serie, z. B. KXNBAGAME. Je Markt: Ticker, Titel, Preis (Cent), Volumen."""
    try:
        data, _ = get(f"{KALSHI_BASE}/events", {"series_ticker": series, "status": "open",
                                                 "with_nested_markets": "true", "limit": 200})
        events = []
        for ev in data.get("events", []):
            mk = []
            for m in ev.get("markets", []):
                price = _cents(m, "last_price") or None
                bid, ask = _cents(m, "yes_bid"), _cents(m, "yes_ask")
                if bid and ask:
                    price = (bid + ask) / 2
                mk.append({"ticker": m.get("ticker", ""), "title": m.get("yes_sub_title") or m.get("title", ""),
                           "price": price, "volume": m.get("volume") or m.get("volume_fp") or 0})
            events.append({"ticker": ev.get("event_ticker", ""), "title": ev.get("title", ""), "markets": mk})
        log(f"Kalshi {series}: {len(events)} Events")
        return events
    except Exception as e:  # noqa: BLE001
        log(f"Kalshi {series} fehlgeschlagen: {e}")
        return []


# ---------------- Polymarket (Prognosemarkt + Großeinsätze) ----------------

def polymarket_events(tag):
    try:
        data, _ = get(f"{GAMMA_BASE}/events", {"tag_slug": tag, "active": "true", "closed": "false", "limit": 200})
        log(f"Polymarket {tag}: {len(data)} Events")
        return data
    except Exception as e:  # noqa: BLE001
        log(f"Polymarket {tag} fehlgeschlagen: {e}")
        return []


def polymarket_trades(condition_id, limit=1000, min_usd=1000):
    """Data API v2 (v1 wurde am 2. 11. 2026 abgeschaltet): nur Trades ab min_usd Dollar."""
    try:
        data, _ = get(f"{PM_DATA_BASE}/v2/trades", {"condition": condition_id, "limit": limit,
                                                   "filter_type": "CASH", "filter_amount": min_usd})
        return data.get("data", data) if isinstance(data, dict) else data
    except Exception as e:  # noqa: BLE001
        log(f"Polymarket Trades {condition_id[:10]}… fehlgeschlagen: {e}")
        return []
