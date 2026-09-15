#!/usr/bin/env python3
"""Fetch Florida turnout by county AND precinct, by party and voting method.

Primary source: VR Systems "Turnout Quick View" (TQV) per-county static feeds
on S3 (the same data the county Supervisors of Elections publish live). Each
county exposes:
  data/FL/<CODE>/index.json           -> [electionId, ...]
  data/FL/<CODE>/<electionId>/data.json  -> Summary + Turnout (party/precinct)
The right election is the one whose Summary.FvrsElectionNumber matches ours.

TQV gives ballots *cast* by method (Mail / Early Voting / Election Day /
Provisional), party registration, registered-voter counts (real turnout %),
precinct-level detail, and a genuine per-county update timestamp.

Secondary source: FL Dept of State statewide consolidated file, used for
"mail ballots outstanding" (which TQV does not report) and as a per-county
fallback if a TQV feed is unavailable.

Outputs (repo-relative):
  data/fl/latest.json            county + statewide snapshot (derived shares/margins/turnout)
  data/fl/precincts/<CODE>.json  per-county precinct turnout by method (written when changed)
  data/fl/history.jsonl          one compact line per change
  data/fl/_tqv_ids.json          cached county -> electionId map (skips index lookups)

Run:  python scripts/fl_update.py        (writes only when data changed)
      python scripts/fl_update.py --force
Prints CHANGED / NOCHANGE for the workflow.
"""
import os
import re
import sys
import json
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import common as C  # noqa: E402

STATE = "fl"
CONFIG_PATH = os.path.join(ROOT, "config", "fl.json")
COUNTIES_PATH = os.path.join(ROOT, "config", "fl_counties.json")
DATA_DIR = os.path.join(ROOT, "data", STATE)
PRECINCT_DIR = os.path.join(DATA_DIR, "precincts")
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
IDCACHE_PATH = os.path.join(DATA_DIR, "_tqv_ids.json")
PRECINCTS_ALL_PATH = os.path.join(DATA_DIR, "precincts_all.json")

VOTED_METHODS = ["mail_voted", "early_voted", "election_day"]  # count toward "cast"
ALL_METHODS = ["mail_voted", "early_voted", "election_day", "provisional", "mail_provided"]
MAX_WORKERS = 10


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------
# TQV (primary)
# --------------------------------------------------------------------------
def tqv_index_url(cfg, code):
    return cfg["tqv"]["base"] + code + "/" + cfg["tqv"]["index_file"]


def tqv_data_url(cfg, code, eid):
    return cfg["tqv"]["base"] + code + "/" + str(eid) + "/" + cfg["tqv"]["data_file"]


def fetch_tqv_county(cfg, county, id_cache):
    """Return (parsed_dict_or_None, resolved_eid_or_None, note)."""
    code = county["code"]
    target = cfg["tqv"]["fvrs_election_number"]

    def try_eid(eid):
        try:
            raw = C.http_get(tqv_data_url(cfg, code, eid), no_cache=True, retries=2)
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            return None
        summ = data.get("Summary", {})
        if summ.get("FvrsElectionNumber") == target:
            return data
        return None

    # 1) Try cached election id first (skips the index lookup).
    cached = id_cache.get(code)
    if cached is not None:
        data = try_eid(cached)
        if data is not None:
            return parse_tqv(cfg, county, data), cached, "cache"

    # 2) Resolve from the county's election index.
    try:
        ids = json.loads(C.http_get(tqv_index_url(cfg, code), no_cache=True, retries=2))
    except Exception as e:  # noqa: BLE001
        return None, None, "index-fail:%s" % str(e)[:40]
    for eid in ids:
        if eid == cached:
            continue
        data = try_eid(eid)
        if data is not None:
            return parse_tqv(cfg, county, data), eid, "resolved"
    return None, None, "no-matching-election"


