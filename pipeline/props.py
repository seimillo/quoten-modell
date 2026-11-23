"""Spielerwetten per Monte-Carlo-Simulation (10.000 Durchläufe pro Spiel).

Pro Spieler:
- Minuten: letzte 10 Spiele (70 %) und Saison (30 %), Ausfall-Wahrscheinlichkeit aus dem Verletzungsbericht.
  Die Minuten aller verfügbaren Spieler werden auf 240 pro Team hochgerechnet – fehlt ein Starter,
  bekommen die anderen seine Minuten.
- Werte pro Minute (Punkte, Rebounds, Assists, Dreier): Saison, zur Liga-Mitte gezogen (40 Minuten).
- Team-Rahmen aus dem Markt (Gesamtpunkte- und Handicap-Linie von Pinnacle): erwartete Team-Punkte und
  Punktdifferenz – der Markt ist darin sehr genau; die Simulation verteilt die Punkte auf die Spieler.
Pro Durchlauf: Punktdifferenz und Tempo gemeinsam für beide Teams, Team-Form, Blowouts (Starter spielen
weniger), Minuten mit Zufall, dann jeder Wert als Gamma-Poisson-Zufallszahl (mehr Streuung als Poisson).
Ergebnis: für jede angebotene Linie die Chance auf „über“ und „unter“.
Bewertung wie bei Spielen: Mischung Simulation/Markt, EV, ✓ nur bei klaren Fällen.
"""
from datetime import date

import math

import numpy as np

from .common import devig_power, load_json, log, now_utc, parse_time, save_json
from .nba import TEAMS
from .sources import BOOK_NAMES, player_key

N_SIM = 10000
K_RATE = 40.0                       # Minuten, mit denen die Liga-Mitte in die Rate eingeht
# Zusätzliche Streuung je Wert: Varianz ≈ (1 + PHI) × Erwartung. Punkte fallen in 2er- und 3er-Schritten,
# darum wächst ihre Streuung mit der Erwartung, aber nicht quadratisch. Gemessen im Rücktest (props_backtest.py).
PHI = {"pts": 1.6, "reb": 0.5, "ast": 0.5, "fg3m": 0.6}
STATS = ("pts", "reb", "ast", "fg3m")
STAT_NAME = {"pts": "Punkte", "reb": "Rebounds", "ast": "Assists", "fg3m": "Dreier"}
CONF_P = 0.3                        # Start-Gewicht der Simulation gegenüber dem Markt
# Vertrauen je Abweichung Simulation ↔ Markt: wird täglich aus allen simulierten Linien gelernt (ersetzt den alten Schutz)
GAP_BUCKETS = ((0.0, 0.05), (0.05, 0.10), (0.10, 0.15), (0.15, 1.01))
W_GRID = [round(i * 0.05, 2) for i in range(21)]
CONF_PRIOR_N, CONF_MIN_N, CONF_MAX = 400, 200, 0.7
ONE_SIDED_MARGIN = 0.07             # angenommene Marge, wenn nur „über“ angeboten wird
MIN_EV, MIN_ODDS, MAX_ODDS = 0.05, 1.5, 3.5
MIN_EV_ONE_SIDED = 0.10             # nur „über“ angeboten → Marge unsicher → strengere Schwelle
LEAGUE_POSS = 99.5
KELLY_FRAC, KELLY_CAP = 0.25, 0.01
IDX = {"min": 1, "pts": 2, "reb": 3, "ast": 4, "fg3m": 5}


def _team_dates(pstats, abbr):
    ds = set()
    for k, P in pstats["players"].items():
        if k.startswith(abbr + "|"):
            ds.update(g[0] for g in P["games"])
    return sorted(ds)


def league_rates(pstats):
    tot = {s: 0.0 for s in STATS}
    mins = 0.0
    for P in pstats["players"].values():
        for g in P["games"]:
            mins += g[1]
            for s in STATS:
                tot[s] += g[IDX[s]]
    return {s: tot[s] / mins for s in STATS} if mins else {"pts": 0.46, "reb": 0.18, "ast": 0.11, "fg3m": 0.05}


K_PRIOR_OWN = 250.0                 # Minuten, mit denen die eigene Vorsaison in die Rate eingeht


