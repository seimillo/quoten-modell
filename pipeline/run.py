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


def nba_block(now, params, kalshi_nba, pm_nba, history, recent_min=None, op="normal"):
    season = nba_season(now)
    cur, prior = S.bbref_ratings(season), S.bbref_ratings(season - 1)
    pace = S.bbref_pace(season)
    cur_pl, prior_pl = S.bbref_players(season), S.bbref_players(season - 1)
    injuries, stamp = merged_injuries(now, {k for k, _ in list(cur_pl) + list(prior_pl)})
    events = S.odds_events("basketball_nba", "h2h,spreads,totals")
    if op != "off":                                   # Abendlauf ohne OddsPapi (Kontingent), vor Spielbeginn mit allen
        try:
            from . import oddspapi
            oddspapi.attach([e for e in events if parse_time(e["commence_time"]) > now], extra=(op == "extra"))
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
    links = {e["id"]: e.get("links") for e in events if e.get("links")}
    for g in nba:
        g["links"] = links.get(g["id"]) or {}
        g["injuryReport"] = stamp
        g["sdUsed"], g["sdMarket"] = P["sd_margin"], msd
    return nba, events


def props_block(nba, events, pstats, now, mode="mittags"):
    """Spielerwetten simulieren und speichern. mittags: ✓ in die Trefferliste, Linien fürs Lernen merken.
    vor Spielbeginn: Mittags-✓ nachprüfen, neue ✓ aufnehmen, Schlussquoten (CLV)."""
    from . import props as PR
    from .nba import canon
    from .sources import book_prices
    import statistics
    try:
        sd = nba[0].get("sdUsed", 13.0) if nba else 13.0
        # Spielrahmen aus dem Markt: Gesamtpunkte und Handicap (Pinnacle, sonst Mittel aller Buchmacher)
        by_id = {e["id"]: e for e in events}
        framed = []
        for g in nba:
            e = by_id.get(g["id"])
            g2 = dict(g)
            if e:
                tot = book_prices(e, "totals")
                spr = book_prices(e, "spreads")
                tl = [o["point"] for b, out in tot.items() for o in out if o["name"] == "Over" and o.get("point")]
                sl = [o["point"] for b, out in spr.items() for o in out if canon(o["name"]) == g["home"] and o.get("point") is not None]
                pin_t = next((o["point"] for o in tot.get("pinnacle", []) if o["name"] == "Over"), None)
                pin_s = next((o["point"] for o in spr.get("pinnacle", []) if canon(o["name"]) == g["home"]), None)
                T = pin_t if pin_t else (statistics.median(tl) if tl else None)
                L = pin_s if pin_s is not None else (statistics.median(sl) if sl else None)
                if T and L is not None:
                    g2.update({"ptsH": (T - L) / 2, "ptsA": (T + L) / 2, "margin": -L, "frame": "Markt"})
            framed.append(g2)
        season = nba_season(now)
        tot_cur = S.bbref_totals(season)                       # täglich neu
        tot_prior = S.bbref_totals(season - 1, cache_days=None)   # einmal holen, dann dauerhaft
        games = PR.evaluate(framed, events, pstats, {}, sd, tot_cur=tot_cur, tot_prior=tot_prior)
    except Exception as e:  # noqa: BLE001
        log(f"Spielerwetten fehlgeschlagen: {e}")
        return []
    if mode == "mittags":
        PR.save_lines(games, now)
    for g in games:
        g.pop("_lines", None)
    old = load_json("props.json", {})
    keep = [g for g in old.get("games", []) if parse_time(g["commence"]) <= now] if mode != "mittags" else []
    ids = {g["id"] for g in games}
    save_json("props.json", {"updated": now.isoformat(), "mode": mode, "games": games + [g for g in keep if g["id"] not in ids]})
    if mode == "mittags":
        PR.track(games, now)
        PR.save_sims(games, now)
        return games
    checked = PR.recheck(games, now)
    new = PR.track(games, now, src="vor Spielbeginn")
    PR.record_close(games, now)
    from .notify import notify_props_pregame
    notify_props_pregame(checked, new)
    return games


