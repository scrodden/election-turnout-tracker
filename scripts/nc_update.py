#!/usr/bin/env python3
"""Fetch North Carolina turnout by county and party from the NCSBE voter-level
absentee file (mail + one-stop early voting). Writes data/nc/latest.json in the
same schema as Florida so the front end reuses everything.

Source: https://s3.amazonaws.com/dl.ncsbe.gov/ENRS/2026_11_03/absentee_20261103.zip
Each row = one RETURNED absentee/early ballot (NC law: request info isn't public
until returned), with county_desc, voter_party_code, ballot_req_type
(MAIL / EARLY VOTING), ballot_rtn_status, precinct_desc, etc. We aggregate to
county x party x method, and the same by congressional district
(cong_dist_desc) into data/nc/districts.json for the Districts map. There is
no "mail outstanding" metric for NC. The file is streamed row by row, and the
download is skipped while its S3 ETag is unchanged.

Run:  python scripts/nc_update.py [--force]
"""
import os
import re
import sys
import io
import csv
import json
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "nc"
CONFIG_PATH = os.path.join(ROOT, "config", "nc.json")
COUNTIES_PATH = os.path.join(ROOT, "config", "nc_counties.json")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
DISTRICTS_PATH = os.path.join(DATA_DIR, "districts.json")
CD_GEO_PATH = os.path.join(ROOT, "assets", "nc-cd.geojson")
VOTED_METHODS = ["mail_voted", "early_voted"]
_TODAY = __import__("datetime").datetime.utcnow().strftime("%Y-%m-%d")


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def parse_date(s):
    s = (s or "").strip()
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", s)
    return "%s-%s-%s" % (m.group(3), m.group(1), m.group(2)) if m else ""


AGE_GROUPS = [("18-25", 18, 25), ("26-40", 26, 40), ("41-65", 41, 65), ("Over 65", 66, 200)]
GENDER = {"F": "Female", "M": "Male"}
RACE = {"WHITE": "White", "BLACK or AFRICAN AMERICAN": "Black", "ASIAN": "Asian",
        "INDIAN AMERICAN or ALASKA NATIVE": "American Indian / Alaska Native",
        "NATIVE HAWAIIAN or PACIFIC ISLANDER": "Native Hawaiian / Pacific Islander",
        "TWO or MORE RACES": "Two or more races", "OTHER": "Other"}
ETHNICITY = {"HISPANIC or LATINO": "Hispanic or Latino", "NOT HISPANIC or NOT LATINO": "Not Hispanic or Latino"}


class Demo:
    """Age / gender / race / ethnicity of returned ballots, as self-reported on
    the voter registration (NCSBE file columns age, gender, race, ethnicity).
    Accumulates row by row so the (eventually multi-million-row) file is never
    held in memory."""

    def __init__(self):
        from collections import Counter
        self.n = 0
        self.age, self.gen, self.race, self.eth = Counter(), Counter(), Counter(), Counter()

    def add(self, r):
        self.n += 1
        a = (r.get("age") or "").strip()
        label = "Unknown"
        if a.isdigit():
            label = next((g for g, lo, hi in AGE_GROUPS if lo <= int(a) <= hi), "Unknown")
        self.age[label] += 1
        self.gen[GENDER.get((r.get("gender") or "").strip().upper(), "Unknown")] += 1
        self.race[RACE.get((r.get("race") or "").strip(), "Not designated")] += 1
        self.eth[ETHNICITY.get((r.get("ethnicity") or "").strip(), "Not designated")] += 1

    def result(self):
        def ordered(counter, order):
            keys = [k for k in order if counter.get(k)] + sorted(k for k in counter if k not in order)
            return [{"label": k, "count": counter[k]} for k in keys]
        return {"age": ordered(self.age, [g for g, _, _ in AGE_GROUPS] + ["Unknown"]),
                "gender": ordered(self.gen, ["Female", "Male", "Unknown"]),
                "race": ordered(self.race, list(RACE.values()) + ["Not designated"]),
                "ethnicity": ordered(self.eth, list(ETHNICITY.values()) + ["Not designated"])}


def head_etag(url):
    """ETag of the S3 file (changes whenever NCSBE republishes it), or ''."""
    import urllib.request
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": C.USER_AGENT})
        with urllib.request.urlopen(req, timeout=30, context=C._SSL_CTX) as r:
            return (r.headers.get("ETag") or "").strip('"')
    except Exception:  # noqa: BLE001
        return ""


