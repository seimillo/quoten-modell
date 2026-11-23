"""Rücktest: Das Modell wird auf allen bisherigen Spielen nachgerechnet – jeweils nur mit dem Wissen vor dem Spiel.

Daten: Ergebnisse der Vorsaison und der laufenden Saison von ESPN (kostet keine Credits).
Team-Stärke: eigene Ratings aus den Ergebnissen bis zum Vortag (Ridge-Regression, Vorsaison als Startwert).
Für die Vergangenheit fehlen Spielerwerte, Verletzungen und Quoten. Darum lernt der Rücktest nur:
Stärke-Gewicht (Team + Aufstellung zusammen), Heimvorteil, Ruhe, Spielplan-Dichte, Reise, Form und die Streuung.
Bewertet wird zeitlich sauber: Schätzen auf den ersten 70 % der Spiele, Prüfen auf den letzten 30 %.
"""
import math
from datetime import date, timedelta

import numpy as np

from . import sources as S
from .common import load_json, log, now_utc, phi, save_json
from .nba import ALT_BONUS, TEAMS, canon, form, rest_points, schedule, team_history
from .params import BOUNDS, load_params, save_params

SEASONS = {2026: (date(2025, 10, 21), date(2026, 4, 12))}   # Vorsaison: nur Hauptrunde
SKIP_FIRST = 150          # erste Spiele der ältesten Saison: Ratings noch ohne Grundlage
PRIOR_GAMES = 8           # Ridge: Startwert zählt wie 8 Spiele pro Team
CARRY = 0.6               # Anteil der Vorsaison-Stärke, der in die neue Saison mitgenommen wird


def fetch_season(season, start, end):
    fn = f"games_{season}.json"
    cached = load_json(fn, [])
    if cached and cached[-1]["date"] >= (end - timedelta(days=3)).isoformat():
        return cached
    games, d = [], start
    while d <= end:
        day = S.espn_scoreboard(d)
        games += day or []
        d += timedelta(days=1)
    games = [g for g in games if g["done"] and canon(g["home"]) in TEAMS and canon(g["away"]) in TEAMS]
    save_json(fn, games)
    log(f"Rücktest: Saison {season - 1}/{str(season)[2:]} geladen, {len(games)} Spiele")
    return games


def ratings(games, prior):
    """Stärke in Punkten gegenüber dem Liga-Schnitt (neutraler Platz) aus allen übergebenen Spielen."""
    teams = sorted(TEAMS)
    idx = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    rows, y = [], []
    for g in games:
        r = np.zeros(n + 1)
        r[idx[canon(g["home"])]] += 1
        r[idx[canon(g["away"])]] -= 1
        r[n] = 0.0 if g.get("neutral") else 1.0
        rows.append(r)
        y.append(g["hs"] - g["as"])
    lam = math.sqrt(PRIOR_GAMES)
    for t in teams:                                    # Startwerte als zusätzliche „Spiele“
        r = np.zeros(n + 1)
        r[idx[t]] = lam
        rows.append(r)
        y.append(lam * prior.get(t, 0.0))
    r = np.zeros(n + 1)
    r[:n] = 1.0                                        # Summe der Stärken = 0
    rows.append(r * 10)
    y.append(0.0)
    sol = np.linalg.lstsq(np.array(rows), np.array(y, dtype=float), rcond=None)[0]
    return {t: float(sol[idx[t]]) for t in teams}, float(sol[n])


def features(season_games, prior, skip):
    """Merkmale je Spiel, jeweils mit dem Stand vom Vortag."""
    by_day = {}
    for g in season_games:
        by_day.setdefault(g["date"], []).append(g)
    out, done = [], []
    for day in sorted(by_day):
        d = date.fromisoformat(day)
        R, hca = ratings(done, prior) if done else (dict(prior), 2.5)
        hist = team_history(season_games, d)

        def expect(team, opp, home):
            return R.get(team, 0) - R.get(opp, 0) + (hca if home else -hca)

        for g in by_day[day]:
            h, a = canon(g["home"]), canon(g["away"])
            if len(done) >= skip:
                sa_, sh_ = schedule(a, d, hist, h), schedule(h, d, hist, h)
                alt = ALT_BONUS if TEAMS[h][0] in ("DEN", "UTA") and not g.get("neutral") else 0.0
                out.append({"date": day, "home": h, "away": a, "y": g["hs"] - g["as"] - alt, "win": 1.0 if g["hs"] > g["as"] else 0.0, "alt": alt,
                            "strength": R.get(h, 0.0) - R.get(a, 0.0), "hca": 0.0 if g.get("neutral") else 1.0,
                            "rest": rest_points(sa_["rest"], sh_["rest"], h, a),
                            "sched": sa_["dens"] - sh_["dens"], "travel": (sa_["km"] - sh_["km"]) / 1000,
                            "form": form(h, hist, expect) - form(a, hist, expect)})
        done += by_day[day]
    final, _ = ratings(done, prior)
    return out, final


KEYS = ["strength", "hca", "rest", "sched", "travel", "form"]


START = {"strength": 1.0, "hca": 2.2, "rest": 1.0, "sched": 1.0, "travel": 0.3, "form": 0.15}
RIDGE_GAMES = 150         # Startwerte zählen wie 150 Spiele – schwache Faktoren bleiben nahe am Start