def player_results(pstats, now, days=12):
    """Kleine Datei mit den letzten Spielerwerten – damit „Meine Wetten“ Spielerwetten selbst auswerten kann."""
    cut = (now.date() - timedelta(days=days)).isoformat()
    out = {}
    for k, P in pstats["players"].items():
        rows = [g[:6] for g in P.get("games", []) if g[0] >= cut]
        if rows:
            out[k] = rows
    return {"updated": now.isoformat(), "cols": ["date", "min", "pts", "reb", "ast", "fg3m"], "players": out}


def main():
    mode = os.environ.get("RUN_MODE")
    if mode == "close":
        return main_close()
    if mode == "pregame":
        return main_close(pregame=True)
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
    if mode == "props_backtest":
        from .props_backtest import run_variants
        try:
            run_variants()
        except Exception as e:  # noqa: BLE001
            log(f"Spieler-Rücktest fehlgeschlagen: {e}")
        old = load_json("status.json", {})
        save_json("status.json", {**old, "propsBacktestLog": LOG[-12:]})
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
    from . import playerstats, props as PR
    pstats = playerstats.update(history)
    prop_games = []
    if "basketball_nba" in active:
        nba, events_all = nba_block(now, params, kalshi_nba, pm_nba, history, recent_min)
        for g in nba:
            g["middayBest"] = g["best"]
        prop_games = props_block(nba, events_all, pstats, now, "mittags")
    else:
        log("NBA: aktuell keine Spiele bei The Odds API")

    # ---------- Trefferliste ----------
    update_picks(picks, nba)
    open_ids = {k for k, r in picks.items() if not r.get("result")}
    settle(picks, nba_scores_from_history(picks, history, now))
    notify_results([picks[k] for k in open_ids if picks[k].get("result")])
    notify_value_bets(nba)
    T_before = {k for k, t in load_json("props_picks.json", {}).items() if not t.get("result")}
    T = PR.settle(pstats, now)
    PR.learn(pstats, now)
    PR.learn_conf(pstats, now)
    save_json("player_results.json", player_results(pstats, now))
    from .notify import notify_props, notify_props_results
    notify_props_results([T[k] for k in T_before if k in T and T[k].get("result")])
    notify_props(prop_games)
    params = calibrate(picks, params)                 # Modell lernt aus allen ausgewerteten Spielen
    save_params(params)

    save_json("nba.json", nba)
    save_json("picks.json", picks)
    old = load_json("status.json", {})
    save_json("status.json", {"updated": now.isoformat(), "credits": S.CREDITS, "stats": stats(picks), "log": LOG[-60:],
                              "propStats": PR.stats(),
                              "closeUpdated": old.get("closeUpdated"), "closeLog": old.get("closeLog", []),
                              "params": {k: params[k] for k in ("nba", "n")}})
    notify_problems(LOG, S.CREDITS, "Mittagslauf")
    log("Fertig.")


def main_close(pregame=False):
    """Abendlauf (pregame=False, 23:37 Uhr, ohne OddsPapi) bzw. Lauf kurz vor Spielbeginn (pregame=True, ca. 0:22 Uhr,
    mit allen OddsPapi-Anbietern und Spielerwetten). Nach dem NBA-Verletzungsbericht von 17:30 Uhr US-Zeit:
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
                                    load_json("games.json", []), op="extra" if pregame else "off")
        if pregame:
            from . import playerstats
            props_block(nba, nba_events, playerstats.load(), now, "vor Spielbeginn")
        events += nba_events
        for g in nba:
            mb = (old.get(g["id"]) or {}).get("middayBest")
            g["middayBest"] = mb
            g["eveningUpdate"] = now.isoformat()
            g["updateKind"] = "vor Spielbeginn" if pregame else "abends"
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
    from . import props as PR
    extra = {"pregameUpdated": now.isoformat(), "pregameLog": LOG[-30:], "propStats": PR.stats()} if pregame else \
        {"closeUpdated": now.isoformat(), "closeLog": LOG[-30:]}
    save_json("status.json", {**old, **extra, "credits": S.CREDITS, "stats": stats(picks)})
    notify_problems(LOG, S.CREDITS, "Lauf vor Spielbeginn" if pregame else "Abendlauf")
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