def unit_entity(methods, fips):
    ent = {"fips": fips}
    for mkey, s in methods.items():
        ent[mkey] = C.party_block(s["rep"], s["dem"], s["oth"], s["npa"])
    voted = [ent[m] for m in VOTED_METHODS if ent.get(m)]
    ent["cast"] = C.add_blocks(*voted) if voted else C.party_block(0, 0, 0, 0)
    return ent


def write_districts(cfg, agg_cd, demo_cd, bad_cd, statewide, max_rtn, force):
    """data/nc/districts.json: the same ballots by each voter's U.S. House
    district (file column cong_dist_desc), for the Counties | Districts map."""
    geo = load(CD_GEO_PATH)
    gidx = {f["properties"]["district_number"]: f["properties"] for f in geo["features"]}
    districts = {}
    for num in sorted(gidx):
        ent = unit_entity(agg_cd.get(num, {}), gidx[num]["fips"])
        if num in demo_cd:
            ent["demographics"] = demo_cd[num].result()
        districts[gidx[num]["name"]] = ent
    unassigned = statewide["cast"]["total"] - sum(d["cast"]["total"] for d in districts.values())
    unknown = sorted(n for n in agg_cd if n not in gidx)
    body = {
        "state": STATE, "election": cfg["election"], "unit_label": "District", "unit_label_plural": "Districts",
        "partisan": True,
        "source": "NCSBE voter-level absentee file (returned ballots; mail + early), by each voter's "
                  "congressional district",
        "methods_present": sorted({m for d in districts.values() for m in VOTED_METHODS if d.get(m)}),
        "method_labels": cfg.get("method_labels", {}),
        # districts in the file but not on the map would mean the boundaries are out of date
        "coverage": {"ok": max(0, len(gidx) - len(unknown)), "total": len(gidx),
                     "unmatched": [str(n) for n in unknown] + sorted(bad_cd)[:20]},
        "as_of": max_rtn, "statewide": statewide, "counties": districts,
    }
    if unassigned:
        body["map_note"] = ("%s returned ballots in the NCSBE file carry no recognizable congressional district "
                            "and are left off the district map." % format(unassigned, ","))
    h = C.data_hash(body)
    prev = {}
    if os.path.exists(DISTRICTS_PATH):
        try:
            prev = load(DISTRICTS_PATH)
        except (ValueError, OSError):
            prev = {}
    if not force and h == prev.get("data_hash"):
        return False
    out = dict(body, source_compiled=max_rtn, source_compiled_iso=(max_rtn + "T00:00:00") if max_rtn else "",
               data_hash=h, generated_at=C.utc_now_iso())
    with open(DISTRICTS_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    print("  districts: %d written (%d ballots without a district)" % (len(districts), unassigned))
    return True


def main():
    force = "--force" in sys.argv
    cfg = load(CONFIG_PATH)
    counties = load(COUNTIES_PATH)["counties"]
    by_code = {c["code"]: c for c in counties}   # code = UPPERCASE county name
    party_map = cfg["party_map"]
    method_map = cfg["method_map"]
    url = cfg["source"]["absentee_zip"]

    prev_snap = {}
    if os.path.exists(LATEST_PATH):
        try:
            prev_snap = load(LATEST_PATH)
        except (ValueError, OSError):
            prev_snap = {}
    # NCSBE republishes the file about daily; skip the download when it hasn't changed
    etag = head_etag(url)
    if not force and etag and etag == prev_snap.get("source_etag") and os.path.exists(DISTRICTS_PATH):
        print("NOCHANGE  (NCSBE file unchanged since %s)" % prev_snap.get("source_compiled"))
        return 0

    print("Fetching NCSBE absentee file...")
    raw = C.http_get(url, binary=True, no_cache=True, timeout=300)
    z = zipfile.ZipFile(io.BytesIO(raw))
    stream = io.TextIOWrapper(z.open(z.namelist()[0]), encoding="latin-1", newline="")

    # aggregate county (and congressional district) -> method -> party counts
    agg, agg_cd = {}, {}
    demo_all, demo_cty, demo_cd = Demo(), {}, {}
    bad_cd = set()
    max_rtn = ""
    n_rows = 0
    for r in csv.DictReader(stream):
        n_rows += 1
        code = (r.get("county_desc") or "").strip().upper()
        mkey = method_map.get((r.get("ballot_req_type") or "").strip().upper())
        if not code or not mkey or code not in by_code:
            continue
        tgt = party_map.get((r.get("voter_party_code") or "").strip().upper(), "oth")
        slot = agg.setdefault(code, {}).setdefault(mkey, {"rep": 0, "dem": 0, "oth": 0, "npa": 0})
        slot[tgt] += 1
        demo_all.add(r)
        demo_cty.setdefault(code, Demo()).add(r)
        m = re.search(r"(\d+)\s*$", r.get("cong_dist_desc") or "")
        if m:
            num = int(m.group(1))
            agg_cd.setdefault(num, {}).setdefault(mkey, {"rep": 0, "dem": 0, "oth": 0, "npa": 0})[tgt] += 1
            demo_cd.setdefault(num, Demo()).add(r)
        else:
            bad_cd.add((r.get("cong_dist_desc") or "").strip())
        d = parse_date(r.get("ballot_rtn_dt"))
        if d and d <= _TODAY and d > max_rtn:  # ignore stray future-dated records
            max_rtn = d
    print("  %d ballot records" % n_rows)

    # build snapshot
    counties_out = {}
    for c in counties:
        ent = unit_entity(agg.get(c["code"], {}), c["fips"])
        if c["code"] in demo_cty:
            ent["demographics"] = demo_cty[c["code"]].result()
        counties_out[c["name"]] = ent

    statewide = {}
    for mkey in VOTED_METHODS:
        blocks = [counties_out[n][mkey] for n in counties_out if counties_out[n].get(mkey)]
        if blocks:
            statewide[mkey] = C.add_blocks(*blocks)
    voted = [statewide[m] for m in VOTED_METHODS if statewide.get(m)]
    statewide["cast"] = C.add_blocks(*voted) if voted else C.party_block(0, 0, 0, 0)

    methods_present = sorted({m for c in counties_out.values() for m in VOTED_METHODS if c.get(m)})
    snap = {
        "state": STATE, "state_name": cfg["state_name"], "election": cfg["election"],
        "source": {"primary": "NCSBE voter-level absentee file (returned ballots; mail + early)"},
        "source_compiled": max_rtn, "source_compiled_iso": (max_rtn + "T00:00:00") if max_rtn else "",
        "methods_present": methods_present, "method_labels": cfg.get("method_labels", {}),
        "statewide": statewide, "counties": counties_out,
    }
    if demo_all.n:
        snap["demographics"] = dict(demo_all.result(), note=(
            "Age, gender, race and ethnicity as self-reported on each voter's registration record (NCSBE absentee "
            "file). Many registrants leave race or ethnicity blank, shown as 'Not designated'."))
    snap["data_hash"] = C.data_hash(snap)
    snap["generated_at"] = C.utc_now_iso()

    os.makedirs(DATA_DIR, exist_ok=True)
    write_districts(cfg, agg_cd, demo_cd, bad_cd, statewide, max_rtn, force)
    prev = prev_snap.get("data_hash")
    changed = force or (snap["data_hash"] != prev)
    if not changed:
        snap["generated_at"] = prev_snap.get("generated_at", snap["generated_at"])
    snap["source_etag"] = etag   # outside the data hash: a republished-but-identical file isn't a change
    if changed or etag != prev_snap.get("source_etag"):
        with open(LATEST_PATH, "w", encoding="utf-8") as f:
            json.dump(snap, f, separators=(",", ":"))
    if changed:
        rec = {"generated_at": snap["generated_at"], "compiled": max_rtn, "data_hash": snap["data_hash"],
               "statewide": {m: [b["rep"], b["dem"], b["oth"], b["npa"], b["total"]]
                             for m, b in statewide.items() if b.get("total")}}
        append = True
        if os.path.exists(HISTORY_PATH):
            try:
                with open(HISTORY_PATH, "rb") as f:
                    try:
                        f.seek(-2048, os.SEEK_END)
                    except OSError:
                        f.seek(0)
                    tail = f.read().decode("utf-8", "replace").strip().splitlines()
                if tail and json.loads(tail[-1]).get("data_hash") == rec["data_hash"]:
                    append = False
            except (ValueError, OSError):
                pass
        if append:
            with open(HISTORY_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        cast = statewide["cast"]
        print("CHANGED  through=%s  cast=%s (R%s D%s NPA%s) margin=%s"
              % (max_rtn, cast["total"], cast["rep"], cast["dem"], cast["npa"], cast["margin"]))
    else:
        print("NOCHANGE  through=%s (hash %s)" % (max_rtn, (prev or "")[:12]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
