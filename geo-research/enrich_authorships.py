#!/usr/bin/env python3
"""Palimpsesto v0.10: reconcile 1,250 corpus papers with OpenAlex authorships and institution geography.

Inputs are source-curated corpus IDs, titles, publication years and DOI.
DOI matches are validated against titles, never inferred from author home institutions.
"""
import csv, json, os, re, sys, time, unicodedata
import urllib.parse, urllib.request, urllib.error
from collections import defaultdict, Counter
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

ROOT=Path(__file__).resolve().parent
SRC=ROOT/"input"/"corpus_1250_doi_map_v10.csv"
OUT=ROOT/"output-authorships"
OUT.mkdir(parents=True,exist_ok=True)
API="https://api.openalex.org"
NOW=datetime.now(timezone.utc).isoformat(timespec="seconds")
with SRC.open(encoding="utf-8-sig",newline="") as f: papers=list(csv.DictReader(f))
assert len(papers)==1250 and len({p["publication_id"] for p in papers})==1250

def doi_norm(val):
    s=urllib.parse.unquote((val or "").strip().lower())
    s=re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", s).strip().rstrip(".")
    return s
def oa_id(obj):
    return (obj or "").rsplit("/",1)[-1]
def norm_title(text):
    s=unicodedata.normalize("NFKD",text or "")
    s="".join(c for c in s if not unicodedata.combining(c)).lower()
    return " ".join(re.findall(r"[a-z0-9]+",s))
def similarity(a,b):
    aa,bb=norm_title(a),norm_title(b)
    return round(SequenceMatcher(None,aa,bb).ratio(),4) if aa and bb else 0.0
def request_json(url):
    key=os.getenv("OPENALEX_API_KEY","").strip()
    if key: url+=("&" if "?" in url else "?")+urllib.parse.urlencode({"api_key":key})
    req=urllib.request.Request(url,headers={"Accept":"application/json","User-Agent":"PalimpsestoHeritageResearch/0.10 (public bibliographic provenance audit)"})
    error=None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req,timeout=90) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error=exc
            if exc.code not in (429,500,502,503,504): break
        except (TimeoutError,urllib.error.URLError) as exc: error=exc
        time.sleep(min(25,2**(attempt+1)))
    raise RuntimeError(str(error))
def batched(seq,n):
    for i in range(0,len(seq),n):yield seq[i:i+n]
def write_csv(path, fields, rows):
    with path.open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction="ignore")
        w.writeheader();w.writerows(rows)

doi_list=sorted({doi_norm(p.get("doi")) for p in papers if doi_norm(p.get("doi"))})
assert len(doi_list)==1114
works=defaultdict(list)
errors=[]
batch_log=[]
def get_works(batch):
    # OpenAlex DOI filter allows up to 100 alternatives per request.
    # Recursive subdivision recovers from one invalid/oversized DOI without losing the batch.
    param={"filter":"doi:"+"|".join("https://doi.org/"+d for d in batch),
           "per_page":max(50,len(batch)),
           "select":"id,doi,display_name,publication_year,authorships,type"}
    url=API+"/works?"+urllib.parse.urlencode(param)
    try:
        obj=request_json(url)
        return obj.get("results",[])
    except Exception as e:
        if len(batch)>1:
            half=len(batch)//2
            return get_works(batch[:half])+get_works(batch[half:])
        errors.append({"type":"DOI_REQUEST_FAILED","key":batch[0],"error":str(e)})
        return []
for bno,batch in enumerate(batched(doi_list,45),1):
    result=get_works(batch)
    keys=set(batch)
    new=0
    for w in result:
        d=doi_norm(w.get("doi"))
        if d in keys:works[d].append(w);new+=1
    batch_log.append({"batch":bno,"requested":len(batch),"results_matching_input":new})
    print(f"DOI batch {bno}: matched {new}/{len(batch)}",flush=True)
    time.sleep(0.3)