def roster(pstats, abbr, before, lg, injuries, tot_cur=None, tot_prior=None):
    """Spieler eines Teams mit erwarteten Minuten, Raten und Ausfall-Wahrscheinlichkeit.
    tot_cur / tot_prior: Saison-Gesamtwerte von Basketball-Reference ({Spielerschlüssel: {mp, pts, reb, ast, fg3m}})."""
    dates = [d for d in _team_dates(pstats, abbr) if d < before][-10:]
    out_p = {player_key(r["player"]): r.get("pOut", 0.0) for r in injuries}
    rows = []
    for k, P in pstats["players"].items():
        if not k.startswith(abbr + "|"):
            continue
        games = [g for g in P["games"] if g[0] < before]
        if not games:
            continue
        recent = [g for g in games if g[0] in dates]
        last5 = set(dates[-5:])
        played_recent = [g for g in recent if g[1] > 0]
        if not played_recent and not any(g[0] in last5 for g in games):
            continue                                # lange nicht gespielt und nicht gemeldet → nicht im Kader
        # Rotationsspieler (≥ 20 Min., wenn sie spielen): Schnitt der Spiele mit Einsatz.
        # Bankspieler: nicht eingesetzte Teamspiele zählen als 0 Minuten (sonst zu viele Teamminuten).
        m_played = (sum(g[1] for g in played_recent) / len(played_recent)) if played_recent else \
            sum(g[1] for g in games) / len(games)
        m_all = sum(g[1] for g in recent) / max(1, len(dates))
        all_dates = [d for d in _team_dates(pstats, abbr) if d < before]
        m_season_played = sum(g[1] for g in games) / len(games)
        m_season_all = sum(g[1] for g in games) / max(1, len(all_dates))
        if m_played >= 20:
            m_recent, m_season = m_played, m_season_played
        else:
            m_recent, m_season = m_all, m_season_all
        exp_min = 0.7 * m_recent + 0.3 * m_season
        pkey = k.split("|", 1)[1]
        p_out = out_p.get(pkey, 0.0)
        if pkey not in out_p and dates:
            last3 = set(dates[-3:])
            r3 = sum(1 for g in games if g[0] in last3 and g[1] > 0) / max(1, len(last3))
            if r3 == 0:
                p_out = 0.85                        # die letzten 3 Teamspiele gefehlt, aber nicht gemeldet
            elif r3 < 1:
                p_out = 0.3 * (1 - r3)
        tot_min = sum(g[1] for g in games)
        cur_sum = {s: sum(g[IDX[s]] for g in games) for s in STATS}
        c = (tot_cur or {}).get(pkey)
        if c and c.get("mp", 0) > tot_min:                  # volle Saison (auch Spiele ohne ESPN-Bericht)
            tot_min, cur_sum = c["mp"], {s: c[s] for s in STATS}
        pr = (tot_prior or {}).get(pkey)
        if pr and pr.get("mp", 0) >= 300:                   # eigene Vorsaison als Grundlage
            rates = {s: (cur_sum[s] + K_PRIOR_OWN * pr[s] / pr["mp"]) / (tot_min + K_PRIOR_OWN) for s in STATS}
        else:
            rates = {s: (cur_sum[s] + K_RATE * lg[s]) / (tot_min + K_RATE) for s in STATS}
        rows.append({"key": pkey, "name": P["name"], "min": exp_min, "pOut": p_out, "rates": rates, "gp": len(games),
                     "role": role_of(rates)})
    return rows


ROLE_OPP = False                    # Gegner-Effekt je Rolle (Big/Guard/Flügel) statt je Team – im Rücktest geprüft


def role_of(rates):
    """Rolle aus den Werten pro Minute (Positionen stehen nicht in den Daten)."""
    if rates["reb"] >= 0.25:
        return "big"
    if rates["ast"] >= 0.15:
        return "guard"
    return "wing"


def opponent_role_factors(pstats, before):
    """Wie viel jedes Team je Rolle zulässt (Punkte, Rebounds, Assists, Dreier) im Verhältnis zur Liga, geglättet."""
    # Rolle je Spieler aus seiner Saison bis zum Vortag
    role = {}
    for k, P in pstats["players"].items():
        g = [x for x in P["games"] if x[0] < before]
        m = sum(x[1] for x in g)
        if m >= 60:
            role[k] = role_of({"reb": sum(x[3] for x in g) / m, "ast": sum(x[4] for x in g) / m})
    al, mn, gm = {}, {}, {}
    for k, P in pstats["players"].items():
        r = role.get(k)
        if not r:
            continue
        for x in P["games"]:
            if x[0] >= before:
                continue
            key = (x[6], r)
            mn[key] = mn.get(key, 0.0) + x[1]
            a = al.setdefault(key, {s: 0.0 for s in STATS})
            for s in STATS:
                a[s] += x[IDX[s]]
            gm.setdefault(x[6], set()).add(x[0])
    lg = {}
    for r in ("big", "guard", "wing"):
        m = sum(v for (t, rr), v in mn.items() if rr == r)
        lg[r] = {s: sum(a[s] for (t, rr), a in al.items() if rr == r) / m if m else 0 for s in STATS}
    out = {}
    for (t, r), a in al.items():
        n = len(gm.get(t, ()))
        w = 0.5 * n / (n + 10)
        out.setdefault(t, {})[r] = {s: 1 + w * ((a[s] / mn[(t, r)]) / lg[r][s] - 1) if lg[r][s] and mn[(t, r)] else 1.0
                                   for s in STATS}
    return out


