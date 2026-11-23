"""Gemeinsame Hilfsfunktionen: HTTP, Wahrscheinlichkeiten, Namensabgleich, Logging."""
import json
import math
import os
import re
import time
import unicodedata
from datetime import datetime, timezone

import requests

UA = {"User-Agent": "Mozilla/5.0 (quoten-modell; Lernprojekt) Python-requests"}
LOG = []          # Meldungen für die Statusseite
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "docs", "data")


def mask(msg):
    """Schlüssel nie in Meldungen: apiKey=… und die Werte der geheimen Umgebungsvariablen schwärzen."""
    msg = re.sub(r"(api[_-]?key=)[^&\s'\"]+", r"\1***", str(msg), flags=re.I)
    for var in ("ODDS_API_KEY", "ODDSPAPI_KEY", "NTFY_TOPIC"):
        v = os.environ.get(var, "").strip()
        if len(v) >= 8:
            msg = msg.replace(v, "***")
    return msg


def log(msg):
    msg = mask(msg)
    print(msg, flush=True)
    LOG.append(msg)


def get(url, params=None, as_json=True, tries=3, timeout=30):
    """GET mit Wiederholung. Gibt (daten, response) zurück oder wirft die letzte Ausnahme."""
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=timeout)
            if r.status_code == 429:
                time.sleep(5 * (i + 1))
                continue
            r.raise_for_status()
            return (r.json() if as_json else r.text), r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise last


def load_json(name, default):
    path = os.path.join(DATA_DIR, name)
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(name, obj):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def now_utc():
    return datetime.now(timezone.utc)


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------- Wahrscheinlichkeiten ----------

def devig_power(qa, qb):
    """Marge entfernen (Power-Methode: Außenseiter trägt mehr Marge). Gibt P(A) zurück."""
    ia, ib = 1 / qa, 1 / qb
    lo, hi = 1.0, 5.0
    for _ in range(60):
        k = (lo + hi) / 2
        if ia ** k + ib ** k > 1:
            lo = k
        else:
            hi = k
    return ia ** ((lo + hi) / 2)


def phi(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def logit(p):
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def sig(x):
    return 1 / (1 + math.exp(-x))


def elo_p(a, b):
    return 1 / (1 + 10 ** ((b - a) / 400))


# ---------- Namen ----------

def norm(name):
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z ]", " ", s.lower())
    return " ".join(s.split())


def last_name(name):
    parts = norm(name).split()
    return parts[-1] if parts else ""
