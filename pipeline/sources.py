"""Datenquellen. Jede Funktion gibt im Fehlerfall leere Daten zurück und schreibt eine Meldung ins Log,
damit ein ausgefallener Dienst nie die ganze Pipeline stoppt."""
import io
import os
import re

import pandas as pd

from .common import get, log

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


# ---------------- ESPN (NBA-Verletzungen, nur zur Anzeige) ----------------

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


def polymarket_trades(condition_id, limit=500):
    try:
        data, _ = get(f"{PM_DATA_BASE}/trades", {"market": condition_id, "limit": limit})
        return data.get("data", data) if isinstance(data, dict) else data
    except Exception as e:  # noqa: BLE001
        log(f"Polymarket Trades {condition_id[:10]}… fehlgeschlagen: {e}")
        return []