def opponent_factors(pstats, before):
    """Wie viel jedes Team zulässt (Rebounds, Assists, Dreier pro 240 Minuten) im Verhältnis zur Liga, geglättet."""
    allowed, mins, games = {}, {}, {}
    for P in pstats["players"].values():
        for g in P["games"]:
            if g[0] >= before:
                continue
            opp = g[6]
            mins[opp] = mins.get(opp, 0.0) + g[1]
            for s in ("reb", "ast", "fg3m"):
                allowed.setdefault(opp, {}).setdefault(s, 0.0)
                allowed[opp][s] += g[IDX[s]]
            games.setdefault(opp, set()).add(g[0])
    lg = {s: sum(a[s] for a in allowed.values()) / max(1.0, sum(mins.values())) for s in ("reb", "ast", "fg3m")}
    out = {}
    for t, a in allowed.items():
        n = len(games[t])
        w = 0.5 * n / (n + 10)                               # höchstens halb, bei wenigen Spielen weniger
        out[t] = {s: 1 + w * ((a[s] / mins[t]) / lg[s] - 1) if lg[s] else 1.0 for s in ("reb", "ast", "fg3m")}
    return out


def conf_table():
    """Gelerntes Gewicht der Simulation je Abweichungsklasse (Standard CONF_P, solange zu wenig Daten)."""
    Pp = load_json("props_params.json", {})
    t = Pp.get("confGap") or {}
    return [float((t.get(f"{lo:.2f}") or {}).get("w", CONF_P)) for lo, _ in GAP_BUCKETS]


def conf_for(gap, table):
    for (lo, hi), w in zip(GAP_BUCKETS, table):
        if lo <= gap < hi:
            return w
    return table[-1]


def corrections():
    """Gelernte Korrekturfaktoren je Wert (1.0 = keine Korrektur)."""
    c = load_json("props_params.json", {}).get("corr", {})
    return {s: float(c.get(s, 1.0)) for s in STATS}


def simulate(game, home_rows, away_rows, sd_margin, rng, corr=None, opp_f=None):
    """10.000 Durchläufe eines Spiels → {Spielerschlüssel: {stat: Array der simulierten Werte}}."""
    res = {}
    M, poss = game["margin"], game.get("poss", LEAGUE_POSS)
    margin = rng.normal(M, sd_margin, N_SIM)
    pace = rng.normal(1.0, 0.04, N_SIM)
    blow = np.clip(1 - 0.008 * np.maximum(0, np.abs(margin) - 15), 0.82, 1.0)   # Starter sitzen bei Blowouts
    for rows, target, side in ((home_rows, game["ptsH"], "home"), (away_rows, game["ptsA"], "away")):
        if not rows:
            continue
        avail = np.array([1 - r["pOut"] for r in rows])
        base_min = np.array([r["min"] for r in rows])
        exp_tot = float((base_min * avail).sum())
        scale = min(1.3, max(0.7, 240 / exp_tot)) if exp_tot > 0 else 1.0
        exp_min = np.minimum(base_min * scale, 42)
        f_pace = poss / LEAGUE_POSS
        opp = (opp_f or {}).get(side, {})
        team_form = rng.normal(1.0, 0.05, N_SIM)
        plays = rng.random((N_SIM, len(rows))) < avail                 # wer heute spielt
        starter = exp_min >= 24
        mins = exp_min[None, :] * rng.normal(1.0, 0.13, (N_SIM, len(rows)))
        mins = np.where(starter[None, :], mins * blow[:, None], mins * (2 - blow[:, None]))
        mins = np.clip(mins, 0, 48) * plays
        # fehlt jemand im Durchlauf, verteilen sich seine Minuten anteilig auf die anderen (Team = 240)
        tot = mins.sum(axis=1, keepdims=True)
        mins = np.where(tot > 0, mins * np.clip(240 / np.maximum(tot, 1), 0.85, 1.3), 0)
        mins = np.minimum(mins, 48)
        roles = opp.get("_roles") if isinstance(opp, dict) else None
        for s in STATS:
            rate = np.array([r["rates"][s] for r in rows])
            if roles:                                                    # Gegner-Effekt je Rolle
                rate = rate * np.array([roles.get(r.get("role", "wing"), {}).get(s, 1.0) for r in rows])
            # Team-Summe, die verteilt wird: Punkte = Markt-Rahmen, sonst alle gesund × Tempo × Gegner
            if s == "pts":
                team_target = target
            else:
                team_target = float((np.minimum(base_min * min(1.3, max(0.7, 240 / max(1.0, base_min.sum()))), 42)
                                     * rate).sum()) * f_pace * (1.0 if roles else opp.get(s, 1.0))
                if s == "fg3m":
                    exp_p = float((exp_min * avail * np.array([r["rates"]["pts"] for r in rows])).sum())
                    team_target *= (target / exp_p) if exp_p > 0 else 1.0     # mehr Punkte → mehr Dreier
            # Umverteilung je Durchlauf: Fehlendes geht vor allem an die, die ohnehin viel davon haben
            base = mins * rate[None, :]
            S = base.sum(axis=1, keepdims=True)
            D = np.where(S > 0, team_target / np.maximum(S, 1e-9), 1.0)
            w = np.sqrt(rate)[None, :]                               # weiche Gewichtung (Wurzel der Rate)
            wbar = np.where(S > 0, (base * w).sum(axis=1, keepdims=True) / np.maximum(S, 1e-9), 1.0)
            m = np.clip(1 + (D - 1) * w / np.maximum(wbar, 1e-9), 0.6, 1.6)
            lam = base * m * (pace * team_form)[:, None] * (corr or {}).get(s, 1.0)
            k = np.maximum(lam / PHI[s], 0.2)                       # Gamma-Form wächst mit der Erwartung
            vals = rng.poisson(lam * rng.gamma(k, 1 / k))
            for j, r in enumerate(rows):
                res.setdefault(r["key"], {})[s] = vals[:, j]
        for j, r in enumerate(rows):
            res[r["key"]]["min"] = mins[:, j]
    return res


