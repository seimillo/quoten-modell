"""Lernende Stellschrauben: werden täglich vorsichtig an die ausgewerteten Spiele angepasst.

Verfahren:
- NBA-Punktedifferenz: Kleinste-Quadrate-Regression des echten Ergebnisses auf die Modellteile
  (Teamstärke, Heimvorteil, Ruhe, Verletzungen) → Skalierungsfaktoren und Heimvorteil.
- Streuung (SD) und Gesamtpunkte-Verschiebung aus den echten Abweichungen.
- Modellgewicht im Markt-Anker: der Wert mit dem kleinsten Log-Loss (Raster 0–80 %).
Jeder Wert bewegt sich pro Tag nur um einen Bruchteil Richtung Schätzung (Schrittweite wächst mit der Datenmenge)
und bleibt in festen Grenzen. Unter MIN_GAMES Spielen ändert sich nichts.
"""
import math

from .common import load_json, log, now_utc, save_json

VERSION = 2          # bei neuen Modellteilen: alte, noch ungelernte Werte verwerfen
DEFAULTS = {
    "nba": {"team_scale": 0.5, "player_scale": 0.5, "hca": 2.2, "rest_scale": 1.0, "inj_scale": 0.5,
            "sched_scale": 1.0, "travel_scale": 0.3, "form_scale": 0.15,
            "sd_margin": 12.5, "sd_total": 18.0, "total_bias": 0.0, "conf": 0.3},
    "tennis": {"conf_scale": 1.0},
}
BOUNDS = {
    "nba": {"team_scale": (0.0, 1.5), "player_scale": (0.0, 1.5), "hca": (0.0, 4.5), "rest_scale": (0.0, 2.5),
            "inj_scale": (0.0, 2.0), "sched_scale": (0.0, 3.0), "travel_scale": (0.0, 1.5), "form_scale": (0.0, 0.6),
            "sd_margin": (10.0, 15.0), "sd_total": (14.0, 22.0), "total_bias": (-6.0, 6.0), "conf": (0.05, 0.8)},
    "tennis": {"conf_scale": (0.2, 2.0)},
}
MIN_GAMES = 40
HALF = 300          # bei 300 Spielen ist die Schrittweite halb so groß wie maximal
MAX_STEP = 0.25


def load_params():
    p = load_json("params.json", {})
    if p.get("version") != VERSION and not any(h for h in p.get("history", [])):
        p = {}
    out = {s: {**DEFAULTS[s], **(p.get(s) or {})} for s in DEFAULTS}
    out["history"] = p.get("history", [])
    out["n"] = p.get("n", {})
    out["version"] = VERSION
    return out


def _step(n):
    return MAX_STEP * n / (n + HALF)


def _move(params, sport, key, target, n):
    lo, hi = BOUNDS[sport][key]
    target = max(lo, min(hi, target))
    old = params[sport][key]
    new = old + _step(n) * (target - old)
    params[sport][key] = round(max(lo, min(hi, new)), 4)
    return old, params[sport][key]


def _lstsq(X, y, prior=None, prior_games=40):
    """Regression mit Ridge-Bremse Richtung der aktuellen Werte (zählt wie `prior_games` zusätzliche Spiele).
    So bleiben stark verwandte Teile (Team-Ratings, Aufstellung, Verletzungen) stabil und seltene Teile unverändert."""
    k, n = len(X[0]), len(X)
    prior = prior or [0.0] * k
    lam = [prior_games * max(1e-6, sum(r[i] ** 2 for r in X) / n) for i in range(k)]
    A = [[sum(r[i] * r[j] for r in X) + (lam[i] if i == j else 0) for j in range(k)] for i in range(k)]
    b = [sum(r[i] * t for r, t in zip(X, y)) + lam[i] * prior[i] for i in range(k)]
    for i in range(k):                       # Gauß-Elimination
        piv = max(range(i, k), key=lambda r: abs(A[r][i]))
        A[i], A[piv], b[i], b[piv] = A[piv], A[i], b[piv], b[i]
        for r in range(i + 1, k):
            f = A[r][i] / A[i][i]
            A[r] = [a - f * c for a, c in zip(A[r], A[i])]
            b[r] -= f * b[i]
    x = [0.0] * k
    for i in reversed(range(k)):
        x[i] = (b[i] - sum(A[i][j] * x[j] for j in range(i + 1, k))) / A[i][i]
    return x


