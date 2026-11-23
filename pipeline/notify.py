"""Push-Nachrichten über ntfy.sh (kostenlos, ohne Konto): App „ntfy“ installieren und das Thema abonnieren."""
import os
from zoneinfo import ZoneInfo

import requests

from .common import load_json, log, parse_time, save_json

TOPIC = os.environ.get("NTFY_TOPIC") or "quoten-b6a776bab3f5"
SITE = "https://seimillo.github.io/quoten-modell/"
BERLIN = ZoneInfo("Europe/Berlin")


def quiet_hours(now=None):
    """22–9 Uhr deutscher Zeit: Nachrichten ohne Ton und Vibration (morgens in der Liste)."""
    from datetime import datetime
    h = (now or datetime.now(BERLIN)).astimezone(BERLIN).hour
    return h >= 22 or h < 9


def send(title, lines, tags=("basketball",), priority=3):
    if not lines or os.environ.get("NO_NOTIFY"):
        return False
    if quiet_hours():
        priority = 2                                  # ntfy „niedrig“: kein Ton, keine Vibration
    try:
        r = requests.post("https://ntfy.sh/", json={"topic": TOPIC, "title": title, "message": "\n".join(lines),
                                                    "tags": list(tags), "priority": priority, "click": SITE},
                          timeout=15)
        log(f"Push-Nachricht „{title}“: {'gesendet' if r.ok else 'Fehler ' + str(r.status_code)}")
        return r.ok
    except Exception as e:  # noqa: BLE001
        log(f"Push-Nachricht fehlgeschlagen: {e}")
        return False


def _time(iso):
    return parse_time(iso).astimezone(BERLIN).strftime("%H:%M")


def _line(g, b):
    return (f"✓ {g['abbrA']} @ {g['abbrH']} {_time(g['commence'])} Uhr: {b['pick']} @ {b['odds']:.2f} {b.get('bookName', '')}"
            f" · EV {b['ev'] * 100:+.1f} % · Einsatz {b['stake'] * 100:.1f} %")


def _key(g, b):
    return f"{g['id']}|{b['market']}|{b['pick']}"


def notify_value_bets(nba, title="Neue Wetten (✓ Modell, ⚡ besser als Pinnacle)"):
    """Meldet nur ✓-Wetten, die noch nicht gemeldet wurden."""
    sent = load_json("notified.json", {})
    lines, keys = [], []
    for g in nba:
        for b in g["bets"]:
            if b.get("value") and _key(g, b) not in sent:
                lines.append(_line(g, b))
                keys.append((_key(g, b), g["commence"]))
        fl = g.get("flash")
        if fl and "⚡" + _key(g, fl) not in sent:
            lines.append(f"⚡ {g['abbrA']} @ {g['abbrH']} {_time(g['commence'])} Uhr: {fl['pick']} @ {fl['odds']:.2f} "
                         f"{fl.get('bookName', '')} · {fl['evPin'] * 100:+.1f} % über Pinnacle · Einsatz {fl['stake'] * 100:.1f} %")
            keys.append(("⚡" + _key(g, fl), g["commence"]))
    if lines and send(title, lines, tags=("basketball", "white_check_mark"), priority=4):
        for k, c in keys:
            sent[k] = c
    # alte Einträge aufräumen
    keep = sorted(sent.items(), key=lambda kv: kv[1])[-300:]
    save_json("notified.json", dict(keep))


def notify_changes(nba):
    """Abend-Update: Empfehlungen, die sich seit mittags geändert haben."""
    lines = []
    sent = load_json("notified.json", {})
    for g in nba:
        mb = g.get("middayBest") or {}
        k = f"W|{g['id']}|{mb.get('pick')}"
        if mb.get("value") and k not in sent and not any(b.get("value") and b["pick"] == mb.get("pick") for b in g["bets"]):
            lines.append(f"⚠ {g['abbrA']} @ {g['abbrH']}: {mb['pick']} ist kein ✓ mehr (neue Verletzungsmeldung/Quote)")
            sent[k] = g["commence"]
    if send("Abend-Update: Änderungen", lines, tags=("basketball", "warning"), priority=4):
        save_json("notified.json", dict(sorted(sent.items(), key=lambda kv: kv[1])[-400:]))
    notify_value_bets(nba, "Abend-Update: neue ✓-Wetten")


def notify_results(settled):
    """Ergebnisse der ✓-Wetten seit dem letzten Lauf, bei 10 € Einsatz."""
    rows = [r for r in settled if (r.get("bet") or {}).get("value")]
    if not rows:
        return
    lines, total = [], 0.0
    for r in rows:
        b = r["bet"]
        g = 10 * (b["odds"] - 1) if r["betResult"] == "win" else -10 if r["betResult"] == "loss" else 0
        total += g
        word = "gewonnen" if r["betResult"] == "win" else "verloren" if r["betResult"] == "loss" else "zurück"
        lines.append(f"{r['match']}: {b['pick']} @ {b['odds']:.2f} – {word} ({g:+.2f} €)")
    lines.append(f"Summe bei 10 € je Wette: {total:+.2f} €")
    send("Ergebnisse der ✓-Wetten", lines, tags=("basketball", "moneybag"))