def evaluate(games, events, pstats, injuries_by_team, sd_margin, seed=7, tot_cur=None, tot_prior=None):
    """Alle angebotenen Spielerlinien bewerten. Gibt Liste je Spiel zurück."""
    rng = np.random.default_rng(seed)
    lg = league_rates(pstats)
    corr = corrections()
    ctab = conf_table()
    ev_by_id = {e["id"]: e for e in events}
    out = []
    for g in games:
        e = ev_by_id.get(g["id"])
        offers = (e or {}).get("props") or []
        if not offers:
            continue
        from datetime import timedelta
        before = (parse_time(g["commence"]) - timedelta(hours=5)).date().isoformat()   # US-Datum des Spiels
        inj_h = (g.get("injuries") or {}).get("home") or injuries_by_team.get(g["home"], [])
        inj_a = (g.get("injuries") or {}).get("away") or injuries_by_team.get(g["away"], [])
        hr = roster(pstats, g["abbrH"], before, lg, inj_h, tot_cur, tot_prior)
        ar = roster(pstats, g["abbrA"], before, lg, inj_a, tot_cur, tot_prior)
        if ROLE_OPP:
            of = opponent_role_factors(pstats, before)
            opp_f = {"home": {"_roles": of.get(g["abbrA"], {})}, "away": {"_roles": of.get(g["abbrH"], {})}}
        else:
            of = opponent_factors(pstats, before)
            opp_f = {"home": of.get(g["abbrA"], {}), "away": of.get(g["abbrH"], {})}
        sims = simulate(g, hr, ar, sd_margin, rng, corr, opp_f)
        info = {r["key"]: (r, g["abbrH"]) for r in hr}
        info.update({r["key"]: (r, g["abbrA"]) for r in ar})
        # faire Wahrscheinlichkeiten aus zweiseitigen Angeboten (z. B. bwin): (Spieler, Wert) → {Linie: P(über)}
        fair = {}
        for o in offers:
            if o.get("over") and o.get("under"):
                k = player_key(o["player"])
                fair.setdefault((k, o["stat"]), {})[o["line"]] = devig_power(o["over"], o["under"])
        # beste Quote je (Spieler, Wert, Linie, Seite) über alle Buchmacher
        lines = {}
        for o in offers:
            k = player_key(o["player"])
            if k not in sims:
                continue
            for side in ("over", "under"):
                q = o.get(side)
                if not q:
                    continue
                other = o.get("under" if side == "over" else "over")
                key = (k, o["stat"], o["line"], side)
                cand = {"odds": q, "book": o["book"], "twoSided": bool(other),
                        "pMkt": devig_power(q, other) if other else None}
                if key not in lines or q > lines[key]["odds"]:
                    lines[key] = cand
        props = []
        for (k, stat, line, side), c in lines.items():
            r, team = info[k]
            played = sims[k]["min"] > 0               # spielt er nicht, wird die Wette zurückgezahlt
            vals = sims[k][stat][played] if played.any() else sims[k][stat]
            p_over = float((vals > line).mean())
            p_sim = p_over if side == "over" else 1 - p_over
            anchored = False
            if c["pMkt"] is None:
                f = fair.get((k, stat))
                if f:
                    # nächstgelegene faire Linie eines anderen Buchmachers, mit der Form der Simulation verschoben
                    l0 = min(f, key=lambda x: abs(x - line))
                    po = f[l0] + (p_over - float((vals > l0).mean()))
                    po = min(0.98, max(0.02, po))
                    c["pMkt"] = po if side == "over" else 1 - po
                    anchored = True
                else:
                    c["pMkt"] = min(0.97, (1 / c["odds"]) / (1 + ONE_SIDED_MARGIN))
            w = conf_for(abs(p_sim - c["pMkt"]), ctab)
            p = w * p_sim + (1 - w) * c["pMkt"]
            ev = p * c["odds"] - 1
            props.append({"player": r["name"], "key": k, "team": team, "stat": stat, "statName": STAT_NAME[stat],
                          "line": line, "side": side, "odds": c["odds"], "book": c["book"],
                          "bookName": BOOK_NAMES.get(c["book"], c["book"]), "twoSided": c["twoSided"], "anchored": anchored,
                          "mean": round(float(vals.mean()), 1), "median": float(np.median(vals)),
                          "expMin": round(float(sims[k]["min"][played].mean()) if played.any() else 0.0, 1),
                          "pOut": round(r["pOut"], 2), "gp": r["gp"],
                          "pSim": round(p_sim, 4), "pMkt": round(c["pMkt"], 4), "p": round(p, 4), "ev": round(ev, 4), "w": w,
                          "evRaw": round(p_sim * c["odds"] - 1, 4)})
        # ✓: klare Fälle, eine pro Spieler, höchstens 3 pro Spiel
        cands = sorted([p for p in props if p["ev"] >= (MIN_EV if (p["twoSided"] or p["anchored"]) else MIN_EV_ONE_SIDED) and MIN_ODDS <= p["odds"] <= MAX_ODDS and p["gp"] >= 5
                        and p["expMin"] >= 18 and p["pOut"] <= 0.15], key=lambda x: -x["ev"])
        # „mittags sicher“: Bericht beider Teams da, Spieler selbst gesund, kein wichtiger Mitspieler/Gegner fraglich
        rm = g.get("reportMissing") or {}
        rows_by_team = {g["abbrH"]: hr, g["abbrA"]: ar}

        def risks(p):
            out = []
            own, opp = (("home", "away") if p["team"] == g["abbrH"] else ("away", "home"))
            if rm.get(own):
                out.append("Verletzungsbericht des Teams fehlt noch")
            if rm.get(opp):
                out.append("Verletzungsbericht des Gegners fehlt noch")
            if p["pOut"] > 0.05:
                out.append("selbst fraglich")
            for r in rows_by_team.get(p["team"], []):
                if r["key"] != p["key"] and r["min"] >= 20 and 0.1 <= r["pOut"] <= 0.9:
                    out.append(f"Mitspieler {r['name']} fraglich")
            other = g["abbrA"] if p["team"] == g["abbrH"] else g["abbrH"]
            for r in rows_by_team.get(other, []):
                if r["min"] >= 28 and 0.1 <= r["pOut"] <= 0.9:
                    out.append(f"Gegner {r['name']} fraglich")
            return out
        seen = set()
        for p in cands:
            if p["key"] in seen or len(seen) >= 3:
                continue
            seen.add(p["key"])
            p["value"] = True
            p["stake"] = round(min(KELLY_CAP, KELLY_FRAC * p["ev"] / (p["odds"] - 1)), 4)
            p["checkApp"] = p["stat"] != "pts"
            p["risks"] = risks(p)
            p["safe"] = not p["risks"]
        props.sort(key=lambda x: (-bool(x.get("value")), -x["ev"]))
        lines_out = {}
        for p in sorted(props, key=lambda x: not x["twoSided"]):
            if not (p["twoSided"] or p["anchored"]):
                continue
            ov = p["side"] == "over"
            lines_out.setdefault((p["key"], p["stat"], p["line"]),
                                 [p["team"], p["key"], p["stat"], p["line"],
                                  round(p["pSim"] if ov else 1 - p["pSim"], 4), round(p["pMkt"] if ov else 1 - p["pMkt"], 4)])
        summary = {}
        for k, sv in sims.items():
            pl = sv["min"] > 0
            if pl.mean() < 0.5:
                continue
            summary[f"{info[k][1]}|{k}"] = {s_: [round(float(sv[s_][pl].mean()), 2), float(np.percentile(sv[s_][pl], 25)),
                                                 float(np.percentile(sv[s_][pl], 75))] for s_ in STATS}
        out.append({"id": g["id"], "commence": g["commence"], "label": f"{g['abbrA']} @ {g['abbrH']}", "simSummary": summary,
                    "home": g["home"], "away": g["away"], "nProps": len(props), "props": props[:60],
                    "links": (e or {}).get("links", {}), "_lines": list(lines_out.values())})
    n_val = sum(1 for x in out for p in x["props"] if p.get("value"))
    log(f"Spielerwetten: {sum(x['nProps'] for x in out)} Linien in {len(out)} Spielen simuliert, {n_val} ✓")
    return out


