"""NBA-Modell: Team-Ratings + Tempo + Heimvorteil + Ruhe/Reise, Markt-Anker mit Pinnacle und Prognosemärkten."""
from datetime import timedelta

from .common import devig_power, log, parse_time, phi
from .markets import match_kalshi, match_polymarket, whales
from .sources import book_prices

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


def _two(outcomes, a, b, key="name"):
    oa = next((o for o in outcomes if canon(o.get(key, "")) == a), None)
    ob = next((o for o in outcomes if canon(o.get(key, "")) == b), None)
    return oa, ob


def model_games(events, cur, prior, pace, scores, injuries, kalshi, pm_events):
    lg = {"cur": sum(v["ortg_a"] for v in cur.values()) / max(1, len(cur)),
          "prior": sum(v["ortg_a"] for v in prior.values()) / max(1, len(prior)) if prior else 115.0,
          "pace": sum(pace.values()) / max(1, len(pace)) if pace else 99.0}
    last = last_games(scores)
    games = []
    for ev in events:
        home, away = canon(ev["home_team"]), canon(ev["away_team"])
        H, A = rating(home, cur, prior, pace, lg), rating(away, cur, prior, pace, lg)
        if not H or not A or home not in TEAMS or away not in TEAMS:
            log(f"NBA: keine Ratings für {away} @ {home} – übersprungen")
            continue
        parts = []
        poss = A["pace"] * H["pace"] / lg["pace"]
        pts_a = poss / 100 * (lg["cur"] + A["off"] - H["def"])
        pts_h = poss / 100 * (lg["cur"] + H["off"] - A["def"])
        parts.append({"n": "Team-Stärke & Tempo", "m": pts_h - pts_a})
        hca = HCA + (ALT_BONUS if TEAMS[home][0] in ("DEN", "UTA") else 0)
        pts_h += hca / 2; pts_a -= hca / 2
        parts.append({"n": "Heimvorteil" + (" inkl. Höhe" if hca > HCA else ""), "m": hca})
        gd = _et_date(ev["commence_time"])
        rest_a = (gd - last[away]).days - 1 if away in last else 2
        rest_h = (gd - last[home]).days - 1 if home in last else 2
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
        if rest:
            pts_h += rest / 2; pts_a -= rest / 2
            parts.append({"n": "Ruhe & Reise", "m": rest, "info": f"Ruhetage {TEAMS[away][0]} {rest_a}, {TEAMS[home][0]} {rest_h}"})
        M, TOT = pts_h - pts_a, pts_a + pts_h

        # ---- Quoten ----
        h2h, spr, tot = book_prices(ev, "h2h"), book_prices(ev, "spreads"), book_prices(ev, "totals")
        b365 = h2h.get("bet365")
        if not b365:
            continue
        oa, oh = _two(b365, away, home)
        if not oa or not oh:
            continue
        p_model_h = phi(M / SD_MARGIN)
        mk_h = devig_power(oh["price"], oa["price"])
        sharp = h2h.get("pinnacle") or h2h.get("betfair_ex_eu")
        if sharp:
            sa, sh = _two(sharp, away, home)
            if sa and sh:
                mk_h = devig_power(sh["price"], sa["price"])
        ab_a, ab_h = TEAMS[away][0], TEAMS[home][0]
        kal = match_kalshi(kalshi, ab_a, ab_h, TEAMS[away][2], TEAMS[home][2])
        pm = match_polymarket(pm_events, TEAMS[away][2], TEAMS[home][2])
        pred = [p for p in ([kal["b"] / (kal["a"] + kal["b"])] if kal else []) + ([pm["pB"] / (pm["pA"] + pm["pB"])] if pm else [])]
        if pred:
            mk_h = 0.5 * mk_h + 0.5 * sum(pred) / len(pred)
        whale = whales(pm, away, home) if pm else {"trades": [], "usdA": 0, "usdB": 0}

        mix = lambda p, b: CONF * p + (1 - CONF) * b
        bets = []
        def add(market, pick, q, p_model, p_mkt, line=None, side=None):
            p = mix(p_model, p_mkt)
            bets.append({"market": market, "pick": pick, "odds": q, "line": line, "side": side,
                         "pModel": round(p_model, 4), "pMarket": round(p_mkt, 4), "p": round(p, 4),
                         "ev": round(p * q - 1, 4), "evRaw": round(p_model * q - 1, 4)})
        add("Sieg", away, oa["price"], 1 - p_model_h, 1 - mk_h, side="away")
        add("Sieg", home, oh["price"], p_model_h, mk_h, side="home")
        s365 = spr.get("bet365")
        if s365:
            sa, sh = _two(s365, away, home)
            if sa and sh and sa.get("point") is not None:
                line_h = sh["point"]                       # z. B. +2.5 für den Heim-Außenseiter
                p_cov_h = phi((M + line_h) / SD_MARGIN)
                m_cov_h = devig_power(sh["price"], sa["price"])
                sp = spr.get("pinnacle")
                if sp:
                    pa_, ph_ = _two(sp, away, home)
                    if pa_ and ph_ and ph_.get("point") == line_h:
                        m_cov_h = devig_power(ph_["price"], pa_["price"])
                add("Handicap", f"{away} {sa['point']:+g}", sa["price"], 1 - p_cov_h, 1 - m_cov_h, line=sa["point"], side="away")
                add("Handicap", f"{home} {line_h:+g}", sh["price"], p_cov_h, m_cov_h, line=line_h, side="home")
        t365 = tot.get("bet365")
        if t365:
            o = next((x for x in t365 if x["name"] == "Over"), None)
            u = next((x for x in t365 if x["name"] == "Under"), None)
            if o and u:
                p_over = 1 - phi((o["point"] - TOT) / SD_TOTAL)
                m_over = devig_power(o["price"], u["price"])
                tp = tot.get("pinnacle")
                if tp:
                    po = next((x for x in tp if x["name"] == "Over"), None)
                    pu = next((x for x in tp if x["name"] == "Under"), None)
                    if po and pu and po.get("point") == o["point"]:
                        m_over = devig_power(po["price"], pu["price"])
                add("Gesamtpunkte", f"Über {o['point']:g}", o["price"], p_over, m_over, line=o["point"], side="over")
                add("Gesamtpunkte", f"Unter {u['point']:g}", u["price"], 1 - p_over, 1 - m_over, line=u["point"], side="under")
        best = max(bets, key=lambda b: b["ev"])
        inj = {"away": injuries.get(away, []) or injuries.get(TEAMS[away][2].title(), []),
               "home": injuries.get(home, []) or injuries.get(TEAMS[home][2].title(), [])}
        games.append({
            "id": ev["id"], "sportKey": "basketball_nba", "sport": "NBA", "commence": ev["commence_time"], "away": away, "home": home,
            "abbrA": ab_a, "abbrH": ab_h, "margin": round(M, 1), "total": round(TOT, 1), "poss": round(poss, 1),
            "ptsA": round(pts_a, 1), "ptsH": round(pts_h, 1), "pModelHome": round(p_model_h, 4),
            "pMarketHome": round(mk_h, 4), "pFinalHome": round(mix(p_model_h, mk_h), 4),
            "parts": [{**x, "m": round(x["m"], 1)} for x in parts],
            "ratings": {"away": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in A.items()},
                        "home": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in H.items()}},
            "kalshi": kal, "polymarket": ({"pA": pm["pA"], "pB": pm["pB"], "volume": round(pm["volume"])} if pm else None),
            "whales": whale, "injuries": inj, "bets": bets, "best": best,
            "sideA": home, "pickLabel": f"{away} @ {home}",
        })
    games.sort(key=lambda g: -g["best"]["ev"])
    log(f"NBA: {len(games)} Spiele bewertet")
    return games