def fit(rows, keys):
    """Regression mit Bremse Richtung Startwerte (Ridge), damit Zufallsschwankungen nicht übernommen werden."""
    X = np.array([[r[k] for k in keys] for r in rows])
    y = np.array([r["y"] for r in rows], dtype=float)
    lam = np.array([RIDGE_GAMES * max(1e-6, float(np.mean(X[:, i] ** 2))) for i in range(len(keys))])
    prior = np.array([START[k] for k in keys])
    A = X.T @ X + np.diag(lam)
    beta = np.linalg.solve(A, X.T @ y + lam * prior)
    res = y - X @ beta
    sd = float(np.sqrt(res @ res / max(1, len(y) - len(keys))))
    try:
        se = np.sqrt(np.diag(np.linalg.inv(A)) * sd ** 2)
    except np.linalg.LinAlgError:
        se = np.full(len(keys), float("nan"))
    return beta, se, sd


def evaluate(rows, keys, beta, sd):
    """Brier-Wert, Log-Loss und Trefferquote der Siegchance des Heimteams."""
    brier = ll = hit = 0.0
    for r in rows:
        m = sum(b * r[k] for b, k in zip(beta, keys)) + r["alt"]
        p = min(0.999, max(0.001, phi(m / sd)))
        w = r["win"]
        brier += (p - w) ** 2
        ll -= math.log(p if w else 1 - p)
        hit += 1.0 if (p > 0.5) == (w == 1.0) else 0.0
    n = len(rows)
    return {"brier": round(brier / n, 4), "logloss": round(ll / n, 4), "hit": round(hit / n, 4), "n": n}


def run_backtest(apply=True):
    now = now_utc()
    cur_season = now.year + 1 if now.month >= 9 else now.year
    rows, prior, seasons = [], {}, []
    for season, (start, end) in sorted(SEASONS.items()):
        g = fetch_season(season, start, end)
        f, prior = features(g, {}, SKIP_FIRST)
        rows += f
        seasons.append({"season": f"{season - 1}/{str(season)[2:]}", "games": len(g), "used": len(f)})
    cur = [g for g in load_json("games.json", []) if g.get("done") and canon(g["home"]) in TEAMS and canon(g["away"]) in TEAMS]
    f, _ = features(cur, {t: CARRY * v for t, v in prior.items()}, 0)
    rows += f
    seasons.append({"season": f"{cur_season - 1}/{str(cur_season)[2:]}", "games": len(cur), "used": len(f)})
    rows.sort(key=lambda r: r["date"])
    if len(rows) < 300:
        log(f"Rücktest: zu wenige Spiele ({len(rows)})")
        return None

    cut = int(len(rows) * 0.7)
    train, test = rows[:cut], rows[cut:]
    base_keys = ["strength", "hca"]
    b0, _, sd0 = fit(train, base_keys)
    b1, _, sd1 = fit(train, KEYS)
    ev_base, ev_full = evaluate(test, base_keys, b0, sd0), evaluate(test, KEYS, b1, sd1)
    ev_coin = {"brier": 0.25}
    # Beitrag jedes Faktors: Testgüte ohne ihn
    drop = {}
    for k in KEYS[2:]:
        ks = [x for x in KEYS if x != k]
        b, _, sd_ = fit(train, ks)
        drop[k] = round(evaluate(test, ks, b, sd_)["brier"] - ev_full["brier"], 5)

    beta, se, sd = fit(rows, KEYS)
    coef = {k: {"value": round(float(b), 3), "se": round(float(s), 3)} for k, b, s in zip(KEYS, beta, se)}
    report = {"date": now.date().isoformat(), "games": len(rows), "seasons": seasons, "train": len(train), "test": len(test),
              "base": ev_base, "full": ev_full, "coin": ev_coin, "drop": drop, "coef": coef, "sd": round(sd, 2)}
    save_json("backtest.json", report)
    log(f"Rücktest: {len(rows)} Spiele · Test-Brier Basis {ev_base['brier']} → voll {ev_full['brier']} · "
        f"Trefferquote {ev_full['hit']:.1%}")

    if apply:
        P = load_params()
        B = BOUNDS["nba"]
        clip = lambda k, v: round(max(B[k][0], min(B[k][1], v)), 4)
        S_ = float(beta[0])
        new = {"team_scale": clip("team_scale", S_ / 2), "player_scale": clip("player_scale", S_ / 2),
               "hca": clip("hca", float(beta[1])), "rest_scale": clip("rest_scale", float(beta[2])),
               "sched_scale": clip("sched_scale", float(beta[3])), "travel_scale": clip("travel_scale", float(beta[4])),
               "form_scale": clip("form_scale", float(beta[5])), "sd_margin": clip("sd_margin", sd)}
        changes = [{"sport": "NBA", "key": k, "old": P["nba"][k], "new": v, "target": v} for k, v in new.items()
                   if abs(P["nba"][k] - v) > 1e-4]
        P["nba"].update(new)
        P["history"] = (P["history"] + [{"date": now.date().isoformat(), "source": "Rücktest", "changes": changes}])[-120:]
        P["backtest"] = report["date"]
        save_params(P)
        log(f"Rücktest: {len(changes)} Startwerte gesetzt")
    return report
