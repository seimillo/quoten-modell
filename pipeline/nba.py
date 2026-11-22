"""NBA-Modell: Team-Ratings und Spieler-Aufstellung, Tempo, Heimvorteil, Ruhe/Spielplan/Reise, Form, Verletzungen.
Markt-Anker mit Pinnacle und Prognosemärkten, beste Quote aller Buchmacher, strenge Wettauswahl mit Kelly-Einsatz."""
import math
from datetime import date, timedelta

from .common import devig_power, log, parse_time, phi
from .markets import match_kalshi, match_polymarket, whales
from .sources import BOOK_NAMES, SHARP, SOFT, book_prices, main_book, player_key

TEAMS = {  # Name bei The Odds API / Basketball-Reference: (Kürzel, Zeitzone relativ zu New York, Spitzname)
    "Atlanta Hawks": ("ATL", 0, "hawks"), "Boston Celtics": ("BOS", 0, "celtics"), "Brooklyn Nets": ("BKN", 0, "nets"),
    "Charlotte Hornets": ("CHA", 0, "hornets"), "Chicago Bulls": ("CHI", -1, "bulls"), "Cleveland Cavaliers": ("CLE", 0, "cavaliers"),
    "Dallas Mavericks": ("DAL", -1, "mavericks"), "Denver Nuggets": ("DEN", -2, "nuggets"), "Detroit Pistons": ("DET", 0, "pistons"),
    "Golden State Warriors": ("GSW", -3, "warriors"), "Houston Rockets": ("HOU", -1, "rockets"), "Indiana Pacers": ("IND", 0, "pacers"),
    "Los Angeles Clippers": ("LAC", -3, "clippers"), "Los Angeles Lakers": ("LAL", -3, "lakers"), "Memphis Grizzlies": ("MEM", -1, "grizzlies"),
    "Miami Heat": ("MIA", 0, "heat"), "Milwaukee Bucks": ("MIL", -1, "bucks"), "Minnesota Timberwolves": ("MIN", -1, "timberwolves"),
    "New Orleans Pelicans": ("NOP", -1, "pelicans"), "New York Knicks": ("NYK", 0, "knicks"), "Oklahoma City Thunder": ("OKC", -1, "thunder"),
    "Orlando Magic": ("ORL", 0, "magic"), "Philadelphia 76ers": ("PHI", 0, "76ers"), "Phoenix Suns": ("PHX", -2, "suns"),
    "Portland Trail Blazers": ("POR", -3, "trail blazers"), "Sacramento Kings": ("SAC", -3, "kings"), "San Antonio Spurs": ("SAS", -1, "spurs"),
    "Toronto Raptors": ("TOR", 0, "raptors"), "Utah Jazz": ("UTA", -2, "jazz"), "Washington Wizards": ("WAS", 0, "wizards"),
}
ALIASES = {"LA Clippers": "Los Angeles Clippers", "LA Lakers": "Los Angeles Lakers"}
HCA, ALT_BONUS, SD_MARGIN, SD_TOTAL, CONF = 2.2, 0.8, 12.5, 18.0, 0.3


def canon(name):
    return ALIASES.get(name, name)


def _et_date(iso):
    return (parse_time(iso) - timedelta(hours=5)).date()


def last_games(scores):
    last = {}
    for g in scores:
        if not g.get("completed"):
            continue
        d = _et_date(g["commence_time"])
        for t in (canon(g["home_team"]), canon(g["away_team"])):
            if t not in last or d > last[t]:
                last[t] = d
    return last


def rating(team, cur, prior, pace, lg):
    c = cur.get(team)
    p = prior.get(team)
    if not c:
        return None
    w = c["g"] / (c["g"] + 20)
    off_c, def_c = c["ortg_a"] - lg["cur"], lg["cur"] - c["drtg_a"]
    off_p = 0.7 * (p["ortg_a"] - lg["prior"]) if p else 0.0
    def_p = 0.7 * (lg["prior"] - p["drtg_a"]) if p else 0.0
    return {"off": w * off_c + (1 - w) * off_p, "def": w * def_c + (1 - w) * def_p,
            "pace": pace.get(team, lg["pace"]), "w": w, "netCur": off_c + def_c,
            "netPrior": (off_p + def_p) / 0.7 if p else None}


WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
LONG_TERM = ("surgery", "re-evaluated", "weeks", "month", "season", "torn", "tear", "fracture", "rupture",
             "stepping away", "indefinitely", "between", "return sometime")


OFFICIAL_P = {"out": 1.0, "doubtful": 0.75, "questionable": 0.45, "probable": 0.1, "available": 0.0}

ARENA = {  # Breiten- und Längengrad der Hallen (für Reisekilometer)
    "ATL": (33.757, -84.396), "BOS": (42.366, -71.062), "BKN": (40.683, -73.976), "CHA": (35.225, -80.839),
    "CHI": (41.881, -87.674), "CLE": (41.496, -81.688), "DAL": (32.790, -96.810), "DEN": (39.749, -105.008),
    "DET": (42.341, -83.055), "GSW": (37.768, -122.388), "HOU": (29.751, -95.362), "IND": (39.764, -86.156),
    "LAC": (33.945, -118.343), "LAL": (34.043, -118.267), "MEM": (35.138, -90.051), "MIA": (25.781, -80.188),
    "MIL": (43.045, -87.917), "MIN": (44.979, -93.276), "NOP": (29.949, -90.082), "NYK": (40.751, -73.993),
    "OKC": (35.463, -97.515), "ORL": (28.539, -81.384), "PHI": (39.901, -75.172), "PHX": (33.446, -112.071),
    "POR": (45.532, -122.667), "SAC": (38.580, -121.500), "SAS": (29.427, -98.438), "TOR": (43.643, -79.379),
    "UTA": (40.768, -111.901), "WAS": (38.898, -77.021)}


def km(a, b):
    (la1, lo1), (la2, lo2) = ARENA[a], ARENA[b]
    p1, p2 = math.radians(la1), math.radians(la2)
    d = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lo2 - lo1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(d))


def out_prob(status, info, game_day, src=None):
    """Wahrscheinlichkeit, dass der Spieler fehlt – offizieller NBA-Status oder ESPN-Status mit Kurztext."""
    st, txt = status.lower(), info.lower()
    if src == "NBA":
        return OFFICIAL_P.get(st, 0.3)
    if st == "active":
        return 0.0
    if "probable" in txt:
        return 0.1
    if "doubtful" in txt:
        return 0.75
    if "questionable" in txt or "game-time" in txt:
        return 0.45
    if st == "out":
        days = [d for d in WEEKDAYS if d in txt]
        if days and WEEKDAYS[game_day] not in days and not any(k in txt for k in LONG_TERM):
            return 0.4                                   # Meldung betraf ein früheres Spiel (z. B. Pause am Vortag)
        return 1.0
    return 0.3                                           # Day-To-Day ohne Zusatz


def player_value(name, abbr, cur_pl, prior_pl):
    """Punkte pro Spiel, die das Team ohne den Spieler verliert (BPM gegenüber Ersatzniveau −2)."""
    k = player_key(name)
    c = cur_pl.get((k, abbr))
    p = next((v for (n, _), v in prior_pl.items() if n == k), None)
    mp_c, mp_p = (c["mp"] if c else 0.0), (p["mp"] if p else 0.0)
    if mp_c + mp_p == 0:
        return None
    bpm = (mp_c * (c["bpm"] if c else 0) + 0.5 * mp_p * (p["bpm"] if p else 0) - 300) / (mp_c + 0.5 * mp_p + 300)
    if c and c["g"] >= 3:
        mpg = c["mp"] / c["g"]
    elif p and p["g"]:
        mpg = p["mp"] / p["g"]
    else:
        mpg = 0.0
    return {"value": max(0.0, min(8.0, (bpm + 2) * mpg / 48)), "bpm": round(bpm, 1), "mpg": round(mpg, 1),
            "gCur": c["g"] if c else 0, "onPrior": bool(prior_pl.get((k, abbr)))}


