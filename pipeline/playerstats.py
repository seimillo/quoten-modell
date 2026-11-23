"""Spielerwerte jedes Spiels der Saison (ESPN-Spielberichte, kostenlos): Minuten, Punkte, Rebounds, Assists, Dreier.
Gespeichert in docs/data/pstats.json; jeder Lauf holt nur neue Spiele."""
from . import sources as S
from .common import load_json, log, save_json
from .nba import TEAMS, canon
from .sources import player_key

FILE = "pstats.json"


def update(hist, max_new=400):
    D = load_json(FILE, {"done": [], "players": {}})
    done = set(D["done"])
    todo = [g for g in hist if g.get("done") and g.get("eid") and g["eid"] not in done][-max_new:]
    ok = 0
    for g in todo:
        box = S.espn_boxscore_stats(g["eid"])
        if box is None:
            continue
        ok += 1
        done.add(g["eid"])
        h, a = canon(g["home"]), canon(g["away"])
        for team, players in box.items():
            t = canon(team)
            if t not in TEAMS:
                continue
            opp = a if t == h else h
            for name, r in players.items():
                key = f"{TEAMS[t][0]}|{player_key(name)}"
                P = D["players"].setdefault(key, {"name": name, "games": []})
                P["name"] = name
                P["games"].append([g["date"], r["min"], r["pts"], r["reb"], r["ast"], r["fg3m"], TEAMS.get(opp, ("?",))[0]])
    for P in D["players"].values():
        P["games"] = sorted({tuple(x[:1]) + tuple(x[1:]): x for x in P["games"]}.values())[-82:]
    D["done"] = sorted(done)
    save_json(FILE, D)
    log(f"Spielerwerte: {ok} neue Spielberichte, {len(D['players'])} Spieler")
    return D


def load():
    return load_json(FILE, {"done": [], "players": {}})