# ---------------- Trefferliste der Spielerwetten ----------------

def _pid(game_id, p):
    return f"{game_id}|{p['key']}|{p['stat']}|{p['line']}|{p['side']}"


def track(prop_games, now=None, src="mittags"):
    """✓-Spielerwetten speichern. Schon gespeicherte bleiben unverändert (zählt die erste Empfehlung)."""
    now = now or now_utc()
    T = load_json("props_picks.json", {})
    new = []
    for g in prop_games:
        if parse_time(g["commence"]) <= now:
            continue
        for p in g["props"]:
            if p.get("value") and _pid(g["id"], p) not in T:
                new.append((g, p))
                T[_pid(g["id"], p)] = {"src": src, **{k: p[k] for k in ("player", "key", "team", "stat", "statName", "line", "side",
                                                               "odds", "book", "bookName", "pSim", "p", "ev", "stake", "mean")},
                                       "safe": p.get("safe"), "risks": p.get("risks", []),
                                       "game": g["label"], "commence": g["commence"], "result": None, "value": None}
    save_json("props_picks.json", T)
    return new


def recheck(prop_games, now=None):
    """Kurz vor Spielbeginn: gilt die Empfehlung von mittags noch? (neue Ausfälle, Aufstellungen, Quoten)"""
    now = now or now_utc()
    T = load_json("props_picks.json", {})
    by = {_pid(g["id"], p): p for g in prop_games for p in g["props"]}
    by_loose = {}
    for g in prop_games:
        for p in g["props"]:
            by_loose.setdefault((g["id"], p["key"], p["stat"], p["side"]), []).append(p)
    out = []
    for pid, t in T.items():
        if t.get("result") or parse_time(t["commence"]) <= now or t.get("src", "mittags") != "mittags":
            continue
        p = by.get(pid)
        if p is None:
            gid = pid.split("|", 1)[0]
            alt = by_loose.get((gid, t["key"], t["stat"], t["side"]))
            if not alt:
                status = "nicht mehr angeboten"
            else:
                a = min(alt, key=lambda x: abs(x["line"] - t["line"]))
                status = f"Linie jetzt {a['line']:g}"
                t["pre"] = {"status": status, "line": a["line"], "odds": a["odds"], "ev": a["ev"], "pSim": a["pSim"],
                            "at": now.isoformat(timespec="minutes")}
                out.append((t, t["pre"]))
                continue
            t["pre"] = {"status": status, "at": now.isoformat(timespec="minutes")}
        else:
            status = "bestätigt" if p.get("value") else "schwächer" if p["ev"] > 0 else "zurückziehen"
            t["pre"] = {"status": status, "odds": p["odds"], "ev": p["ev"], "pSim": p["pSim"], "pOut": p["pOut"],
                        "at": now.isoformat(timespec="minutes")}
        out.append((t, t["pre"]))
    save_json("props_picks.json", T)
    log("Spielerwetten vor Spielbeginn: " + ", ".join(f"{sum(1 for _, x in out if x['status'] == k)} {k}"
                                                     for k in ("bestätigt", "schwächer", "zurückziehen")) +
        f", {len(out)} geprüft")
    return out


