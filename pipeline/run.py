"""Tägliche Pipeline: Daten holen → Modelle rechnen → Trefferliste pflegen → JSON für die Website schreiben.

Aufruf:  python -m pipeline.run            (braucht ODDS_API_KEY als Umgebungsvariable)
         python -m pipeline.selftest       (Offline-Test mit Beispieldaten)
"""
import os
from datetime import date, timedelta

from . import sources as S
from .common import LOG, load_json, log, now_utc, parse_time, save_json
from .nba import ALIASES, TEAMS, _et_date, canon, model_games
from .tennis import model_matches

from .params import calibrate, load_params, save_params
from .tracker import record_close, settle, stats, update_picks

MAX_TENNIS_KEYS = 4   # spart API-Credits (Gratis-Plan: 500 pro Monat)


def nba_season(dt):
    return dt.year + 1 if dt.month >= 9 else dt.year


def scores_map(score_events):
    out = {}
    for g in score_events:
        if g.get("completed") and g.get("scores"):
            try:
                out[g["id"]] = {canon(s["name"]): float(s["score"]) for s in g["scores"]}
            except Exception:  # noqa: BLE001
                pass
    return out


def update_history(now):
    """Alle NBA-Ergebnisse der Saison (ESPN), für Ruhe/Spielplan/Reise/Form und zum Auswerten. Kostet keine Credits."""
    hist = load_json("games.json", [])
    today = (now - timedelta(hours=5)).date()
    start = date(nba_season(now) - 1, 10, 15)
    have = {g["date"] for g in hist}
    days = [today - timedelta(days=i) for i in range(0, 4)]
    if len(have) < 10:                                  # erster Lauf: Saison nachholen
        d = start
        while d < today:
            days.append(d)
            d += timedelta(days=1)
    days = sorted(set(d for d in days if d >= start))
    by_key = {(g["date"], g["home"], g["away"]): g for g in hist}
    ok = 0
    for d in days:
        games = S.espn_scoreboard(d)
        if games is None:
            continue
        ok += 1
        for g in games:
            by_key[(g["date"], g["home"], g["away"])] = g
    hist = sorted(by_key.values(), key=lambda g: (g["date"], g["home"]))
    hist = [g for g in hist if g["date"] >= start.isoformat()]
    save_json("games.json", hist)
    log(f"ESPN-Spielplan: {ok} Tage abgefragt, {sum(1 for g in hist if g['done'])} Spiele in der Saison")
    return hist


def nba_scores_from_history(picks, hist, now):
    """Ergebnisse für offene NBA-Prognosen aus dem ESPN-Spielplan (spart die Credits für den Ergebnis-Abruf)."""
    out = {}
    for pid, r in picks.items():
        if r.get("sport") != "NBA" or r.get("result") or parse_time(r["commence"]) > now:
            continue
        d = _et_date(r["commence"])
        for g in hist:
            if (g["done"] and canon(g["home"]) == r.get("home") and canon(g["away"]) == r.get("away")
                    and abs((date.fromisoformat(g["date"]) - d).days) <= 1):
                out[pid] = {r["home"]: float(g["hs"]), r["away"]: float(g["as"])}
                break
    return out


def merged_injuries(now, known=None):
    """Offizieller NBA-Bericht, wo das Team schon gemeldet hat – sonst ESPN."""
    espn = {canon(k): v for k, v in S.espn_injuries().items()}
    official, missing, stamp = S.nba_official_injuries(list(TEAMS) + list(ALIASES), now, known)
    official = {canon(k): v for k, v in official.items()}
    missing = {canon(k) for k in missing}
    out = {}
    for t in TEAMS:
        if t in official and t not in missing:
            out[t] = official[t]
        elif t in espn:
            out[t] = [{**e, "src": "ESPN"} for e in espn[t]]
    return out, stamp


def nba_block(now, params, kalshi_nba, pm_nba, history):
    season = nba_season(now)
    cur, prior = S.bbref_ratings(season), S.bbref_ratings(season - 1)
    pace = S.bbref_pace(season)
    cur_pl, prior_pl = S.bbref_players(season), S.bbref_players(season - 1)
    injuries, stamp = merged_injuries(now, {k for k, _ in list(cur_pl) + list(prior_pl)})
    events = S.odds_events("basketball_nba", "h2h,spreads,totals")
    upcoming = [e for e in events if parse_time(e["commence_time"]) > now]
    nba = model_games(upcoming, cur, prior, pace, [], injuries, kalshi_nba, pm_nba, cur_pl, prior_pl,
                      params["nba"], history)
    for g in nba:
        g["injuryReport"] = stamp
    return nba, events


