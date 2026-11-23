"""Tägliche NBA-Pipeline: Daten holen → Modell rechnen → Trefferliste pflegen → JSON für die Website schreiben.

Aufruf:  python -m pipeline.run            (braucht ODDS_API_KEY als Umgebungsvariable)
         python -m pipeline.selftest       (Offline-Test mit Beispieldaten)
"""
import os
from datetime import date, timedelta

from . import sources as S
from .common import LOG, load_json, log, now_utc, parse_time, save_json
from .nba import ALIASES, TEAMS, _et_date, canon, market_sd, model_games
from .params import MIN_GAMES

from .params import calibrate, load_params, save_params
from .notify import notify_changes, notify_problems, notify_results, notify_value_bets
from .tracker import record_close, settle, stats, update_picks

def nba_season(dt):
    return dt.year + 1 if dt.month >= 9 else dt.year


def update_history(now):
    """Alle NBA-Ergebnisse der Saison (ESPN), für Ruhe/Spielplan/Reise/Form und zum Auswerten. Kostet keine Credits."""
    hist = load_json("games.json", [])
    today = (now - timedelta(hours=5)).date()
    start = date(nba_season(now) - 1, 10, 15)
    have = {g["date"] for g in hist}
    days = [today - timedelta(days=i) for i in range(0, 4)]
    days += [date.fromisoformat(g["date"]) for g in hist                     # ältere Einträge ohne Spiel-ID nachladen
             if not g.get("eid") and g["date"] >= (today - timedelta(days=21)).isoformat()]
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


def update_minutes(hist, now, max_new=120):
    """Minuten der letzten Spiele je Spieler aus ESPN-Boxscores (für die heutige Aufstellung)."""
    from .sources import player_key
    M = load_json("minutes.json", {"done": [], "players": {}})
    done = set(M["done"])
    since = ((now - timedelta(days=21)).date()).isoformat()
    todo = [g for g in hist if g.get("done") and g.get("eid") and g["date"] >= since and g["eid"] not in done][-max_new:]
    ok = 0
    for g in todo:
        box = S.espn_boxscore(g["eid"])
        if box is None:
            continue
        ok += 1
        done.add(g["eid"])
        for team, players in box.items():
            t = canon(team)
            if t not in TEAMS:
                continue
            for name, m in players.items():
                key = f"{TEAMS[t][0]}|{player_key(name)}"
                M["players"].setdefault(key, []).append([g["date"], m])
    for k, v in M["players"].items():
        M["players"][k] = sorted(v)[-12:]
    M["done"] = sorted(done)[-1500:]
    save_json("minutes.json", M)
    log(f"ESPN-Boxscores: {ok} neue Spiele, Minuten für {len(M['players'])} Spieler")
    return {k: [m for _, m in v] for k, v in M["players"].items()}


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


def load_minutes():
    M = load_json("minutes.json", {"players": {}})
    return {k: [m for _, m in v] for k, v in M.get("players", {}).items()}


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


def nba_block(now, params, kalshi_nba, pm_nba, history, recent_min=None):
    season = nba_season(now)
    cur, prior = S.bbref_ratings(season), S.bbref_ratings(season - 1)
    pace = S.bbref_pace(season)
    cur_pl, prior_pl = S.bbref_players(season), S.bbref_players(season - 1)
    injuries, stamp = merged_injuries(now, {k for k, _ in list(cur_pl) + list(prior_pl)})
    events = S.odds_events("basketball_nba", "h2h,spreads,totals")
    try:
        from . import oddspapi
        oddspapi.attach([e for e in events if parse_time(e["commence_time"]) > now])
    except Exception as e:  # noqa: BLE001
        log(f"OddsPapi fehlgeschlagen: {e}")
    upcoming = [e for e in events if parse_time(e["commence_time"]) > now]
    P = dict(params["nba"])
    msd = market_sd(upcoming)
    if msd and params.get("n", {}).get("nba", 0) < MIN_GAMES:
        # Bis die Kalibrierung genug echte Spiele hat: Mitte zwischen Rücktest (einfacheres Modell) und Markt
        P["sd_margin"] = round(0.5 * (P["sd_margin"] + msd), 2)
    log(f"Streuung: Markt {msd if msd is None else round(msd, 1)}, Modell nutzt {P['sd_margin']}")
    nba = model_games(upcoming, cur, prior, pace, [], injuries, kalshi_nba, pm_nba, cur_pl, prior_pl,
                      P, history, recent_min or load_minutes())
    for g in nba:
        g["injuryReport"] = stamp
        g["sdUsed"], g["sdMarket"] = P["sd_margin"], msd
    return nba, events