PROBLEM_WORDS = ("fehlgeschlagen", "nicht lesbar", "nicht gefunden", "übersprungen", "Kein ODDS_API_KEY", "Format unbekannt")
IGNORE = ("Polymarket Trades", "Push-Nachricht")


def notify_problems(log_lines, credits=None, mode="Lauf"):
    """Warnt, wenn eine Datenquelle ausgefallen ist oder die Credits knapp werden."""
    lines = [l for l in log_lines if any(w in l for w in PROBLEM_WORDS) and not any(i in l for i in IGNORE)]
    lines = list(dict.fromkeys(lines))[:8]
    rem = (credits or {}).get("remaining")
    if rem is not None and rem < 60:
        lines.append(f"Nur noch {rem:.0f} Credits bei The Odds API übrig.")
    if lines:
        send(f"Warnung: {mode} mit Problemen", lines, tags=("warning",), priority=3)


def notify_props(prop_games, limit=6):
    """Neue ✓-Spielerwetten (höchstens `limit` pro Nachricht, beste zuerst)."""
    sent = load_json("notified.json", {})
    cands = []
    for g in prop_games:
        for p in g["props"]:
            k = f"P|{g['id']}|{p['key']}|{p['stat']}|{p['line']}|{p['side']}"
            if p.get("value") and k not in sent:
                cands.append((p["ev"], k, g, p))
    cands.sort(key=lambda x: -x[0])
    lines = []
    for ev, k, g, p in cands[:limit]:
        side = "über" if p["side"] == "over" else "unter"
        lines.append(("🟢 " if p.get("safe") else "🟠 ") + f"{p['player']} {side} {p['line']:g} {p['statName']} @ {p['odds']:.2f} {p['bookName']} · "
                     f"Sim Ø {p['mean']:g} · EV {p['ev'] * 100:+.1f} %" + (" · in App prüfen" if p.get("checkApp") else "")
                     + ("" if p.get("safe") else f" · Risiko: {p['risks'][0]}"))
        sent[k] = g["commence"]
    if lines:
        lines.append("🟢 = mittags sicher · 🟠 = kann sich bis abends noch ändern")
    if lines and send("Spielerwetten ✓", lines, tags=("basketball", "dart"), priority=3):
        save_json("notified.json", dict(sorted(sent.items(), key=lambda kv: kv[1])[-400:]))


def notify_props_results(rows):
    rows = [t for t in rows if t.get("result") in ("win", "loss", "push")]
    if not rows:
        return
    lines, total = [], 0.0
    for t in rows:
        g = 10 * (t["odds"] - 1) if t["result"] == "win" else -10 if t["result"] == "loss" else 0
        total += g
        side = "über" if t["side"] == "over" else "unter"
        lines.append(f"{t['player']} {side} {t['line']:g} {t['statName']}: {t.get('value')} – "
                     f"{'gewonnen' if t['result'] == 'win' else 'verloren' if t['result'] == 'loss' else 'zurück'} ({g:+.2f} €)")
    lines.append(f"Summe bei 10 € je Wette: {total:+.2f} €")
    send("Ergebnisse Spielerwetten", lines, tags=("basketball", "moneybag"))


def notify_props_pregame(checked, new, limit=6):
    """Kurz vor Spielbeginn: Mittags-Tipps bestätigt oder zurückziehen, dazu neue ✓."""
    lines = []
    sym = {"bestätigt": "✅", "schwächer": "➖", "zurückziehen": "❌"}
    for t, pre in checked:
        side = "über" if t["side"] == "over" else "unter"
        st = pre["status"]
        lines.append(f"{sym.get(st, '⚠️')} {t['player']} {side} {t['line']:g} {t['statName']}: {st}"
                     + (f" (EV jetzt {pre['ev'] * 100:+.1f} %)" if pre.get("ev") is not None else ""))
    for g, p in sorted(new, key=lambda x: -x[1]["ev"])[:limit]:
        side = "über" if p["side"] == "over" else "unter"
        lines.append(f"🆕 {p['player']} {side} {p['line']:g} {p['statName']} @ {p['odds']:.2f} {p['bookName']} · "
                     f"EV {p['ev'] * 100:+.1f} % · {_time(g['commence'])} Uhr")
    if lines:
        send("Spielerwetten vor Spielbeginn", lines, tags=("basketball", "stopwatch"), priority=3)
