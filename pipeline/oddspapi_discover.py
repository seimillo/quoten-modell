"""Einmalige Erkundung von OddsPapi: Buchmacher-Namen, Markt-Kennungen und ein Beispiel-Abruf der NBA.
Kostet 5 Abrufe. Ergebnis (ohne Schlüssel) in docs/data/oddspapi_discovery.json."""
import json
import os
import time

import requests

from .common import save_json

BASE = "https://api.oddspapi.io/v4"


def call(path, **params):
    params["apiKey"] = os.environ["ODDSPAPI_KEY"]
    r = requests.get(f"{BASE}/{path}", params=params, timeout=40)
    time.sleep(1.2)
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        body = r.text[:2000]
    return {"status": r.status_code, "body": body}


def trim(x, depth=0, maxlist=6):
    if isinstance(x, list):
        return [trim(v, depth + 1) for v in x[:maxlist]] + ([f"... {len(x) - maxlist} more"] if len(x) > maxlist else [])
    if isinstance(x, dict):
        return {k: trim(v, depth + 1) for k, v in list(x.items())[:60]}
    return x


def main():
    out = {}
    sports = call("sports")
    out["sports"] = sports
    bk = call("bookmakers")
    body = bk["body"] if isinstance(bk["body"], list) else []
    out["bookmakers"] = {"status": bk["status"], "matches": [b for b in body if isinstance(b, dict) and any(
        s in json.dumps(b).lower() for s in ("bet365", "bwin", "tipico", "interwetten", "winamax", "betano", "betway", "888", "leovegas", "admiral", "merkur", "neo"))]}
    basket = next((s.get("sportId") for s in (sports["body"] if isinstance(sports["body"], list) else [])
                   if "basket" in json.dumps(s).lower()), 11)
    m = call("markets", sportId=basket)
    out["markets_status"] = m["status"]
    mb = m["body"] if isinstance(m["body"], list) else []
    out["markets"] = [x for x in mb if isinstance(x, dict)][:80]
    slug = next((b.get("slug") or b.get("bookmakerSlug") for b in out["bookmakers"]["matches"]
                 if "bet365" in json.dumps(b).lower() and ("de" in json.dumps(b).lower())), "bet365")
    o = call("odds-by-tournaments", tournamentIds=132, bookmaker=slug, oddsFormat="decimal")
    out["odds_slug"] = slug
    out["odds"] = {"status": o["status"], "body": trim(o["body"])}
    save_json("oddspapi_discovery.json", out)
    print("fertig", {k: (v.get("status") if isinstance(v, dict) else len(v)) for k, v in out.items()})


if __name__ == "__main__":
    main()