def parse_tqv(cfg, county, data):
    """Turn one county's TQV data.json into county-method party counts +
    precinct rows."""
    party_map = cfg["tqv"]["party_map"]
    method_map = cfg["tqv"]["method_map"]
    summ = data.get("Summary", {})
    turnout = data.get("Turnout", {}) or {}

    # county-level: party x method -> counts
    methods = {}  # method_key -> {rep,dem,oth,npa}
    for party_code, by_method in (turnout.get("PartyType") or {}).items():
        tgt_party = party_map.get(str(party_code).upper(), "oth")
        for m_label, n in (by_method or {}).items():
            mkey = method_map.get(m_label)
            if not mkey:
                continue
            slot = methods.setdefault(mkey, {"rep": 0, "dem": 0, "oth": 0, "npa": 0})
            slot[tgt_party] += int(n or 0)

    # precinct-level: method totals + eligible voters
    precincts = []
    for pkey, pdata in (turnout.get("PrecinctType") or {}).items():
        row = {"precinct": str(pkey).strip(),
               "eligible": int(pdata.get("EligibleVoters") or 0)}
        cast = 0
        for m_label, n in (pdata.get("BallotTypeTotals") or {}).items():
            mkey = method_map.get(m_label)
            if not mkey:
                continue
            row[mkey] = int(n or 0)
            if mkey in VOTED_METHODS:
                cast += int(n or 0)
        row["cast"] = cast
        row["turnout_pct"] = C.pct(cast, row["eligible"])
        precincts.append(row)
    precincts.sort(key=lambda r: r["precinct"])

    raw_ts = summ.get("LastUpdatedTime", "")
    iso_ts = re.sub(r"\.\d+", "", raw_ts).replace("+00:00", "Z") if raw_ts else ""
    return {
        "registered": int(summ.get("TotalRegisteredVoters") or 0),
        "last_updated": iso_ts,
        "methods": methods,
        "precincts": precincts,
    }


# --------------------------------------------------------------------------
# DOS (secondary: mail-outstanding + fallback)
# --------------------------------------------------------------------------
FILE_URL_RE = re.compile(
    r"https://electionfiles\.floridados\.gov/countyballotreportfiles/[^\"'\s<>]+\.txt",
    re.IGNORECASE)


def fetch_dos(cfg):
    """Return {county_name: {method_key: {rep,dem,oth,npa}}} from the DOS
    statewide files. Best-effort; returns {} on failure."""
    dos = cfg["dos"]
    urls = set(dos["file_base"] + f for f in dos["files"].values())
    try:
        html = C.http_get(dos["stats_page"], no_cache=True, retries=2)
        for m in FILE_URL_RE.findall(html):
            if cfg["election"]["number"] in m:
                urls.add(m)
    except Exception:  # noqa: BLE001
        pass
    out = {}
    smap = dos["stat_type_map"]
    for url in sorted(urls):
        try:
            text = C.http_get(url, no_cache=True, retries=2)
        except Exception:  # noqa: BLE001
            continue
        for line in text.splitlines():
            parts = line.split("\t")
            if len(parts) < 10 or not parts[0].strip().isdigit():
                continue
            county = parts[3].strip()
            mkey = smap.get(parts[4].strip())
            if not mkey or county.lower() == "state totals":
                continue
            out.setdefault(county, {})[mkey] = {
                "rep": C.parse_number(parts[5]), "dem": C.parse_number(parts[6]),
                "oth": C.parse_number(parts[7]), "npa": C.parse_number(parts[8]),
            }
    return out


