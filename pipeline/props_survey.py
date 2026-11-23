"""Einmalige Bestandsaufnahme: Welche NBA-Spielerwetten bieten die deutschen Buchmacher an? (~10 OddsPapi-Abrufe)"""
from collections import Counter, defaultdict

from . import oddspapi as OP
from .common import log, save_json

DE_BOOKS = ["bet365.de", "bwin.de", "tipico", "winamax.de", "betano.de", "betway.de", "888sport.de", "sportwetten.de", "merkurbets"]


def main():
    data = OP._get("markets", sportId=OP.SPORT_BASKETBALL)
    items = data if isinstance(data, list) else data.get("markets", data.get("data", []))
    props = {str(m["marketId"]): {"name": m.get("marketName"), "type": m.get("marketType"), "line": m.get("handicap"),
                                  "period": m.get("period"), "outcomes": {str(o["outcomeId"]): o.get("outcomeName") for o in m.get("outcomes", [])}}
             for m in items if isinstance(m, dict) and m.get("playerProp")}
    save_json("oddspapi_prop_markets.json", props)
    log(f"Spielerwetten-Märkte bei OddsPapi (Basketball): {len(props)}")
    survey = {}
    for slug in DE_BOOKS:
        try:
            fx = OP._get("odds-by-tournaments", tournamentIds=OP.NBA_TOURNAMENT, bookmaker=slug, oddsFormat="decimal")
        except Exception as e:  # noqa: BLE001
            survey[slug] = {"error": str(e)[:200]}
            continue
        fx = fx if isinstance(fx, list) else fx.get("data", [])
        kinds, players, games = Counter(), defaultdict(set), 0
        example = {}
        for f in fx:
            bo = (f.get("bookmakerOdds") or {}).get(slug) or {}
            if bo.get("markets"):
                games += 1
            for mid, m in (bo.get("markets") or {}).items():
                d = props.get(str(mid))
                if not d:
                    continue
                key = f"{d['name']} [{d['type']}, {d['period']}]"
                for oid, o in (m.get("outcomes") or {}).items():
                    for pk, p in (o.get("players") or {}).items():
                        if pk == "0" or not p.get("price"):
                            continue
                        kinds[key] += 1
                        players[key].add(p.get("playerName"))
                        example.setdefault(key, {"player": p.get("playerName"), "line": d["line"],
                                                 "outcome": d["outcomes"].get(str(oid)), "price": p.get("price")})
        survey[slug] = {"games": games, "markets": {k: {"quotes": v, "players": len(players[k]), "example": example[k]}
                                                     for k, v in kinds.most_common()}}
        log(f"Spielerwetten {slug}: {games} Spiele, {len(kinds)} Arten")
    save_json("props_survey.json", survey)


if __name__ == "__main__":
    main()
