"""Prognosemärkte (Kalshi, Polymarket) den Spielen zuordnen und Großeinsätze ("Whales") finden."""
import json
import time

from .common import norm
from .sources import polymarket_trades

WHALE_USD = 1000      # ab diesem Einsatz gilt ein Trade als Großeinsatz
WHALE_HOURS = 48


def _contains_all(text, keys):
    t = norm(text)
    return all(k and k in t for k in keys)


def _pm_moneyline(event, key_a, key_b):
    """Sucht im Polymarket-Event den Sieger-Markt mit beiden Namen als Ausgängen."""
    for m in event.get("markets", []):
        q = norm(m.get("question", ""))
        if any(w in q for w in ("spread", "o u", "over", "under", "total", "set ", "handicap")):
            continue
        try:
            outs = json.loads(m.get("outcomes") or "[]")
            prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
        except Exception:  # noqa: BLE001
            continue
        if len(outs) != 2 or len(prices) != 2:
            continue
        o0, o1 = norm(outs[0]), norm(outs[1])
        if key_a in o0 and key_b in o1:
            return {"pA": prices[0], "pB": prices[1], "condition": m.get("conditionId"), "volume": float(m.get("volume") or 0), "outcomes": outs}
        if key_a in o1 and key_b in o0:
            return {"pA": prices[1], "pB": prices[0], "condition": m.get("conditionId"), "volume": float(m.get("volume") or 0), "outcomes": [outs[1], outs[0]]}
    return None


def match_polymarket(pm_events, key_a, key_b):
    for ev in pm_events:
        if _contains_all(ev.get("title", "") + " " + ev.get("slug", "").replace("-", " "), [key_a, key_b]):
            ml = _pm_moneyline(ev, key_a, key_b)
            if ml:
                ml["title"] = ev.get("title", "")
                return ml
    return None


def match_kalshi(k_events, abbr_a, abbr_b, key_a, key_b):
    """Kalshi-Spielmarkt finden. Rückgabe: Chance A/B in Prozent und Volumen."""
    for ev in k_events:
        t = ev["ticker"].upper()
        title_ok = _contains_all(ev.get("title", ""), [key_a, key_b])
        ticker_ok = abbr_a and abbr_b and abbr_a in t and abbr_b in t
        if not (title_ok or ticker_ok):
            continue
        pa = pb = None
        vol = 0
        for m in ev["markets"]:
            vol += float(m.get("volume") or 0)
            tk = m["ticker"].upper()
            title = norm(m.get("title", ""))
            if (abbr_a and tk.endswith("-" + abbr_a)) or (key_a and key_a in title):
                pa = m["price"]
            elif (abbr_b and tk.endswith("-" + abbr_b)) or (key_b and key_b in title):
                pb = m["price"]
        if pa and pb:
            return {"a": round(pa, 1), "b": round(pb, 1), "vol": round(vol)}
    return None


def _wallet(t):
    w = t.get("proxy_wallet") or t.get("proxyWallet") or ""
    name = t.get("pseudonym") or t.get("name") or ""
    short = w[:6] + "…" + w[-4:] if w else ""
    return f"{name} ({short})" if name and short else short or name


def whales(pm, side_a_name, side_b_name):
    """Großeinsätze der letzten Stunden für einen Polymarket-Markt."""
    if not pm or not pm.get("condition"):
        return {"trades": [], "usdA": 0, "usdB": 0}
    cutoff = time.time() - WHALE_HOURS * 3600
    trades = []
    usd = {"A": 0.0, "B": 0.0}
    oa, ob = norm(pm["outcomes"][0]), norm(pm["outcomes"][1])
    for t in polymarket_trades(pm["condition"]):
        try:
            ts = float(t.get("timestamp", 0))
            ts = ts / 1000 if ts > 1e12 else ts
            amount = float(t.get("size", 0)) * float(t.get("price", 0))
        except Exception:  # noqa: BLE001
            continue
        if ts < cutoff or amount < WHALE_USD:
            continue
        outcome = norm(t.get("outcome", ""))
        buy = str(t.get("side", "BUY")).upper() == "BUY"
        side = "A" if outcome == oa else "B" if outcome == ob else None
        if side is None:
            continue
        # Verkauf einer Seite wirkt wie eine Wette auf die andere
        eff = side if buy else ("B" if side == "A" else "A")
        usd[eff] += amount
        trades.append({"side": eff, "team": side_a_name if eff == "A" else side_b_name, "usd": round(amount),
                       "price": round(float(t.get("price", 0)), 3), "buy": buy, "ts": int(ts),
                       "wallet": _wallet(t)})
    trades.sort(key=lambda x: -x["usd"])
    return {"trades": trades[:10], "usdA": round(usd["A"]), "usdB": round(usd["B"])}