# --------------------------------------------------------------------------
# Broward ENR (registered voters + overall turnout; partisan stays DOS)
# --------------------------------------------------------------------------
ENR_ANCHOR_RE = re.compile(
    r'<a[^>]+href="[^"]*?/ENR/browardflenr/(\d+)/[^"]*"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL)


def _xml_tag(text, tag):
    m = re.search(r"<%s>(.*?)</%s>" % (tag, tag), text, re.IGNORECASE | re.DOTALL)
    return m.group(1).strip() if m else None


def fetch_broward_enr(cfg):
    """Return {registered, cast_total, turnout, is_general, id} or None.

    Picks a COUNTYWIDE election for the registered-voter denominator: prefers
    the 2026 General, then the most recent general/primary (municipal/special
    elections cover only part of the county and would undercount registration).
    """
    b = cfg.get("broward_enr")
    if not b:
        return None
    try:
        page = C.http_get(b["results_page"], no_cache=True, retries=2)
    except Exception:  # noqa: BLE001
        return None
    ids = []
    for m in ENR_ANCHOR_RE.finditer(page):
        label = re.sub(r"<[^>]+>", " ", m.group(2))
        ids.append((m.group(1), re.sub(r"\s+", " ", label).strip().lower()))
    if not ids:
        return None
    prefer = (b.get("prefer_label") or "").lower()
    chosen, is_general = None, False
    for eid, label in ids:                       # 1) exact preferred (2026 general)
        if prefer and prefer in label:
            chosen, is_general = eid, True
            break
    if chosen is None:                            # 2) most recent countywide race
        countywide = [eid for eid, label in ids
                      if "general" in label or "primary" in label]
        if countywide:
            chosen = max(countywide, key=int)
    if chosen is None:                            # 3) last resort: latest of anything
        chosen = max((eid for eid, _ in ids), key=int)
    try:
        xml = C.http_get(b["enr_base"] + chosen + "/summary_" + chosen + ".xml",
                         no_cache=True, retries=2)
    except Exception:  # noqa: BLE001
        return None
    reg = C.parse_number(_xml_tag(xml, "RegisteredVoters"))
    cast = C.parse_number(_xml_tag(xml, "TotalBallotsCast"))
    turnout = _xml_tag(xml, "VoterTurnout")
    if not reg:
        return None
    return {"id": chosen, "registered": reg, "cast_total": cast,
            "turnout": turnout, "is_general": is_general}


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------
def precinct_key(split_id):
    """Aggregate a TQV precinct-split id (e.g. '14.42') to the precinct level
    ('14') used to join with precinct boundary polygons. Whole precinct ids
    (no dot) pass through unchanged."""
    s = str(split_id).strip()
    return s.split(".")[0] if "." in s else s


def aggregate_precincts(rows):
    """Roll split-level TQV rows up to precinct level for the map.
    Returns {precinct_id: [eligible, cast, mail, early, election_day, provisional]}."""
    agg = {}
    for r in rows:
        pid = precinct_key(r["precinct"])
        a = agg.setdefault(pid, [0, 0, 0, 0, 0, 0])
        a[0] += r.get("eligible", 0)
        a[1] += r.get("cast", 0)
        a[2] += r.get("mail_voted", 0)
        a[3] += r.get("early_voted", 0)
        a[4] += r.get("election_day", 0)
        a[5] += r.get("provisional", 0)
    return agg


def block_from_counts(counts, compiled="", compiled_iso=""):
    c = counts or {}
    return C.party_block(c.get("rep", 0), c.get("dem", 0), c.get("oth", 0),
                         c.get("npa", 0), compiled, compiled_iso)


def build_county_entity(county, tqv, dos_methods):
    """Combine TQV (cast methods) + DOS (mail_provided) into one county entity."""
    ent = {"code": county["code"], "fips": county["fips"],
           "tqv_url": "https://tqv.vrswebapps.com/?state=FL&county=" + county["code"].lower()}
    iso = (tqv or {}).get("last_updated", "")
    registered = (tqv or {}).get("registered", 0)

    if tqv:
        for mkey in ["mail_voted", "early_voted", "election_day", "provisional"]:
            if mkey in tqv["methods"]:
                ent[mkey] = block_from_counts(tqv["methods"][mkey], iso, iso)
        ent["source"] = "tqv"
    elif dos_methods:
        # fallback: use DOS cast methods for this county
        for mkey in ["mail_voted", "early_voted", "election_day"]:
            if mkey in dos_methods:
                ent[mkey] = block_from_counts(dos_methods[mkey])
        ent["source"] = "dos-fallback"
    else:
        ent["source"] = "none"

    # mail outstanding always comes from DOS (TQV does not report it)
    if dos_methods and "mail_provided" in dos_methods:
        ent["mail_provided"] = block_from_counts(dos_methods["mail_provided"])

    voted = [ent[m] for m in VOTED_METHODS if ent.get(m)]
    ent["cast"] = C.add_blocks(*voted) if voted else C.party_block(0, 0, 0, 0, iso, iso)
    ent["registered"] = registered
    ent["last_updated"] = iso
    ent["turnout_pct"] = C.pct(ent["cast"]["total"], registered)
    return ent


def write_if_changed(path, obj):
    """Write compact JSON only when content differs. Returns True if written."""
    blob = json.dumps(obj, separators=(",", ":"))
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                if f.read() == blob:
                    return False
        except OSError:
            pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(blob)
    return True


def existing_hash():
    if not os.path.exists(LATEST_PATH):
        return None
    try:
        return load_json(LATEST_PATH).get("data_hash")
    except (ValueError, OSError):
        return None


def append_history(snap):
    def compact(ent):
        out = {}
        for m in ALL_METHODS + ["cast"]:
            b = ent.get(m)
            if b and b.get("total"):
                out[m] = [b["rep"], b["dem"], b["oth"], b["npa"], b["total"]]
        return out
    rec = {
        "generated_at": snap["generated_at"], "compiled": snap["source_compiled"],
        "data_hash": snap["data_hash"],
        "statewide": compact(snap["statewide"]),
        "registered": snap["statewide"].get("registered"),
        "counties": {n: compact(e) for n, e in snap["counties"].items() if compact(e)},
    }
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, "rb") as f:
                try:
                    f.seek(-4096, os.SEEK_END)
                except OSError:
                    f.seek(0)
                tail = f.read().decode("utf-8", "replace").strip().splitlines()
            if tail and json.loads(tail[-1]).get("data_hash") == rec["data_hash"]:
                return False
        except (ValueError, OSError):
            pass
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    return True


