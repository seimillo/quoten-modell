"""Tägliche Pipeline: Daten holen → Modelle rechnen → Trefferliste pflegen → JSON für die Website schreiben.

Aufruf:  python -m pipeline.run            (braucht ODDS_API_KEY als Umgebungsvariable)
         python -m pipeline.selftest       (Offline-Test mit Beispieldaten)
"""
from . import sources as S
from .common import LOG, load_json, log, now_utc, parse_time, save_json
from .nba import canon, model_games
from .tennis import model_matches
import os

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


def main():
    if os.environ.get("RUN_MODE") == "close":
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
    nba_scores = []
    if "basketball_nba" in active:
        season = nba_season(now)
        cur, prior = S.bbref_ratings(season), S.bbref_ratings(season - 1)
        pace = S.bbref_pace(season)
        injuries = S.espn_injuries()
        cur_pl, prior_pl = S.bbref_players(season), S.bbref_players(season - 1)
        nba_scores = S.odds_scores("basketball_nba", 3)
        events = S.odds_events("basketball_nba", "h2h,spreads,totals")
        upcoming = [e for e in events if parse_time(e["commence_time"]) > now]
        nba = model_games(upcoming, cur, prior, pace, nba_scores, injuries, kalshi_nba, pm_nba, cur_pl, prior_pl, params["nba"])
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
    scores = scores_map(nba_scores)
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
    """Abendlauf kurz vor den NBA-Spielen: nur Schlussquoten holen und den CLV jeder offenen Prognose speichern."""
    now = now_utc()
    picks = load_json("picks.json", {})
    params = load_params()
    open_recs = [r for r in picks.values() if not r.get("result") and parse_time(r["commence"]) > now]
    keys = sorted({r.get("sportKey") for r in open_recs if r.get("sportKey")})
    keys = [k for k in keys if k == "basketball_nba"] + [k for k in keys if k != "basketball_nba"][:2]
    events = []
    S.odds_sports()                                   # liest nur das Credit-Guthaben aus, kostet nichts
    for k in keys:
        events += S.odds_events(k, "h2h,spreads,totals" if k == "basketball_nba" else "h2h")
    record_close(picks, events, params)
    save_json("picks.json", picks)
    old = load_json("status.json", {})
    save_json("status.json", {**old, "closeUpdated": now.isoformat(), "credits": S.CREDITS, "stats": stats(picks),
                              "closeLog": LOG[-30:]})
    log("Fertig (Schlussquoten).")


if __name__ == "__main__":
    main()