def injury_impact(team, abbr, entries, cur_team, w, cur_pl, prior_pl, game_day):
    """Wertverlust durch Ausfälle. Was schon in den Team-Ratings fehlt, wird nicht doppelt abgezogen."""
    total, rows = 0.0, []
    team_g = max(1.0, cur_team["g"]) if cur_team else 1.0
    for e in entries:
        po = out_prob(e.get("status", ""), e.get("info", ""), game_day, e.get("src"))
        pv = player_value(e.get("player", ""), abbr, cur_pl, prior_pl)
        row = {**e, "pOut": po}
        if pv and po > 0:
            in_ratings = w * min(1.0, pv["gCur"] / team_g) + (1 - w) * 0.7 * (1.0 if pv["onPrior"] else 0.0)
            loss = pv["value"] * in_ratings * po
            total += loss
            row.update({"value": round(pv["value"], 1), "bpm": pv["bpm"], "mpg": pv["mpg"], "loss": round(loss, 2)})
        rows.append(row)
    return total, rows


def _two(outcomes, a, b, key="name"):
    oa = next((o for o in outcomes if canon(o.get(key, "")) == a), None)
    ob = next((o for o in outcomes if canon(o.get(key, "")) == b), None)
    return oa, ob


# ---------------- Spielerebene: heutige Aufstellung ----------------

def _shrunk_bpm(c, p):
    mp_c, mp_p = (c["mp"] if c else 0.0), (p["mp"] if p else 0.0)
    return (mp_c * (c["bpm"] if c else 0) + 0.5 * mp_p * (p["bpm"] if p else 0) - 300) / (mp_c + 0.5 * mp_p + 300)


def rosters(cur_pl):
    """Aktueller Kader je Team: bei Trades zählt die letzte Zeile bei Basketball-Reference."""
    last = {}
    for (k, abbr), v in cur_pl.items():
        if k not in last or v.get("order", 0) > last[k][1].get("order", 0):
            last[k] = (abbr, v)
    out = {}
    for k, (abbr, v) in last.items():
        out.setdefault(abbr, []).append((k, v))
    return out


def lineup_net(abbr, roster, prior_by_name, avail):
    """Netto-Rating pro 100 Ballbesitze aus den heute verfügbaren Spielern: Σ BPM × Minuten/48 (240 Minuten)."""
    players = []
    for k, c in roster.get(abbr, []):
        p = prior_by_name.get(k)
        mpg = c["mp"] / c["g"] if c["g"] >= 3 else (min(p["mp"] / p["g"], 24.0) if p and p["g"] else c["mp"] / max(1, c["g"]))
        players.append((k, _shrunk_bpm(c, p), mpg * (1 - avail.get(k, 0.0))))
    if not players:
        return None, 0.0
    tot = sum(m for _, _, m in players)
    scale = 240 / tot if tot > 240 else 1.0
    net = sum(b * m * scale for _, b, m in players) / 48
    if tot < 240:
        net += -2.0 * (240 - tot) / 48                  # fehlende Minuten mit Ersatzniveau auffüllen
    return net, tot


# ---------------- Spielplan: Ruhe, Dichte, Reise, Form ----------------

def team_history(games, before):
    """{Team: [(Datum, Gastgeber, eigene Punkte, Gegner-Punkte, Gegner, heim?)]} für beendete Spiele vor `before`."""
    out = {}
    for g in games:
        d = date.fromisoformat(g["date"])
        if not g.get("done") or d >= before:
            continue
        h, a = canon(g["home"]), canon(g["away"])
        out.setdefault(h, []).append((d, h, g["hs"], g["as"], a, True))
        out.setdefault(a, []).append((d, h, g["as"], g["hs"], h, False))
    for v in out.values():
        v.sort()
    return out


def schedule(team, gd, hist, host):
    """Ruhetage, 3-in-4 / 4-in-6, Reisekilometer seit dem letzten Spiel."""
    h = hist.get(team, [])
    if not h:
        return {"rest": 2, "dens": 0, "km": 0.0, "n": 0}
    last_d, last_host = h[-1][0], h[-1][1]
    rest = (gd - last_d).days - 1
    in4 = sum(1 for x in h if 1 <= (gd - x[0]).days <= 3)
    in6 = sum(1 for x in h if 1 <= (gd - x[0]).days <= 5)
    dens = (1 if in4 >= 2 else 0) + (1 if in6 >= 3 else 0)
    dist = km(TEAMS[last_host][0], TEAMS[host][0]) if last_host in TEAMS and host in TEAMS else 0.0
    weight = 1.0 if rest == 0 else 0.5 if rest == 1 else 0.0
    return {"rest": rest, "dens": dens, "km": dist * weight, "n": len(h)}


