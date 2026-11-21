#!/usr/bin/env python3
"""Fetch and audit the OFFICIAL UNESCO DataHub World Heritage List (whc001).

No OpenAlex records are used. Output is property-level reference markers,
not official UNESCO boundary polygons. The full raw JSON source is included.
"""
import csv, json, urllib.request, urllib.parse, urllib.error, time
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path

HERE=Path(__file__).resolve().parent
OUT=HERE/"output-unesco"
OUT.mkdir(parents=True,exist_ok=True)
API="https://data.unesco.org/api/explore/v2.1/catalog/datasets/whc001/records"
def fetch(url):
    last=None
    for retry in range(6):
        try:
            req=urllib.request.Request(url,headers={"User-Agent":"Palimpsesto Research Observatory/0.14 (UNESCO official data audit)","Accept":"application/json"})
            with urllib.request.urlopen(req,timeout=65) as r: return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last=e;time.sleep(min(30,2**retry))
    raise RuntimeError("UNESCO DataHub fetch failed: "+str(last))

records=[]
total=None
for offset in range(0,2000,100):
    q=urllib.parse.urlencode({"limit":100,"offset":offset})
    data=fetch(API+"?"+q)
    if total is None: total=data.get("total_count")
    chunk=data.get("results",[])
    if not isinstance(chunk,list):raise RuntimeError("Unexpected response")
    records.extend(chunk)
    print("UNESCO offset",offset,"received",len(chunk),"total",total,flush=True)
    if total is not None and len(records)>=int(total):break
    if not chunk:break
    time.sleep(.28)
if total is None or len(records)!=int(total):raise RuntimeError(f"Incomplete UNESCO download: {len(records)} of {total}")
now=datetime.now(timezone.utc).isoformat(timespec="seconds")
fields=Counter()
for item in records:fields.update(item.keys())
print("Sample keys:",dict(fields.most_common(40)),flush=True)
(OUT/"unesco_raw_datahub_records.json").write_text(json.dumps({"dataset":"whc001","retrieved_utc":now,"total":total,"results":records},ensure_ascii=False),encoding="utf-8")

def first(*names,record):
    for k in names:
        x=record.get(k)
        if x not in (None,"",[],{}):return x
    return None

def country_names(r):
    val=first("states_names","states_parties","states_name","states",record=r)
    if isinstance(val,list):
        return "; ".join(str(v.get("name") if isinstance(v,dict) else v) for v in val)
    return str(val or "")

def geocoords(r):
    # geo_point_2d is [latitude, longitude] in DataHub
    p=r.get("geo_point_2d")
    if isinstance(p,dict):
        a,b=p.get("lat",p.get("latitude")),p.get("lon",p.get("longitude"))
        if a is None or b is None:
            coords=p.get("coordinates")
            if isinstance(coords,list) and len(coords)>=2:b,a=coords[:2]
    elif isinstance(p,(list,tuple)) and len(p)>=2:a,b=p[:2]
    else:a=b=None
    if a is None or b is None:
        g=r.get("coordinates")
        if isinstance(g,dict):
            pts=g.get("coordinates")
            if isinstance(pts,list) and pts:
                pair=pts[0] if isinstance(pts[0],(list,tuple)) else pts
                if isinstance(pair,(list,tuple)) and len(pair)>=2:b,a=pair[:2]
    try:
        a,b=float(a),float(b)
        if -90<=a<=90 and -180<=b<=180:return a,b
    except Exception:pass
    return None,None
rows=[]; missing=[];features=[];dupids=[]
seen=set()
for item in records:
    rid=str(first("id_no","id",record=item) or "")
    title=first("site_en","name_en","site","name","property_name","name_en",record=item)
    if isinstance(title,dict):title=title.get("en") or next(iter(title.values()),"")
    title=str(title or "Unnamed UNESCO property")
    if rid in seen: dupids.append(rid)
    seen.add(rid)
    lat,lon=geocoords(item)
    if lat is None:missing.append(rid)
    category=first("category","category_name",record=item)
    if isinstance(category,dict):category=category.get("name") or str(category)
    year=first("date_inscribed","year_inscribed","year",record=item)
    serial=item.get("components_count")
    source=f"https://whc.unesco.org/en/list/{rid}/" if rid else "https://whc.unesco.org/en/list/"
    row={"unesco_id":rid,"name":title,"category":str(category or ""),"country":country_names(item),
         "year_inscribed":str(year or ""),"latitude":lat if lat is not None else "","longitude":lon if lon is not None else "",
         "components_count":serial if serial is not None else "",
         "reference_point_kind":"PROPERTY_POINT_FROM_UNESCO_DATAHUB_NOT_BOUNDARY",
         "source_url":source,"dataset":"UNESCO DataHub whc001","retrieved_utc":now}
    rows.append(row)
    if lat is not None:features.append({"type":"Feature","geometry":{"type":"Point","coordinates":[lon,lat]},"properties":{k:v for k,v in row.items() if k not in("latitude","longitude")}})
with (OUT/"unesco_whc001_properties.csv").open("w",newline="",encoding="utf-8-sig") as f:
    w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
(OUT/"unesco_whc001_reference_points.geojson").write_text(json.dumps({"type":"FeatureCollection","name":"UNESCO World Heritage List whc001 - property reference points","features":features},ensure_ascii=False),encoding="utf-8")
audit={"publisher":"UNESCO DataHub","dataset":"whc001","api":API,"license":"CC BY-SA 4.0","retrieved_utc":now,
       "records_advertised":total,"properties_downloaded":len(records),"distinct_ids":len(seen),
       "properties_with_reference_point":len(features),"properties_without_reference_point":len(missing),
       "missing_ids":missing,"duplicate_ids":dupids,
       "category_counts":dict(Counter(r["category"] for r in rows)),
       "geometry_policy":"One property-level reference marker per UNESCO List ID; serial components and World Heritage boundaries not represented by this one marker."}
(OUT/"unesco_audit.json").write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps({k:v for k,v in audit.items() if k not in ("missing_ids","duplicate_ids")},ensure_ascii=False),flush=True)
if len(seen)<1000:raise RuntimeError("Too few UNESCO properties for full world map")