def settle(pstats, now=None):
    """Nach dem Spiel: echten Wert aus dem Spielbericht holen. Nicht gespielt = Wette zurück."""
    now = now or now_utc()
    T = load_json("props_picks.json", {})
    n = 0
    for pid, t in T.items():
        if t.get("result") or parse_time(t["commence"]) > now:
            continue
        P = pstats["players"].get(f"{t['team']}|{t['key']}")
        d_et = (parse_time(t["commence"]).date())
        game = None
        for gm in (P or {}).get("games", []):
            if abs((date.fromisoformat(gm[0]) - d_et).days) <= 1:
                game = gm
        if game is None:
            if (now - parse_time(t["commence"])).days >= 2:
                t["result"], t["value"] = "push", 0       # nicht eingesetzt → Einsatz zurück
            continue
        v = game[IDX[t["stat"]]]
        t["value"] = v
        if v == t["line"]:
            t["result"] = "push"
        else:
            t["result"] = "win" if (v > t["line"]) == (t["side"] == "over") else "loss"
        n += 1
    save_json("props_picks.json", T)
    if n:
        log(f"Spielerwetten ausgewertet: {n}")
    return T


def record_close(prop_games, now=None):
    """Schlussquote für offene ✓-Spielerwetten: gleiche Linie, gleicher Buchmacher → CLV."""
    now = now or now_utc()
    T = load_json("props_picks.json", {})
    by = {}
    for g in prop_games:
        for p in g["props"]:
            by[_pid(g["id"], p)] = p
    n = 0
    for pid, t in T.items():
        if t.get("result") or parse_time(t["commence"]) <= now:
            continue
        p = by.get(pid)
        if p:
            t["close"] = {"odds": p["odds"], "pMkt": p["pMkt"], "clv": round(t["odds"] * p["pMkt"] - 1, 4),
                          "book": p["bookName"], "at": now.isoformat(timespec="minutes")}
            n += 1
    save_json("props_picks.json", T)
    log(f"Spielerwetten Schlussquoten: {n}")