paper_rows=[]
auth_rows=[]
raw_work_json=[]
unique_inst=set()
for p in papers:
    pid,doi=p["publication_id"],doi_norm(p.get("doi"))
    candidates=works.get(doi,[]) if doi else []
    ranked=sorted(candidates,key=lambda w:similarity(p["title"],w.get("display_name")),reverse=True)
    chosen=ranked[0] if ranked else None
    sim=similarity(p["title"], chosen.get("display_name")) if chosen else 0
    if not doi:status="NO_DOI"
    elif not chosen:status="DOI_NOT_RETURNED"
    elif len(candidates)>1:status="MULTIPLE_OPENALEX_WORKS_FOR_DOI_REVIEW"
    elif sim>=0.78:status="DOI_TITLE_CONFIRMED"
    elif sim>=0.55:status="DOI_TITLE_VARIANT_REVIEW"
    else:status="DOI_TITLE_CONFLICT_REVIEW"
    n_auth=len(chosen.get("authorships") or []) if chosen else 0
    inst=set()
    correspondent=0
    for a in chosen.get("authorships") or [] if chosen else []:
        if a.get("is_corresponding") is True:correspondent+=1
        for i in a.get("institutions") or []:
            if oa_id(i.get("id")).startswith("I"):inst.add(oa_id(i.get("id")))
    paper_rows.append({
        "publication_id":pid,"title_original":p["title"],"year_original":p["year"],"doi_original":p.get("doi") or "",
        "doi_normalized":doi,"openalex_work_id":chosen.get("id","") if chosen else "",
        "openalex_work_title":chosen.get("display_name","") if chosen else "",
        "openalex_publication_year":chosen.get("publication_year","") if chosen else "",
        "title_similarity":sim if chosen else "",
        "verification_status":status,
        "n_openalex_authors":n_auth,
        "n_structured_institutions":len(inst),
        "n_corresponding_authors":correspondent,
        "openalex_source_url":chosen.get("id","") if chosen else "",
        "retrieved_utc":NOW
    })
    if not chosen:continue
    raw_work_json.append({"publication_id":pid,"verification_status":status,"work":chosen})
    for order,a in enumerate(chosen.get("authorships") or [],1):
        author=a.get("author") or {}
        aff=a.get("institutions") or []
        raw_aff=" | ".join(a.get("raw_affiliation_strings") or [])
        corresponding=a.get("is_corresponding")
        countries="|".join(a.get("countries") or [])
        for inst_obj in aff or [None]:
            inst_obj=inst_obj or {}
            iid=oa_id(inst_obj.get("id")) if inst_obj.get("id") else ""
            if iid.startswith("I"):unique_inst.add(iid)
            auth_rows.append({
                "publication_id":pid,
                "doi":doi,
                "openalex_work_id":chosen.get("id",""),
                "link_verification":status,
                "author_order":order,
                "author_name":author.get("display_name") or "",
                "author_openalex_id":author.get("id") or "",
                "author_orcid":author.get("orcid") or "",
                "corresponding_author": "YES" if corresponding is True else ("NO" if corresponding is False else "NOT_RECORDED"),
                "author_position":a.get("author_position") or "",
                "institution_openalex_id":inst_obj.get("id") or "",
                "institution_name_in_authorship":inst_obj.get("display_name") or "",
                "affiliation_kind":"STRUCTURED_INSTITUTION" if iid else "RAW_AFFILIATION_ONLY_OR_NONE",
                "raw_affiliation_strings":raw_aff,
                "author_country_codes_in_authorship":countries,
                "source_work_url":chosen.get("id",""),
                "retrieved_utc":NOW
            })

# Fetch all institutions *actually referenced in authorships*, including IDs outside the original 396.
inst_geo={}
def fetch_insts(batch):
    params={"filter":"openalex_id:"+"|".join(batch),"per_page":len(batch),"select":"id,display_name,ror,geo,ids"}
    try:return request_json(API+"/institutions?"+urllib.parse.urlencode(params)).get("results",[])
    except Exception as e:
        if len(batch)>1:
            half=len(batch)//2;return fetch_insts(batch[:half])+fetch_insts(batch[half:])
        errors.append({"type":"INSTITUTION_REQUEST_FAILED","key":batch[0],"error":str(e)})
        return []
for idx,batch in enumerate(batched(sorted(unique_inst),50),1):
    entries=fetch_insts(batch)
    for inst in entries:
        iid=oa_id(inst.get("id"))
        if iid in batch: inst_geo[iid]=inst
    print(f"Institution batch {idx}: {len(entries)}/{len(batch)}",flush=True)
    time.sleep(0.25)

