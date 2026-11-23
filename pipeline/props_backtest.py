"""Rücktest der Spieler-Simulation an vergangenen Spielen (kostenlos, nur ESPN-Spielberichte).

Für jedes Spiel seit MIN_DATE wird mit dem Wissen vom Vortag simuliert. Wer gespielt hat, gilt als verfügbar
(den Verletzungsbericht von damals haben wir nicht), Team-Punkte = Schnitt der bisherigen Spiele beider Teams.
Gemessen: Abweichung des Simulations-Mittels, Log-Score (wie wahrscheinlich war der echte Wert laut Simulation)
und ob die mittleren 50 % der Simulation in 50 % der Fälle treffen. Ein Raster sucht die beste Streuung pro Wert
und die beste Schrumpfung der Raten."""
import math
from collections import defaultdict

import numpy as np

from . import props as PR
from .common import load_json, log, save_json

MIN_DATE = "2026-11-08"
GRID_DISP = (0.5, 1.0, 1.6, 2.5)                  # PHI: zusätzliche Streuung je Wert
GRID_K = (25.0, 80.0, 200.0)


def _games(pstats):
    """{(Datum, Team): {Spieler: Zeile}} aus den Spielberichten."""
    out = defaultdict(dict)
    for k, P in pstats["players"].items():
        team, pk = k.split("|", 1)
        for g in P["games"]:
            out[(g[0], team)][pk] = g
    return out


def _team_points(by_game, team, before):
    pts = [sum(r[2] for r in rows.values()) for (d, t), rows in by_game.items() if t == team and d < before]
    return sum(pts) / len(pts) if len(pts) >= 3 else None


def run(max_games=220, seed=3):
    pstats = PR_load()
    by_game = _games(pstats)
    pairs = defaultdict(list)                           # Datum → Teams, Gegner aus Spalte 6
    for (d, t), rows in by_game.items():
        opp = next(iter(rows.values()))[6]
        pairs[d].append((t, opp))
    matchups = []
    for d in sorted(pairs):
        if d < MIN_DATE:
            continue
        seen = set()
        for t, o in pairs[d]:
            if (o, t) in seen or (d, o) not in by_game:
                continue
            seen.add((t, o))
            matchups.append((d, t, o))
    matchups = matchups[-max_games:]
    results = {}
    for K in GRID_K:
        PR.K_RATE = K
        lg = PR.league_rates(pstats)
        scores = {(s, k): [] for s in PR.STATS for k in GRID_DISP}
        bias = {s: [] for s in PR.STATS}
        cover = {s: [] for s in PR.STATS}
        mins_err = []
        rng = np.random.default_rng(seed)
        for d, t1, t2 in matchups:
            r1 = PR.roster(pstats, t1, d, lg, [])
            r2 = PR.roster(pstats, t2, d, lg, [])
            act1, act2 = by_game[(d, t1)], by_game[(d, t2)]
            for rows, act in ((r1, act1), (r2, act2)):
                for r in rows:
                    r["pOut"] = 0.0 if r["key"] in act else 1.0
            p1, p2 = _team_points(by_game, t1, d), _team_points(by_game, t2, d)
            if not p1 or not p2:
                continue
            game = {"margin": 0.0, "poss": PR.LEAGUE_POSS, "ptsH": p1, "ptsA": p2}
            for k_disp in GRID_DISP:
                PR.PHI = {s: k_disp for s in PR.STATS}
                sims = PR.simulate(game, r1, r2, 13.0, rng)
                for rows, act in ((r1, act1), (r2, act2)):
                    for r in rows:
                        a = act.get(r["key"])
                        if not a or r["key"] not in sims:
                            continue
                        played = sims[r["key"]]["min"] > 0
                        for s in PR.STATS:
                            v = sims[r["key"]][s][played]
                            if not len(v):
                                continue
                            y = a[PR.IDX[s]]
                            p = ((v == y).sum() + 0.5) / (len(v) + 10)
                            scores[(s, k_disp)].append(math.log(p))
                            if k_disp == GRID_DISP[2]:
                                bias[s].append(float(v.mean()) - y)
                                lo, hi = np.percentile(v, 25), np.percentile(v, 75)
                                cover[s].append(1.0 if lo <= y <= hi else 0.0)
        best = {s: max(GRID_DISP, key=lambda k: np.mean(scores[(s, k)])) for s in PR.STATS}
        results[K] = {"best_disp": best,
                      "score": {s: round(float(np.mean(scores[(s, best[s])])), 4) for s in PR.STATS},
                      "score_all": {f"{s}@{k}": round(float(np.mean(v)), 4) for (s, k), v in scores.items()},
                      "bias": {s: round(float(np.mean(v)), 2) for s, v in bias.items()},
                      "mae": {s: round(float(np.mean(np.abs(v))), 2) for s, v in bias.items()},
                      "cover50": {s: round(float(np.mean(v)), 3) for s, v in cover.items()},
                      "n": len(bias["pts"])}
        log(f"Spieler-Rücktest K={K}: {results[K]['n']} Spielerspiele, Streuung {best}, Log-Score {results[K]['score']}, "
            f"Abweichung {results[K]['bias']}")
    bestK = max(GRID_K, key=lambda K: sum(results[K]["score"].values()))
    rep = {"games": len(matchups), "bestK": bestK, "results": {str(k): v for k, v in results.items()}}
    save_json("props_backtest.json", rep)
    return rep


def PR_load():
    from .playerstats import load
    return load()


if __name__ == "__main__":
    import pipeline.common as C
    C.DATA_DIR = "docs/data"
    r = run()
    print(r["bestK"], r["results"][str(r["bestK"])])
