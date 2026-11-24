#!/usr/bin/env python3
"""Hourly price collector for the Tanken app (stdlib only).

For every big city in cities.json it reads the official free fuel-price source of its country and stores ONE number
per fuel and run: the "typical cheap price" = mean of the 3 cheapest valid station prices within radius_km of the
city centre (same definition as the app's PricePatternService, so server and phone data are comparable).
History is kept for 30 days in data/<country>/<city>.json:

    {"id": "berlin", "country": "de", "name": "Berlin", "lat": .., "lng": .., "radius_km": 15,
     "fuels": {"e5": [[unix_seconds, price], ...], "e10": [...], "diesel": [...]}}

data/index.json lists the cities (the app downloads this small file first, then only the city it needs).

Sources (all official / open, no personal data):
  de  Tankerkönig (MTS-K)     creativecommons.tankerkoenig.de   needs TANKERKOENIG_KEY (repository secret)
  at  E-Control Spritpreisrechner   api.e-control.at
  fr  Prix des carburants     data.economie.gouv.fr
  es  MITECO                  sedeaplicaciones.minetur.gob.es

Usage:  python collector/collect.py [--only de,at] [--dry]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
WINDOW_S = 30 * 86400
MIN_GAP_S = 30 * 60  # a second run within 30 minutes does not add a point
UA = "TankenPriceHistory/1.0 (github.com/ahmdel/tanken-price-history)"
FUELS = ("e5", "e10", "diesel")


def get_json(url: str, timeout: int = 60, headers: dict | None = None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json", **(headers or {})})  # headers override
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    p = math.pi / 180
    a = 0.5 - math.cos((lat2 - lat1) * p) / 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * (1 - math.cos((lng2 - lng1) * p)) / 2
    return 12742 * math.asin(math.sqrt(a))


def typical_cheap(prices: list[float]) -> float | None:
    """Mean of the 3 cheapest valid prices (one wrong entry does not move it)."""
    ok = sorted(p for p in prices if p and 0.3 < p < 5)
    if not ok:
        return None
    top = ok[:3]
    return round(sum(top) / len(top), 4)


# ---------------------------------------------------------------- DE
def fetch_de(city: dict) -> dict[str, float]:
    key = os.environ.get("TANKERKOENIG_KEY", "")
    if not key:
        raise RuntimeError("TANKERKOENIG_KEY is not set")
    rad = min(float(city["radius_km"]), 25)
    q = urllib.parse.urlencode({"lat": city["lat"], "lng": city["lng"], "rad": rad, "sort": "dist", "type": "all", "apikey": key})
    data = get_json(f"https://creativecommons.tankerkoenig.de/json/list.php?{q}")
    if not data.get("ok"):
        raise RuntimeError(f"tankerkoenig: {data.get('message')}")
    out = {}
    for fuel in FUELS:
        v = typical_cheap([s.get(fuel) for s in data.get("stations", []) if s.get("isOpen", True) and isinstance(s.get(fuel), (int, float))])
        if v is not None:
            out[fuel] = v
    return out


# ---------------------------------------------------------------- AT
def fetch_at(city: dict) -> dict[str, float]:
    out = {}
    for fuel, code in (("diesel", "DIE"), ("e5", "SUP")):
        q = urllib.parse.urlencode({"latitude": city["lat"], "longitude": city["lng"], "fuelType": code, "includeClosed": "false"})
        data = get_json(f"https://api.e-control.at/sprit/1.0/search/gas-stations/by-address?{q}")
        prices = []
        for s in data if isinstance(data, list) else []:
            loc = s.get("location") or {}
            if loc.get("latitude") is None or loc.get("longitude") is None:
                continue
            if haversine_km(city["lat"], city["lng"], loc["latitude"], loc["longitude"]) > city["radius_km"]:
                continue
            pr = s.get("prices") or []
            if pr and isinstance(pr[0].get("amount"), (int, float)):
                prices.append(pr[0]["amount"])
        v = typical_cheap(prices)
        if v is not None:
            out[fuel] = v
    return out


# ---------------------------------------------------------------- FR
def fetch_fr(city: dict) -> dict[str, float]:
    out = {}
    base = "https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets/prix-des-carburants-en-france-flux-instantane-v2/records"
    for fuel, field in (("diesel", "gazole_prix"), ("e10", "e10_prix"), ("e5", "sp95_prix")):
        where = f"within_distance(geom, GEOM'POINT({city['lng']} {city['lat']})', {city['radius_km']}km) AND {field} IS NOT NULL"
        q = urllib.parse.urlencode({"limit": 5, "where": where, "order_by": f"{field} asc", "select": field})
        data = get_json(f"{base}?{q}")
        v = typical_cheap([r.get(field) for r in data.get("results", []) if isinstance(r.get(field), (int, float))])
        if v is not None:
            out[fuel] = v
    return out


# ---------------------------------------------------------------- ES
_es_cache: list | None = None


def _es_num(v):
    if v is None:
        return None
    try:
        return float(str(v).strip().replace(",", "."))
    except ValueError:
        return None


def fetch_es(city: dict) -> dict[str, float]:
    global _es_cache
    if _es_cache is None:
        # The ministry's server resets Python's TLS handshake but answers curl: use curl (present on GitHub runners).
        res = subprocess.run(
            ["curl", "-sS", "--fail", "-m", "120", "-H", "Accept: application/json", "-H", "User-Agent: Mozilla/5.0 (compatible; TankenPriceHistory/1.0)",
             "https://sedeaplicaciones.minetur.gob.es/ServiciosRESTCarburantes/PreciosCarburantes/EstacionesTerrestres/"],
            capture_output=True, check=True)
        data = json.loads(res.stdout.decode("utf-8", "replace"))
        _es_cache = data.get("ListaEESSPrecio") or []
    # E10 is left out on purpose: only a handful of Spanish stations sell it, the numbers jump around
    keys = {"diesel": "Precio Gasoleo A", "e5": "Precio Gasolina 95 E5"}
    near = []
    for s in _es_cache:
        lat, lng = _es_num(s.get("Latitud")), _es_num(s.get("Longitud (WGS84)"))
        if lat is None or lng is None:
            continue
        if haversine_km(city["lat"], city["lng"], lat, lng) <= city["radius_km"]:
            near.append(s)
    out = {}
    for fuel, key in keys.items():
        v = typical_cheap([_es_num(s.get(key)) for s in near])
        if v is not None:
            out[fuel] = v
    return out


FETCHERS = {"de": fetch_de, "at": fetch_at, "fr": fetch_fr, "es": fetch_es}


# ---------------------------------------------------------------- storage
def load_city(city: dict) -> dict:
    f = DATA / city["country"] / f"{city['id']}.json"
    base = {k: city[k] for k in ("id", "country", "name", "lat", "lng", "radius_km")}
    if f.exists():
        try:
            old = json.loads(f.read_text(encoding="utf-8"))
            base["fuels"] = old.get("fuels", {})
        except (OSError, ValueError):
            base["fuels"] = {}
    else:
        base["fuels"] = {}
    return base


def add_samples(doc: dict, prices: dict[str, float], now: int) -> int:
    added = 0
    for fuel, price in prices.items():
        series = doc["fuels"].setdefault(fuel, [])
        series[:] = [p for p in series if p[0] >= now - WINDOW_S]
        if series and now - series[-1][0] < MIN_GAP_S:
            continue
        series.append([now, price])
        added += 1
    return added


def write_city(doc: dict) -> None:
    f = DATA / doc["country"] / f"{doc['id']}.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def write_index(cities: list[dict], now: int) -> None:
    entries = []
    for c in cities:
        f = DATA / c["country"] / f"{c['id']}.json"
        if not f.exists():
            continue
        doc = json.loads(f.read_text(encoding="utf-8"))
        fuels = {k: len(v) for k, v in doc["fuels"].items() if v}
        if not fuels:
            continue
        first = min(v[0][0] for v in doc["fuels"].values() if v)
        entries.append({"id": c["id"], "country": c["country"], "name": c["name"], "lat": c["lat"], "lng": c["lng"],
                        "fuels": sorted(fuels), "samples": max(fuels.values()), "since": first, "file": f"data/{c['country']}/{c['id']}.json"})
    index = {"updated": now, "window_days": 30, "cities": entries}
    (DATA / "index.json").write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="comma separated country codes")
    ap.add_argument("--dry", action="store_true", help="print only, write nothing")
    args = ap.parse_args()
    only = {c.strip() for c in args.only.split(",") if c.strip()}
    cities = json.loads((ROOT / "cities.json").read_text(encoding="utf-8"))["cities"]
    now = int(time.time())
    ok = fail = 0
    for c in cities:
        if only and c["country"] not in only:
            continue
        fetch = FETCHERS.get(c["country"])
        if not fetch:
            continue
        try:
            prices = fetch(c)
        except Exception as e:  # one broken source must not stop the others
            fail += 1
            print(f"FAIL {c['country']}/{c['id']}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        if not prices:
            fail += 1
            print(f"NONE {c['country']}/{c['id']}: no prices", file=sys.stderr)
            continue
        ok += 1
        print(f"ok   {c['country']}/{c['id']:12s} " + "  ".join(f"{k}={v:.3f}" for k, v in sorted(prices.items())))
        if not args.dry:
            doc = load_city(c)
            add_samples(doc, prices, now)
            write_city(doc)
    if not args.dry:
        write_index([c for c in cities if not only or c["country"] in only] if only else cities, now)
    print(f"done: {ok} cities ok, {fail} failed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