institution_rows=[]
features=[]
for iid in sorted(unique_inst):
    inst=inst_geo.get(iid) or {}
    geo=inst.get("geo") or {}
    lat,lon=geo.get("latitude"),geo.get("longitude")
    valid=isinstance(lat,(int,float)) and isinstance(lon,(int,float)) and -90<=lat<=90 and -180<=lon<=180
    row={
        "institution_openalex_id":"https://openalex.org/"+iid,
        "display_name_verified":inst.get("display_name",""),
        "ror_id":inst.get("ror",""),
        "wikidata_id":(inst.get("ids") or {}).get("wikidata",""),
        "city":geo.get("city") or "",
        "region":geo.get("region") or "",
        "country_code":geo.get("country_code") or "",
        "latitude":lat if valid else "",
        "longitude":lon if valid else "",
        "precision":"OPENALEX_CITY_REFERENCE_NOT_CAMPUS" if valid else "NO_GEO",
        "lookup_status":"MATCHED_COORDINATES" if valid else ("MATCHED_NO_COORDINATES" if inst else "NOT_RETURNED"),
        "source_url":API+"/institutions/"+iid,
        "retrieved_utc":NOW
    }
    institution_rows.append(row)
    if valid:
        features.append({"type":"Feature","geometry":{"type":"Point","coordinates":[lon,lat]},
                         "properties":{k:v for k,v in row.items() if k not in ("latitude","longitude")}})

# Do not represent unverified title/DOI matches as established paper-institution links.
confirmed={p["publication_id"] for p in paper_rows if p["verification_status"]=="DOI_TITLE_CONFIRMED"}
confirmed_links=[x for x in auth_rows if x["publication_id"] in confirmed and x["institution_openalex_id"]]
unique_pairs={(a["publication_id"],a["institution_openalex_id"]) for a in confirmed_links}
all_ids={oa_id(x["institution_openalex_id"]) for x in institution_rows}
orig396=[x.strip() for x in (ROOT/"input"/"institution_ids_v08.txt").read_text().splitlines() if x.strip()]
novel=all_ids-set(orig396)
counts=Counter(p["verification_status"] for p in paper_rows)
audit={
    "retrieved_utc":NOW,"corpus_records":len(papers),"distinct_dois":len(doi_list),
    "records_with_doi":sum(bool(doi_norm(p["doi"])) for p in papers),
    "records_without_doi":sum(not doi_norm(p["doi"]) for p in papers),
    "doi_values_found":len(works),
    "doi_values_not_found":len(doi_list)-len(works),
    "publication_verification_counts":dict(counts),
    "authorship_affiliation_rows":len(auth_rows),
    "confirmed_paper_institution_pairs":len(unique_pairs),
    "confirmed_publications_with_structured_institutions":len({p for p,_ in unique_pairs}),
    "institutions_in_authorships":len(unique_inst),
    "new_institutions_outside_previous_396":len(novel),
    "institutions_resolved_with_coordinates":len(features),
    "batch_log":batch_log,"errors":errors,
    "method":"DOI match + title similarity check; one row per authorship x explicitly listed institution; no author-last-known-institution substitution.",
    "precision_note":"OpenAlex geo usually means city/locality reference, not campus or heritage case location."
}
write_csv(OUT/"works_1250_doi_authorship_audit.csv",list(paper_rows[0].keys()),paper_rows)
write_csv(OUT/"author_institution_links.csv",list(auth_rows[0].keys()) if auth_rows else ["publication_id"],auth_rows)
write_csv(OUT/"institutions_authorship_geo.csv",list(institution_rows[0].keys()) if institution_rows else ["institution_openalex_id"],institution_rows)
(OUT/"institutions_authorship_geo.geojson").write_text(json.dumps({"type":"FeatureCollection","features":features},ensure_ascii=False,indent=2),encoding="utf-8")
with (OUT/"openalex_work_authorships_raw.jsonl").open("w",encoding="utf-8") as f:
    for obj in raw_work_json:f.write(json.dumps(obj,ensure_ascii=False)+"\n")
(OUT/"audit_v10.json").write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding="utf-8")
print("AUDIT",json.dumps({k:v for k,v in audit.items() if k not in ("batch_log","errors")},ensure_ascii=False),flush=True)
if not works:sys.exit("No OpenAlex DOI matches; audit uploaded for troubleshooting.")