def stats(T=None):
    T = T if T is not None else load_json("props_picks.json", {})
    done = [t for t in T.values() if t.get("result") in ("win", "loss", "push")]
    pr = lambda t: 10 * (t["odds"] - 1) if t["result"] == "win" else -10 if t["result"] == "loss" else 0
    clv = [t["close"]["clv"] for t in T.values() if t.get("close")]
    profit = sum(pr(t) for t in done)
    hit = [t for t in done if t["result"] != "push"]
    by = {}
    for key, rows in (("mittags", [t for t in done if t.get("src", "mittags") == "mittags"]),
                      ("vorSpiel", [t for t in done if t.get("src") == "vor Spielbeginn"]),
                      ("bestaetigt", [t for t in done if (t.get("pre") or {}).get("status") == "bestätigt"]),
                      ("zurueck", [t for t in done if (t.get("pre") or {}).get("status") in ("zurückziehen", "schwächer")]),
                      ("sicher", [t for t in done if t.get("src", "mittags") == "mittags" and t.get("safe") is True]),
                      ("unsicher", [t for t in done if t.get("src", "mittags") == "mittags" and t.get("safe") is False])):
        h = [t for t in rows if t["result"] != "push"]
        by[key] = {"n": len(h), "profit": round(sum(pr(t) for t in rows), 2),
                   "roi": round(sum(pr(t) for t in rows) / (10 * len(h)), 4) if h else None}
    return {"by": by, "clvPos": round(sum(1 for c in clv if c > 0) / len(clv), 3) if clv else None,"bets": len(T), "settled": len(done), "profit": round(profit, 2),
            "roi": round(profit / (10 * len(hit)), 4) if hit else None,
            "hit": round(sum(1 for t in hit if t["result"] == "win") / len(hit), 3) if hit else None,
            "expHit": round(sum(t["p"] for t in hit) / len(hit), 3) if hit else None,
            "clv": round(sum(clv) / len(clv), 4) if clv else None, "clvN": len(clv)}


# ---------------- Vertrauen je Abweichung aus allen simulierten Linien ----------------

def save_lines(prop_games, now=None):
    """Mittags: alle Linien mit echtem Marktmaßstab merken (Simulation und Markt, je P(über))."""
    now = now or now_utc()
    D = load_json("props_lines.json", {"open": []})
    have = {(r[0], r[2], r[3], r[4], r[5]) for r in D["open"]}
    for g in prop_games:
        if parse_time(g["commence"]) <= now:
            continue
        for row in g.get("_lines") or []:
            r = [g["commence"]] + row
            if (r[0], r[2], r[3], r[4], r[5]) not in have:
                D["open"].append(r)
    save_json("props_lines.json", D)


def learn_conf(pstats, now=None):
    """Abgerechnete Linien in Summen je Abweichungsklasse verdichten (Log-Score je Gewicht), dann Gewicht wählen."""
    now = now or now_utc()
    D = load_json("props_lines.json", {"open": []})
    Pp = load_json("props_params.json", {"corr": {}, "history": []})
    agg = Pp.get("confAgg") or {f"{lo:.2f}": {"n": 0, "ll": [0.0] * len(W_GRID), "y": 0, "ps": 0.0, "pm": 0.0}
                                for lo, _ in GAP_BUCKETS}
    keep, used = [], 0
    for r in D["open"]:
        commence, team, key, stat, line, ps, pm = r
        if (now - parse_time(commence)).total_seconds() < 6 * 3600:
            keep.append(r)
            continue
        P = pstats["players"].get(f"{team}|{key}")
        d = parse_time(commence).date()
        hit = [g for g in (P or {}).get("games", []) if abs((date.fromisoformat(g[0]) - d).days) <= 1]
        if not hit:
            if (now - parse_time(commence)).days < 2:
                keep.append(r)
            continue                                       # nicht gespielt → zählt nicht
        v = hit[-1][IDX[stat]]
        if v == line:
            continue
        y = 1 if v > line else 0
        gap = abs(ps - pm)
        lo = next(lo for lo, hi in GAP_BUCKETS if lo <= gap < hi)
        a = agg[f"{lo:.2f}"]
        a["n"] += 1
        a["y"] += y
        a["ps"] += ps
        a["pm"] += pm
        for i, w in enumerate(W_GRID):
            p = min(0.99, max(0.01, w * ps + (1 - w) * pm))
            a["ll"][i] += math.log(p if y else 1 - p)
        used += 1
    D["open"] = keep
    save_json("props_lines.json", D)
    table = {}
    for lo, hi in GAP_BUCKETS:
        a = agg[f"{lo:.2f}"]
        if not a["n"]:
            table[f"{lo:.2f}"] = {"w": CONF_P, "n": 0, "hi": hi}
            continue
        w_fit = W_GRID[max(range(len(W_GRID)), key=lambda i: a["ll"][i])]
        w = (a["n"] * w_fit + CONF_PRIOR_N * CONF_P) / (a["n"] + CONF_PRIOR_N) if a["n"] >= CONF_MIN_N else CONF_P
        table[f"{lo:.2f}"] = {"w": round(min(CONF_MAX, w), 3), "wFit": w_fit, "n": a["n"], "hi": hi,
                              "hit": round(a["y"] / a["n"], 3), "sim": round(a["ps"] / a["n"], 3), "mkt": round(a["pm"] / a["n"], 3)}
    Pp["confAgg"], Pp["confGap"] = agg, table
    save_json("props_params.json", Pp)
    log(f"Vertrauen in die Simulation: {used} neue Linien ausgewertet · " +
        ", ".join(f"Abweichung ab {float(k) * 100:.0f} %: {v['w']:.2f} (n={v['n']})" for k, v in table.items()))
    return table