def main():
    mode = os.environ.get("RUN_MODE")
    if mode == "close":
        return main_close()
    if mode == "late":
        return main_late()
    if mode == "histodds":
        from .histodds import run as hist
        try:
            hist()
        except Exception as e:  # noqa: BLE001
            log(f"Historische Quoten fehlgeschlagen: {e}")
        old = load_json("status.json", {})
        save_json("status.json", {**old, "histLog": LOG[-15:]})
        return
    if mode == "props_survey":
        from .props_survey import main as survey
        survey()
        old = load_json("status.json", {})
        save_json("status.json", {**old, "surveyLog": LOG[-15:]})
        return
    if mode == "discover":
        from .oddspapi_discover import main as discover
        return discover()
    if mode == "backtest":
        from .backtest import run_backtest
        update_history(now_utc())
        run_backtest()
        old = load_json("status.json", {})
        save_json("status.json", {**old, "backtestLog": LOG[-20:]})
        return
    now = now_utc()
    picks = {k: v for k, v in load_json("picks.json", {}).items() if v.get("sport") == "NBA"}
    params = load_params()

    sports = S.odds_sports()
    active = {s["key"]: s for s in sports if s.get("active")}

    # ---------- Prognosemärkte ----------
    kalshi_nba = S.kalshi_events("KXNBAGAME")
    pm_nba = S.polymarket_events("nba")

    # ---------- NBA ----------
    nba = []
    history = update_history(now)
    recent_min = update_minutes(history, now)
    if "basketball_nba" in active:
        nba, _ = nba_block(now, params, kalshi_nba, pm_nba, history, recent_min)
        for g in nba:
            g["middayBest"] = g["best"]
    else:
        log("NBA: aktuell keine Spiele bei The Odds API")

    # ---------- Trefferliste ----------
    update_picks(picks, nba)
    open_ids = {k for k, r in picks.items() if not r.get("result")}
    settle(picks, nba_scores_from_history(picks, history, now))
    notify_results([picks[k] for k in open_ids if picks[k].get("result")])
    notify_value_bets(nba)
    params = calibrate(picks, params)                 # Modell lernt aus allen ausgewerteten Spielen
    save_params(params)

    save_json("nba.json", nba)
    save_json("picks.json", picks)
    old = load_json("status.json", {})
    save_json("status.json", {"updated": now.isoformat(), "credits": S.CREDITS, "stats": stats(picks), "log": LOG[-60:],
                              "closeUpdated": old.get("closeUpdated"), "closeLog": old.get("closeLog", []),
                              "params": {k: params[k] for k in ("nba", "n")}})
    notify_problems(LOG, S.CREDITS, "Mittagslauf")
    log("Fertig.")


def main_close():
    """Abendlauf nach dem NBA-Verletzungsbericht von 17:30 Uhr US-Zeit:
    NBA neu rechnen (neue Ausfälle, neue Quoten), Änderungen gegenüber mittags markieren,
    Schlussquoten holen und den CLV der Mittags-Prognosen speichern. Die Prognosen selbst bleiben die von mittags."""
    now = now_utc()
    picks = load_json("picks.json", {})
    params = load_params()
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
        notify_changes(nba)
    except Exception as e:  # noqa: BLE001
        log(f"Abend-Update NBA fehlgeschlagen: {e}")
    record_close(picks, events, params)
    if nba is not None:
        started = [g for g in old.values() if parse_time(g["commence"]) <= now]   # schon laufende Spiele behalten
        ids = {g["id"] for g in nba}
        save_json("nba.json", nba + [g for g in started if g["id"] not in ids])
    save_json("picks.json", picks)
    old = load_json("status.json", {})
    save_json("status.json", {**old, "closeUpdated": now.isoformat(), "credits": S.CREDITS, "stats": stats(picks),
                              "closeLog": LOG[-30:]})
    notify_problems(LOG, S.CREDITS, "Abendlauf")
    log("Fertig (Schlussquoten).")


def main_late():
    """Nachtlauf (ca. 3:47 Uhr): nur Schlussquoten für Spiele, die erst danach beginnen (Westküste).
    Kein Modell, keine Push-Nachrichten außer bei Problemen. Kostet nur dann Credits, wenn noch Spiele offen sind."""
    now = now_utc()
    picks = load_json("picks.json", {})
    open_later = [r for r in picks.values() if not r.get("result") and parse_time(r["commence"]) > now
                  and parse_time(r["commence"]) < now + timedelta(hours=3)]
    if not open_later:
        log("Nachtlauf: keine Spiele mehr offen – keine Abfrage")
        return
    params = load_params()
    record_close(picks, S.odds_events("basketball_nba", "h2h,spreads,totals"), params)
    save_json("picks.json", picks)
    old = load_json("status.json", {})
    save_json("status.json", {**old, "lateUpdated": now.isoformat(), "credits": S.CREDITS, "stats": stats(picks)})
    notify_problems(LOG, S.CREDITS, "Nachtlauf")
    log(f"Fertig (Nachtlauf, {len(open_later)} späte Spiele).")


if __name__ == "__main__":
    main()
