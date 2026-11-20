#!/usr/bin/env python3
"""Resolve audited OpenAlex institution IDs into a source-linked, city-level geodata layer."""
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = "https://api.openalex.org"
INPUT = HERE / "input" / "institution_ids_v08.txt"
OUT = HERE / "output"
OUT.mkdir(exist_ok=True)
ids = [x.strip() for x in INPUT.read_text(encoding="utf-8").splitlines() if x.strip()]
assert len(ids) == 396 and len(set(ids)) == len(ids), "Expected 396 unique institution IDs"

def fetch_json(url):
    headers = {"Accept": "application/json", "User-Agent": "PalimpsestoHeritageObservatory/0.9 (scientific bibliographic georeferencing)"}
    key = os.getenv("OPENALEX_API_KEY", "").strip()
    if key:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode({"api_key": key})
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=50) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            if err.code not in (429, 500, 502, 503, 504):
                raise
        except (TimeoutError, urllib.error.URLError):
            if attempt == 4: raise
        time.sleep(min(20, 2 ** (attempt + 1)))
    raise RuntimeError("OpenAlex unavailable after retries")

records = {}
batch_log = []
for start in range(0, len(ids), 50):
    subset = ids[start : start + 50]
    params = {
        "filter": "openalex_id:" + "|".join(subset),
        "per_page": len(subset),
        "select": "id,display_name,ror,geo,ids,type",
    }
    url = BASE + "/institutions?" + urllib.parse.urlencode(params)
    try:
        obj = fetch_json(url)
        found = {str(row.get("id", "")).rsplit("/", 1)[-1]: row for row in obj.get("results", [])}
        records.update(found)
        batch_log.append({"batch": len(batch_log) + 1, "requested": len(subset), "returned": len(found), "status": "OK"})
        print("Batch", len(batch_log), "returned", len(found), "/", len(subset), flush=True)
    except Exception as exc:
        batch_log.append({"batch": len(batch_log) + 1, "requested": len(subset), "returned": 0, "status": "ERROR", "detail": str(exc)})
        print("Batch failed", len(batch_log), type(exc).__name__, str(exc), flush=True)
    time.sleep(0.65)

headers = ["openalex_institution_id", "display_name", "ror", "wikidata_id", "city", "region", "country_code",
           "latitude", "longitude", "geo_scope", "lookup_status", "source_url", "retrieved_utc"]
now = datetime.now(timezone.utc).isoformat(timespec="seconds")
rows = []
features = []
for iid in ids:
    source = BASE + "/institutions/" + iid
    record = records.get(iid, {})
    geo = record.get("geo") or {}
    lat = geo.get("latitude")
    lon = geo.get("longitude")
    valid = isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and -90 <= lat <= 90 and -180 <= lon <= 180
    status = ("MATCHED_WITH_CITY_COORDINATES" if valid else ("MATCHED_NO_COORDINATES" if record else "NOT_RETURNED_OR_API_ERROR"))
    row = {
        "openalex_institution_id": "https://openalex.org/" + iid,
        "display_name": record.get("display_name") or "",
        "ror": record.get("ror") or "",
        "wikidata_id": (record.get("ids") or {}).get("wikidata") or "",
        "city": geo.get("city") or "",
        "region": geo.get("region") or "",
        "country_code": geo.get("country_code") or "",
        "latitude": lat if valid else "",
        "longitude": lon if valid else "",
        "geo_scope": "CITY_REFERENCE_NOT_CAMPUS" if valid else "UNRESOLVED",
        "lookup_status": status,
        "source_url": source,
        "retrieved_utc": now,
    }
    rows.append(row)
    if valid:
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {k: v for k, v in row.items() if k not in ("latitude", "longitude")}
        })

with (OUT / "openalex_institutions_396.csv").open("w", newline="", encoding="utf-8-sig") as stream:
    writer = csv.DictWriter(stream, fieldnames=headers)
    writer.writeheader()
    writer.writerows(rows)
(OUT / "openalex_institutions.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, indent=2), encoding="utf-8")
stats = {
    "retrieved_utc": now,
    "input_count": len(ids),
    "api_resolved": sum(r["lookup_status"].startswith("MATCHED") for r in rows),
    "with_coordinates": len(features),
    "pending": sum(r["lookup_status"] == "NOT_RETURNED_OR_API_ERROR" for r in rows),
    "batch_log": batch_log,
    "coordinate_note": "OpenAlex geo values represent reference locality, not surveyed campus positions.",
}
(OUT / "run_audit.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({k: v for k, v in stats.items() if k != "batch_log"}, ensure_ascii=False), flush=True)
if stats["api_resolved"] == 0: sys.exit("No live OpenAlex institutions resolved; inspect audit/logs.")