def main():
    mode = os.environ.get("RUN_MODE")
    if mode == "close":
        return main_close()
    now = now_utc()
    picks = load_json("picks.json", {})
    openings = load_json("openings.json", {})
    params = load_params()

    sports = S.odds_sports()
    active = {s["key"]: s for s in sports if s.get("active")}

    # ---------- Prognosemärkte ----------
    kalshi_nba = S.kalshi_events("KXNBAGAME")
    pm_nba = S.polymarket_events("nba")
    pm_tennis = S.polymarket_events("tennis")

    # ---------- NBA ----------
    nba = []
    history = update_history(now)
    if "basketball_nba" in active:
        nba, _ = nba_block(now, params, kalshi_nba, pm_nba, history)
        for g in nba:
            g["middayBest"] = g["best"]
    else:
        log("NBA: aktuell keine Spiele bei The Odds API")

    # ---------- Tennis ----------
    tennis_keys = [k for k in active if k.startswith(("tennis_atp", "tennis_wta"))][:MAX_TENNIS_KEYS]
    tennis = []
    if tennis_keys:
        elo_atp, elo_wta = S.tennis_elo("atp"), S.tennis_elo("wta")
        sport_events = []
        for k in tennis_keys:
            evs = [e for e in S.odds_events(k, "h2h") if parse_time(e["commence_time"]) > now]
            sport_events.append((k, active[k].get("title", k), evs))
        tennis = model_matches(sport_events, elo_atp, elo_wta, pm_tennis, openings, params["tennis"])
    else:
        log("Tennis: aktuell keine ATP/WTA-Turniere bei The Odds API")

    # ---------- Trefferliste ----------
    update_picks(picks, nba + tennis)
    scores = nba_scores_from_history(picks, history, now)
    open_keys = {r.get("sportKey") for r in picks.values()
                 if not r.get("result") and r.get("sportKey", "").startswith("tennis") and parse_time(r["commence"]) < now}
    for k in sorted(open_keys)[:2]:
        scores.update(scores_map(S.odds_scores(k, 3)))
    settle(picks, scores)
    params = calibrate(picks, params)                 # Modell lernt aus allen ausgewerteten Spielen
    save_params(params)

    # alte Eröffnungsquoten aufräumen (nur noch offene Events behalten)
    live_ids = {m["id"] for m in tennis}
    openings = {k: v for k, v in openings.items() if k in live_ids}

    save_json("nba.json", nba)
    save_json("tennis.json", tennis)
    save_json("picks.json", picks)
    save_json("openings.json", openings)
    old = load_json("status.json", {})
    save_json("status.json", {"updated": now.isoformat(), "credits": S.CREDITS, "stats": stats(picks), "log": LOG[-60:],
                              "closeUpdated": old.get("closeUpdated"), "closeLog": old.get("closeLog", []),
                              "params": {k: params[k] for k in ("nba", "tennis", "n")}})
    log("Fertig.")


def main_close():
    """Abendlauf nach dem NBA-Verletzungsbericht von 17:30 Uhr US-Zeit:
    NBA neu rechnen (neue Ausfälle, neue Quoten), Änderungen gegenüber mittags markieren,
    Schlussquoten holen und den CLV der Mittags-Prognosen speichern. Die Prognosen selbst bleiben die von mittags."""
    now = now_utc()
    picks = load_json("picks.json", {})
    params = load_params()
    open_recs = [r for r in picks.values() if not r.get("result") and parse_time(r["commence"]) > now]
    keys = sorted({r.get("sportKey") for r in open_recs if r.get("sportKey")})
    tennis_keys = [k for k in keys if k != "basketball_nba"][:2]
    S.odds_sports()                                   # liest nur das Credit-Guthaben aus, kostet nichts
    events = []
    old = {g["id"]: g for g in load_json("nba.json", [])}
    nba = None
    try:
        nba, nba_events = nba_block(now, params, S.kalshi_events("KXNBAGAME"), S.polymarket_events("nba"),
                                    load_json("games.json", []))
        events += nba_events
        for g in nba:
            mb = (old.get(g["id"]) or {}).get("middayBest")
            g["middayBest"] = mb
            g["eveningUpdate"] = now.isoformat()
            g["changed"] = bool(mb) and (mb.get("pick") != g["best"]["pick"] or bool(mb.get("value")) != bool(g["best"]["value"]))
        log(f"Abend-Update NBA: {sum(1 for g in nba if g['changed'])} Spiele mit geänderter Empfehlung")
    except Exception as e:  # noqa: BLE001
        log(f"Abend-Update NBA fehlgeschlagen: {e}")
    for k in tennis_keys:
        events += S.odds_events(k, "h2h")
    record_close(picks, events, params)
    if nba is not None:
        started = [g for g in old.values() if parse_time(g["commence"]) <= now]   # schon laufende Spiele behalten
        ids = {g["id"] for g in nba}
        save_json("nba.json", nba + [g for g in started if g["id"] not in ids])
    save_json("picks.json", picks)
    old = load_json("status.json", {})
    save_json("status.json", {**old, "closeUpdated": now.isoformat(), "credits": S.CREDITS, "stats": stats(picks),
                              "closeLog": LOG[-30:]})
    log("Fertig (Schlussquoten).")


if __name__ == "__main__":
    main()