# ---------------- Tägliches Lernen aus allen Spielern ----------------
CORR_MIN_N, CORR_STEP, CORR_BOUNDS = 150, 0.25, (0.85, 1.15)


def save_sims(prop_games, now=None):
    """Vorhersage jedes Spielers vor Spielbeginn speichern (Mittagslauf)."""
    now = now or now_utc()
    D = load_json("props_sims.json", {})
    for g in prop_games:
        if parse_time(g["commence"]) <= now:
            continue
        for k, v in (g.get("simSummary") or {}).items():
            D[f"{g['id']}|{k}"] = {"commence": g["commence"], "sim": v, "actual": None}
    # nur die letzten 30 Tage behalten
    cut = (now.date().toordinal() - 30)
    D = {k: v for k, v in D.items() if parse_time(v["commence"]).date().toordinal() >= cut}
    save_json("props_sims.json", D)


def learn(pstats, now=None):
    """Echte Werte eintragen, dann Korrekturfaktor je Wert langsam Richtung (Summe echt / Summe Simulation)."""
    now = now or now_utc()
    D = load_json("props_sims.json", {})
    for key, v in D.items():
        if v.get("actual") is not None or (now - parse_time(v["commence"])).total_seconds() < 6 * 3600:
            continue
        _, team, pk = key.split("|", 2)
        P = pstats["players"].get(f"{team}|{pk}")
        d = parse_time(v["commence"]).date()
        hit = [g for g in (P or {}).get("games", []) if abs((date.fromisoformat(g[0]) - d).days) <= 1]
        if hit:
            g = hit[-1]
            v["actual"] = {s: g[IDX[s]] for s in STATS}
        elif (now - parse_time(v["commence"])).days >= 2:
            v["actual"] = {}                                   # nicht gespielt
    save_json("props_sims.json", D)
    done = [v for v in D.values() if v.get("actual")]
    Pp = load_json("props_params.json", {"corr": {}, "history": []})
    rep = {"n": len(done)}
    if done:
        for s in STATS:
            sim_sum = sum(v["sim"][s][0] for v in done)
            act_sum = sum(v["actual"][s] for v in done)
            cov = sum(1 for v in done if v["sim"][s][1] <= v["actual"][s] <= v["sim"][s][2]) / len(done)
            rep[s] = {"ratio": round(act_sum / sim_sum, 4) if sim_sum else None, "cover50": round(cov, 3),
                      "bias": round((sim_sum - act_sum) / len(done), 2)}
    if len(done) >= CORR_MIN_N:
        changes = []
        for s in STATS:
            old = float(Pp["corr"].get(s, 1.0))
            target = old * (rep[s]["ratio"] or 1.0)
            new = old + CORR_STEP * (min(CORR_BOUNDS[1], max(CORR_BOUNDS[0], target)) - old)
            new = round(min(CORR_BOUNDS[1], max(CORR_BOUNDS[0], new)), 4)
            if abs(new - old) > 1e-4:
                changes.append({"stat": s, "old": old, "new": new})
            Pp["corr"][s] = new
        if changes:
            Pp["history"] = (Pp.get("history", []) + [{"date": now.date().isoformat(), "changes": changes}])[-60:]
    Pp["check"] = rep
    save_json("props_params.json", Pp)
    log(f"Spielerwetten lernen: {len(done)} Spieler-Einsätze geprüft" +
        (f", Korrektur {Pp['corr']}" if len(done) >= CORR_MIN_N else f" (Anpassung ab {CORR_MIN_N})"))
    return Pp
