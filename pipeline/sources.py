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
SHARP = ["pinnacle", "betfair_ex_eu"]
SOFT = ["tipico_de", "winamax_de", "williamhill", "betsson", "unibet_nl", "sport888", "marathonbet", "betvictor"]
BOOKS = ",".join(SHARP + SOFT)
BOOK_NAMES = {"tipico_de": "Tipico", "winamax_de": "Winamax", "williamhill": "William Hill", "betsson": "Betsson",
              "unibet_nl": "Unibet", "sport888": "888sport", "marathonbet": "Marathonbet", "betvictor": "BetVictor",
              "pinnacle": "Pinnacle", "betfair_ex_eu": "Betfair"}
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


# ---------------- Tennis Abstract (Elo) ----------------

def tennis_elo(tour="atp"):
    url = f"https://tennisabstract.com/reports/{tour}_elo_ratings.html"
    try:
        html, _ = get(url, as_json=False)
        tables = pd.read_html(io.StringIO(html))
        df = max(tables, key=len)
        df.columns = [str(c).strip() for c in df.columns]
        out = {}
        for _, row in df.iterrows():
            name = str(row.get("Player", "")).replace("\xa0", " ").strip()
            if not name or name == "nan":
                continue
            def num(col):
                try:
                    return float(row[col])
                except Exception:  # noqa: BLE001
                    return None
            out[name] = {"elo": num("Elo"), "hElo": num("hElo"), "cElo": num("cElo"), "gElo": num("gElo")}
        log(f"Tennis Abstract {tour.upper()}: {len(out)} Spieler")
        return out
    except Exception as e:  # noqa: BLE001
        log(f"Tennis Abstract {tour} fehlgeschlagen: {e}")
        return {}


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


def parse_nba_report(text, team_names):
    """Text des PDFs → {Teamname: [Einträge]}, dazu die Teams mit „NOT YET SUBMITTED“."""
    out, missing, team = {}, set(), None
    names = sorted(team_names, key=len, reverse=True)
    pat = re.compile(r"^(.+?,\s*[^,]+?)\s+(" + "|".join(NBA_STATUS) + r")\b\s*(.*)$")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("Injury Report", "Page ", "Game Date")):
            continue
        rest = line
        hit = None
        for t in names:
            i = line.rfind(t)
            if i >= 0 and (hit is None or i > hit[1]):
                hit = (t, i)
        if hit:
            team = hit[0]
            rest = line[hit[1] + len(hit[0]):].strip()
        if team is None:
            continue
        if rest.startswith("NOT YET SUBMITTED"):
            missing.add(team)
            continue
        m = pat.match(rest)
        if not m:
            if out.get(team) and rest and not re.match(r"^\d", rest):
                out[team][-1]["info"] = (out[team][-1]["info"] + " " + rest).strip()   # umgebrochener Grund
            continue
        name = _flip_name(m.group(1))
        if any(e["player"] == name for e in out.get(team, [])):
            continue                                     # Spieler steht schon beim früheren Spiel
        out.setdefault(team, []).append({"player": name, "status": m.group(2), "info": m.group(3).strip(), "src": "NBA"})
    return out, missing


def nba_official_injuries(team_names, now=None):
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
            out, missing = parse_nba_report(text, team_names)
            if not out and not missing:                  # Format unbekannt → Rohtext zur Fehlersuche ablegen
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
        games.append({"date": day.isoformat(), "start": ev.get("date"), "home": cs["home"]["team"]["displayName"],
                      "away": cs["away"]["team"]["displayName"], "done": done, "neutral": bool(comp.get("neutralSite")),
                      "hs": int(cs["home"].get("score") or 0) if done else None,
                      "as": int(cs["away"].get("score") or 0) if done else None})
    return games


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