def _logloss(rows, w):
    s = 0.0
    for pm, pk, y in rows:
        p = min(0.999, max(0.001, w * pm + (1 - w) * pk))
        s -= math.log(p if y else 1 - p)
    return s / len(rows)


def _best_weight(rows):
    grid = [i / 100 for i in range(0, 81, 2)]
    return min(grid, key=lambda w: _logloss(rows, w))


def calibrate(picks, params):
    changes = []
    nba = [r for r in picks.values() if r.get("sport") == "NBA" and r.get("result") and r.get("feat") and r.get("score")]
    if len(nba) >= MIN_GAMES:
        X, y, tot_res, rows = [], [], [], []
        for r in nba:
            f = r["feat"]
            h, a = (float(v) for v in r["score"].split(":"))
            X.append([f["team"], 1.0, f["rest"], f["inj"], f.get("player", 0.0), f.get("sched", 0.0),
                      f.get("travel", 0.0), f.get("form", 0.0)])     # Rohwerte, noch ohne gelernte Faktoren
            y.append(h - a - f.get("alt", 0.0))
            tot_res.append((h + a) - f["TOTraw"])
            rows.append((f["pModelRaw"], f["pMarket"], 1 if h > a else 0))
        cur = params["nba"]
        keys = ["team_scale", "hca", "rest_scale", "inj_scale", "player_scale", "sched_scale", "travel_scale", "form_scale"]
        coef = _lstsq(X, y, [cur[k] for k in keys])
        pred = [sum(cur[k] * xi for k, xi in zip(keys, x)) for x in X]
        sd_m = math.sqrt(sum((t - p) ** 2 for t, p in zip(y, pred)) / len(y))
        bias = sum(tot_res) / len(tot_res)
        sd_t = math.sqrt(sum((t - bias) ** 2 for t in tot_res) / len(tot_res))
        n = len(nba)
        for key, tgt in (*zip(keys, coef), ("sd_margin", sd_m), ("sd_total", sd_t), ("total_bias", bias), ("conf", _best_weight(rows))):
            old, new = _move(params, "nba", key, tgt, n)
            if abs(new - old) > 1e-4:
                changes.append({"sport": "NBA", "key": key, "old": old, "new": new, "target": round(tgt, 3)})
        params["n"]["nba"] = n
    ten = [r for r in picks.values() if r.get("sport") == "Tennis" and r.get("result") and r.get("feat")]
    if len(ten) >= MIN_GAMES:
        rows = [(r["feat"]["pModelRaw"], r["feat"]["pMarket"], 1 if r["result"] == "A" else 0) for r in ten]
        confs = [r["feat"]["confBase"] for r in ten if r["feat"].get("confBase")]
        avg_conf = sum(confs) / len(confs) if confs else 0.4
        tgt = _best_weight(rows) / avg_conf
        old, new = _move(params, "tennis", "conf_scale", tgt, len(ten))
        if abs(new - old) > 1e-4:
            changes.append({"sport": "Tennis", "key": "conf_scale", "old": old, "new": new, "target": round(tgt, 3)})
        params["n"]["tennis"] = len(ten)
    if changes:
        params["history"] = (params["history"] + [{"date": now_utc().date().isoformat(), "changes": changes}])[-120:]
    log(f"Kalibrierung: {len(nba)} NBA-Spiele, {len(ten)} Tennis-Matches ausgewertet, {len(changes)} Werte angepasst"
        + ("" if len(nba) >= MIN_GAMES or len(ten) >= MIN_GAMES else f" (Anpassung ab {MIN_GAMES} Spielen)"))
    return params


def save_params(params):
    save_json("params.json", params)