def form(team, hist, expect):
    """Ø Abweichung der letzten 10 Spiele vom erwarteten Ergebnis, zur Null gezogen."""
    res = []
    for d, host, pf, pa, opp, home in hist.get(team, [])[-10:]:
        e = expect(team, opp, home)
        if e is not None:
            res.append((pf - pa) - e)
    return sum(res) / (len(res) + 10) if res else 0.0


# ---------------- Wetten: beste Quote, Pinnacle-Check, Kelly ----------------
MIN_EV, MIN_EV_NO_SHARP, KELLY_FRAC, KELLY_CAP = 0.03, 0.05, 0.25, 0.02


def judge(b):
    """Strenge Auswahl: EV ≥ 3 % und die Quote ist auch gegen Pinnacle fair (sonst EV ≥ 5 %). Einsatz: ¼-Kelly, max. 2 %."""
    if b.get("evPin") is not None:
        ok = b["ev"] >= MIN_EV and b["evPin"] >= 0
    else:
        ok = b["ev"] >= MIN_EV_NO_SHARP
    b["value"] = bool(ok)
    k = b["ev"] / (b["odds"] - 1) if b["odds"] > 1 else 0
    b["stake"] = round(min(KELLY_CAP, KELLY_FRAC * k), 4) if ok else 0.0
    return b


def soft_offers(prices, name, point=None):
    """Alle Angebote deutscher/europäischer Buchmacher für eine Seite: [(Buch, Quote, Linie)]."""
    out = []
    for bk in SOFT:
        o = next((x for x in prices.get(bk) or [] if canon(x["name"]) == name), None)
        if o and o.get("price"):
            out.append((bk, o["price"], o.get("point")))
    return out


def sharp_pair(prices, n1, n2):
    for bk in SHARP:
        a, b = _two(prices.get(bk) or [], n1, n2)
        if a and b:
            return devig_power(a["price"], b["price"]), a, b
    return None, None, None


