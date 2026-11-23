"""Push-Nachrichten über ntfy.sh (kostenlos, ohne Konto): App „ntfy“ installieren und das Thema abonnieren."""
import os
from zoneinfo import ZoneInfo

import requests

from .common import load_json, log, parse_time, save_json

TOPIC = os.environ.get("NTFY_TOPIC") or "quoten-b6a776bab3f5"
SITE = "https://seimillo.github.io/quoten-modell/"
BERLIN = ZoneInfo("Europe/Berlin")


def send(title, lines, tags=("basketball",), priority=3):
    if not lines or os.environ.get("NO_NOTIFY"):
        return False
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


def notify_value_bets(nba, title="Neue ✓-Wetten"):
    """Meldet nur ✓-Wetten, die noch nicht gemeldet wurden."""
    sent = load_json("notified.json", {})
    lines, keys = [], []
    for g in nba:
        for b in g["bets"]:
            if b.get("value") and _key(g, b) not in sent:
                lines.append(_line(g, b))
                keys.append((_key(g, b), g["commence"]))
    if lines and send(title, lines, tags=("basketball", "white_check_mark"), priority=4):
        for k, c in keys:
            sent[k] = c
    # alte Einträge aufräumen
    keep = sorted(sent.items(), key=lambda kv: kv[1])[-300:]
    save_json("notified.json", dict(keep))


def notify_changes(nba):
    """Abend-Update: Empfehlungen, die sich seit mittags geändert haben."""
    lines = []
    for g in nba:
        mb = g.get("middayBest") or {}
        if mb.get("value") and not any(b.get("value") and b["pick"] == mb.get("pick") for b in g["bets"]):
            lines.append(f"⚠ {g['abbrA']} @ {g['abbrH']}: {mb['pick']} ist kein ✓ mehr (neue Verletzungsmeldung/Quote)")
    send("Abend-Update: Änderungen", lines, tags=("basketball", "warning"), priority=4)
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