def main():
    force = "--force" in sys.argv
    cfg = load_json(CONFIG_PATH)
    counties = load_json(COUNTIES_PATH)["counties"]
    id_cache = {}
    if os.path.exists(IDCACHE_PATH):
        try:
            id_cache = load_json(IDCACHE_PATH)
        except (ValueError, OSError):
            id_cache = {}

    # 1) DOS (single, cheap) for mail-outstanding + fallback
    print("Fetching DOS statewide files (mail outstanding + fallback)...")
    dos = fetch_dos(cfg)
    print("  DOS counties with data: %d" % len(dos))

    # 2) TQV per county, threaded
    print("Fetching TQV feeds for %d counties..." % len(counties))
    results = {}

    def work(county):
        return county["name"], fetch_tqv_county(cfg, county, id_cache)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for name, res in ex.map(work, counties):
            results[name] = res

    new_id_cache = dict(id_cache)
    ok = fail = 0
    for county in counties:
        tqv, eid, note = results[county["name"]]
        if tqv is not None:
            ok += 1
            if eid is not None:
                new_id_cache[county["code"]] = eid
        else:
            fail += 1
            print("  ! %s (%s): %s" % (county["name"], county["code"], note))
    print("  TQV ok=%d fail=%d" % (ok, fail))
    if ok == 0:
        print("ERROR: no TQV feeds returned; aborting.", file=sys.stderr)
        return 2

    # 3) assemble snapshot + precinct files
    counties_out = {}
    precinct_payloads = {}
    precincts_all = {}   # code -> {precinct: [eligible,cast,mail,early,ed,prov]} for the map
    max_iso = ""
    for county in counties:
        name = county["name"]
        tqv, _eid, _note = results[name]
        ent = build_county_entity(county, tqv, dos.get(name))
        counties_out[name] = ent
        if ent.get("last_updated", "") > max_iso:
            max_iso = ent["last_updated"]
        if tqv and tqv["precincts"]:
            precinct_payloads[county["code"]] = {
                "county": name, "code": county["code"], "fips": county["fips"],
                "election": cfg["election"], "registered": tqv["registered"],
                "last_updated": tqv["last_updated"], "precincts": tqv["precincts"],
            }
            agg = aggregate_precincts(tqv["precincts"])
            if agg:
                precincts_all[county["code"]] = agg

    # Broward: fill registered voters / turnout from ENR while its TQV general
    # feed isn't publishing (partisan cast still comes from DOS).
    bw = counties_out.get("Broward")
    if bw and bw.get("source") != "tqv" and not bw.get("registered"):
        enr = fetch_broward_enr(cfg)
        if enr:
            bw["registered"] = enr["registered"]
            bw["turnout_pct"] = C.pct(bw["cast"]["total"], enr["registered"])
            bw["registered_source"] = "enr"
            bw["enr"] = {"id": enr["id"], "is_general": enr["is_general"]}
            print("  Broward ENR: registered=%d (election %s, general=%s)"
                  % (enr["registered"], enr["id"], enr["is_general"]))

    # statewide = sum of counties
    statewide = {}
    for mkey in ALL_METHODS:
        blocks = [counties_out[c].get(mkey) for c in counties_out if counties_out[c].get(mkey)]
        if blocks:
            statewide[mkey] = C.add_blocks(*blocks)
    voted = [statewide[m] for m in VOTED_METHODS if statewide.get(m)]
    statewide["cast"] = C.add_blocks(*voted) if voted else C.party_block(0, 0, 0, 0)
    statewide["registered"] = sum(counties_out[c].get("registered", 0) for c in counties_out)
    statewide["turnout_pct"] = C.pct(statewide["cast"]["total"], statewide["registered"])

    methods_present = sorted({m for c in counties_out.values() for m in ALL_METHODS if c.get(m)})

    snap = {
        "state": STATE, "state_name": cfg["state_name"], "election": cfg["election"],
        "sources": {"primary": "VR Systems Turnout Quick View (county feeds)",
                    "secondary": "FL Division of Elections (mail outstanding / fallback)"},
        "source_compiled": (max_iso or "").replace("T", " ").replace("Z", " UTC"),
        "source_compiled_iso": max_iso,
        "methods_present": methods_present,
        "method_labels": cfg.get("method_labels", {}),
        "statewide": statewide, "counties": counties_out,
    }
    snap["data_hash"] = C.data_hash(snap)
    snap["generated_at"] = C.utc_now_iso()

    prev = existing_hash()
    changed = force or (snap["data_hash"] != prev)

    # precinct files + id cache write regardless (only when their content changed)
    os.makedirs(PRECINCT_DIR, exist_ok=True)
    pchanged = 0
    for code, payload in precinct_payloads.items():
        if write_if_changed(os.path.join(PRECINCT_DIR, code + ".json"), payload):
            pchanged += 1
    write_if_changed(IDCACHE_PATH, new_id_cache)
    write_if_changed(PRECINCTS_ALL_PATH, {
        "generated_at": snap.get("generated_at", C.utc_now_iso()),
        "election": cfg["election"],
        "fields": ["eligible", "cast", "mail_voted", "early_voted", "election_day", "provisional"],
        "counties": precincts_all,
    })

    if changed:
        with open(LATEST_PATH, "w", encoding="utf-8") as f:
            json.dump(snap, f, separators=(",", ":"))
        added = append_history(snap)
        cast = statewide["cast"]
        print("CHANGED  updated=%s  cast=%s (R%s D%s NPA%s) margin=%s turnout=%s%%  precincts changed=%d  history+=%s"
              % (snap["source_compiled"], cast["total"], cast["rep"], cast["dem"],
                 cast["npa"], cast["margin"], statewide["turnout_pct"], pchanged, added))
    else:
        print("NOCHANGE  updated=%s (hash %s)  precincts changed=%d"
              % (snap["source_compiled"], (prev or "")[:12], pchanged))
    return 0


if __name__ == "__main__":
    sys.exit(main())