def model_games(events, cur, prior, pace, scores, injuries, kalshi, pm_events, cur_pl=None, prior_pl=None, P=None,
                history=None):
    cur_pl, prior_pl = cur_pl or {}, prior_pl or {}
    P = {**{"team_scale": 0.5, "player_scale": 0.5, "hca": HCA, "rest_scale": 1.0, "inj_scale": 0.5, "sched_scale": 1.0,
            "travel_scale": 0.3, "form_scale": 0.15, "sd_margin": SD_MARGIN, "sd_total": SD_TOTAL, "total_bias": 0.0,
            "conf": CONF}, **(P or {})}
    SDM, SDT, CF = P["sd_margin"], P["sd_total"], P["conf"]
    lg = {"cur": sum(v["ortg_a"] for v in cur.values()) / max(1, len(cur)),
          "prior": sum(v["ortg_a"] for v in prior.values()) / max(1, len(prior)) if prior else 115.0,
          "pace": sum(pace.values()) / max(1, len(pace)) if pace else 99.0}
    last = last_games(scores)
    ros = rosters(cur_pl)
    prior_by_name = {}
    for (k, _), v in prior_pl.items():
        if k not in prior_by_name or v["mp"] > prior_by_name[k]["mp"]:
            prior_by_name[k] = v
    hist_all = history or []

    def expect(team, opp, home):
        T, O = rating(team, cur, prior, pace, lg), rating(opp, cur, prior, pace, lg)
        if not T or not O:
            return None
        poss = T["pace"] * O["pace"] / lg["pace"]
        return poss / 100 * ((T["off"] + T["def"]) - (O["off"] + O["def"])) + (P["hca"] if home else -P["hca"])

    games = []
    for ev in events:
        home, away = canon(ev["home_team"]), canon(ev["away_team"])
        H, A = rating(home, cur, prior, pace, lg), rating(away, cur, prior, pace, lg)
        if not H or not A or home not in TEAMS or away not in TEAMS:
            log(f"NBA: keine Ratings für {away} @ {home} – übersprungen")
            continue
        gd = _et_date(ev["commence_time"])
        hist = team_history(hist_all, gd)
        parts = []
        poss = A["pace"] * H["pace"] / lg["pace"]
        raw_a = poss / 100 * (lg["cur"] + A["off"] - H["def"])
        raw_h = poss / 100 * (lg["cur"] + H["off"] - A["def"])
        team_raw, tot_raw = raw_h - raw_a, raw_a + raw_h

        # Verletzungen (gegen Team-Ratings) und Aufstellung (Spielerebene)
        inj_a = injuries.get(away, []) or injuries.get(TEAMS[away][2].title(), [])
        inj_h = injuries.get(home, []) or injuries.get(TEAMS[home][2].title(), [])
        loss_a, rows_a = injury_impact(away, TEAMS[away][0], inj_a, cur.get(away), A["w"], cur_pl, prior_pl, gd.weekday())
        loss_h, rows_h = injury_impact(home, TEAMS[home][0], inj_h, cur.get(home), H["w"], cur_pl, prior_pl, gd.weekday())
        inj_raw = loss_a - loss_h
        av_a = {player_key(r["player"]): r["pOut"] for r in rows_a}
        av_h = {player_key(r["player"]): r["pOut"] for r in rows_h}
        net_a, min_a = lineup_net(TEAMS[away][0], ros, prior_by_name, av_a)
        net_h, min_h = lineup_net(TEAMS[home][0], ros, prior_by_name, av_h)
        if net_a is not None and net_h is not None:
            player_raw = poss / 100 * (net_h - net_a)
            ts, ps = P["team_scale"], P["player_scale"]
        else:                                           # ohne Spielerdaten: nur Team-Ratings, voll gewichtet
            player_raw, ts, ps = 0.0, P["team_scale"] + P["player_scale"], 0.0
        is_ = P["inj_scale"] if ps else P["inj_scale"] + P["player_scale"]
        parts.append({"n": "Team-Stärke & Tempo", "m": ts * team_raw, "info": f"Gewicht {ts:.2f}"})
        if ps:
            parts.append({"n": "Heutige Aufstellung (Spieler)", "m": ps * player_raw,
                          "info": f"Netto {TEAMS[away][0]} {net_a:+.1f}, {TEAMS[home][0]} {net_h:+.1f} pro 100 · Gewicht {ps:.2f}"})
        if loss_a or loss_h:
            parts.append({"n": "Verletzungen (gegen Team-Ratings)", "m": is_ * inj_raw,
                          "info": f"{TEAMS[away][0]} −{loss_a:.1f}, {TEAMS[home][0]} −{loss_h:.1f} Punkte · Gewicht {is_:.2f}"})
        alt = ALT_BONUS if TEAMS[home][0] in ("DEN", "UTA") else 0.0
        hca = P["hca"] + alt
        parts.append({"n": "Heimvorteil" + (" inkl. Höhe" if alt else ""), "m": hca})

        # Ruhe, Spielplan, Reise, Form
        sa_, sh_ = schedule(away, gd, hist, home), schedule(home, gd, hist, home)
        rest_a = sa_["rest"] if sa_["n"] else ((gd - last[away]).days - 1 if away in last else 2)
        rest_h = sh_["rest"] if sh_["n"] else ((gd - last[home]).days - 1 if home in last else 2)
        rest = 0.0
        if rest_a == 0:
            rest += 2.0
        if rest_h == 0:
            rest -= 1.5
        if rest_a > 0 and rest_h > 0:
            rest += 0.3 * max(-2, min(2, rest_h - rest_a))
        tz = TEAMS[home][1] - TEAMS[away][1]
        if tz > 0 and rest_a <= 1:
            rest += 0.3 * tz
        rest_raw = rest
        sched_raw = sa_["dens"] - sh_["dens"]
        travel_raw = (sa_["km"] - sh_["km"]) / 1000
        form_raw = form(home, hist, expect) - form(away, hist, expect)
        r_m = P["rest_scale"] * rest_raw
        if r_m:
            parts.append({"n": "Ruhe & Zeitzone", "m": r_m, "info": f"Ruhetage {TEAMS[away][0]} {rest_a}, {TEAMS[home][0]} {rest_h}"})
        if sched_raw:
            parts.append({"n": "Spielplan-Dichte", "m": P["sched_scale"] * sched_raw,
                          "info": f"3-in-4/4-in-6: {TEAMS[away][0]} {sa_['dens']}, {TEAMS[home][0]} {sh_['dens']}"})
        if abs(travel_raw) > 0.05:
            parts.append({"n": "Reise", "m": P["travel_scale"] * travel_raw,
                          "info": f"{TEAMS[away][0]} {sa_['km']:.0f} km, {TEAMS[home][0]} {sh_['km']:.0f} km (gewichtet)"})
        if abs(form_raw) > 0.05:
            parts.append({"n": "Form (letzte 10)", "m": P["form_scale"] * form_raw,
                          "info": "Abweichung vom erwarteten Ergebnis, zur Null gezogen"})
        M = (ts * team_raw + ps * player_raw + is_ * inj_raw + hca + r_m + P["sched_scale"] * sched_raw
             + P["travel_scale"] * travel_raw + P["form_scale"] * form_raw)
        TOT = tot_raw + P["total_bias"]
        pts_h, pts_a = (TOT + M) / 2, (TOT - M) / 2

        # ---- Quoten ----
        h2h, spr, tot = book_prices(ev, "h2h"), book_prices(ev, "spreads"), book_prices(ev, "totals")
        bk, main = main_book(h2h)
        if not main:
            log(f"NBA: keine Quoten für {away} @ {home}")
            continue
        oa, oh = _two(main, away, home)
        if not oa or not oh:
            log(f"NBA: Teamnamen passen nicht bei {away} @ {home}")
            continue
        p_model_h = phi(M / SDM)
        mk_h = devig_power(oh["price"], oa["price"])
        pin_h, _, _ = sharp_pair(h2h, home, away)
        if pin_h is not None:
            mk_h = pin_h
        ab_a, ab_h = TEAMS[away][0], TEAMS[home][0]
        kal = match_kalshi(kalshi, ab_a, ab_h, TEAMS[away][2], TEAMS[home][2])
        pm = match_polymarket(pm_events, TEAMS[away][2], TEAMS[home][2])
        pred = [p for p in ([kal["b"] / (kal["a"] + kal["b"])] if kal else []) + ([pm["pB"] / (pm["pA"] + pm["pB"])] if pm else [])]
        if pred:
            mk_h = 0.5 * mk_h + 0.5 * sum(pred) / len(pred)
        whale = whales(pm, away, home) if pm else {"trades": [], "usdA": 0, "usdB": 0}

        mix = lambda p, b: CF * p + (1 - CF) * b
        bets = []

        def add(market, pick, offers, p_model_fn, p_mkt_fn, p_pin_fn, side):
            """Bewertet jedes Angebot (Buch, Quote, Linie) und behält das mit dem höchsten EV."""
            best = None
            for book, q, line in offers:
                pm_, pk_ = p_model_fn(line), p_mkt_fn(line)
                if pm_ is None or pk_ is None:
                    continue
                p = mix(pm_, pk_)
                ev_ = p * q - 1
                if best is None or ev_ > best["ev"]:
                    pp = p_pin_fn(line)
                    best = {"market": market, "pick": pick(line), "odds": q, "line": line, "side": side, "book": book,
                            "bookName": BOOK_NAMES.get(book, book), "pModel": round(pm_, 4), "pMarket": round(pk_, 4),
                            "p": round(p, 4), "ev": round(ev_, 4), "evRaw": round(pm_ * q - 1, 4),
                            "evPin": round(pp * q - 1, 4) if pp is not None else None,
                            "pPin": round(pp, 4) if pp is not None else None}
            if best:
                main_q = next((q for b_, q, _ in offers if b_ == bk), None)
                best["mainOdds"], best["mainBook"] = main_q, BOOK_NAMES.get(bk, bk)
                best["nBooks"] = len(offers)
                bets.append(judge(best))

        # Sieg
        add("Sieg", lambda l: away, soft_offers(h2h, away), lambda l: 1 - p_model_h, lambda l: 1 - mk_h,
            lambda l: None if pin_h is None else 1 - pin_h, "away")
        add("Sieg", lambda l: home, soft_offers(h2h, home), lambda l: p_model_h, lambda l: mk_h,
            lambda l: pin_h, "home")

        # Handicap: Marktwahrscheinlichkeit an jeder Linie über die Normalverteilung umgerechnet
        ref_bk = next((b_ for b_ in SHARP + [bk] + SOFT if _two(spr.get(b_) or [], away, home)[1]), None)
        if ref_bk:
            ra, rh = _two(spr[ref_bk], away, home)
            if ra and rh and rh.get("point") is not None:
                z_h = _z(devig_power(rh["price"], ra["price"]))
                lh0 = rh["point"]
                cov_h_mkt = lambda lh: phi(z_h + (lh - lh0) / SDM)
                pin_ok = ref_bk in SHARP
                add("Handicap", lambda l: f"{away} {l:+g}", soft_offers(spr, away), lambda l: 1 - phi((M - l) / SDM),
                    lambda l: 1 - cov_h_mkt(-l), (lambda l: 1 - cov_h_mkt(-l)) if pin_ok else (lambda l: None), "away")
                add("Handicap", lambda l: f"{home} {l:+g}", soft_offers(spr, home), lambda l: phi((M + l) / SDM),
                    lambda l: cov_h_mkt(l), (lambda l: cov_h_mkt(l)) if pin_ok else (lambda l: None), "home")

        # Gesamtpunkte
        ref_bk = next((b_ for b_ in SHARP + [bk] + SOFT if _two(tot.get(b_) or [], "Over", "Under")[0]), None)
        if ref_bk:
            ro, ru = _two(tot[ref_bk], "Over", "Under")
            if ro and ru and ro.get("point") is not None:
                z_o, t0 = _z(devig_power(ro["price"], ru["price"])), ro["point"]
                over_mkt = lambda t: phi(z_o - (t - t0) / SDT)
                pin_ok = ref_bk in SHARP
                add("Gesamtpunkte", lambda l: f"Über {l:g}", soft_offers(tot, "Over"), lambda l: 1 - phi((l - TOT) / SDT),
                    over_mkt, over_mkt if pin_ok else (lambda l: None), "over")
                add("Gesamtpunkte", lambda l: f"Unter {l:g}", soft_offers(tot, "Under"), lambda l: phi((l - TOT) / SDT),
                    lambda l: 1 - over_mkt(l), (lambda l: 1 - over_mkt(l)) if pin_ok else (lambda l: None), "under")

        if not bets:
            continue
        values = [b for b in bets if b["value"]]
        best = max(values or bets, key=lambda b: b["ev"])
        games.append({
            "id": ev["id"], "sportKey": "basketball_nba", "sport": "NBA", "commence": ev["commence_time"], "away": away, "home": home,
            "abbrA": ab_a, "abbrH": ab_h, "margin": round(M, 1), "total": round(TOT, 1), "poss": round(poss, 1),
            "ptsA": round(pts_a, 1), "ptsH": round(pts_h, 1), "pModelHome": round(p_model_h, 4),
            "pMarketHome": round(mk_h, 4), "pFinalHome": round(mix(p_model_h, mk_h), 4),
            "parts": [{**x, "m": round(x["m"], 1)} for x in parts],
            "ratings": {"away": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in A.items()},
                        "home": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in H.items()}},
            "lineup": {"away": {"net": None if net_a is None else round(net_a, 1), "min": round(min_a)},
                       "home": {"net": None if net_h is None else round(net_h, 1), "min": round(min_h)}},
            "schedule": {"away": sa_, "home": sh_},
            "kalshi": kal, "polymarket": ({"pA": pm["pA"], "pB": pm["pB"], "volume": round(pm["volume"])} if pm else None),
            "bookKey": bk, "bookName": BOOK_NAMES.get(bk, bk), "whales": whale, "injuries": {"away": rows_a, "home": rows_h},
            "bets": bets, "best": best, "hasValue": bool(values),
            "sideA": home, "pickLabel": f"{away} @ {home}", "conf": CF,
            "feat": {"team": round(team_raw, 3), "player": round(player_raw, 3), "alt": alt, "rest": round(rest_raw, 3),
                     "inj": round(inj_raw, 3), "sched": sched_raw, "travel": round(travel_raw, 3), "form": round(form_raw, 3),
                     "TOTraw": round(tot_raw, 2), "pModelRaw": round(p_model_h, 4), "pMarket": round(mk_h, 4)},
        })
    games.sort(key=lambda g: (-g["hasValue"], -g["best"]["ev"]))
    log(f"NBA: {len(games)} Spiele bewertet, {sum(g['hasValue'] for g in games)} mit Value-Wette")
    return games


def _z(p):
    from statistics import NormalDist
    return NormalDist().inv_cdf(min(0.999, max(0.001, p)))
