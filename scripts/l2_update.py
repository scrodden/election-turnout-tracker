#!/usr/bin/env python3
"""Modeled profile of each state's early voters, from L2's public early-vote
dashboard (early.l2datamapping.com).

The dashboard reads public JSON files, one per state:
  https://www.l2datamapping.com/json/early/2026/US-DATA.json   (per-state timestamps)
  https://www.l2datamapping.com/json/early/2026/<ST>-DATA.json
Each has, for the state and every county / U.S. House district (and legislative
district), counts of all registered voters ("all"), ballots requested and
ballots returned, broken down by L2's voter-file fields: party, age range,
gender, ethnic group, urban / suburban / rural, and more. L2 combines state
voter files with its own modeled data (party where the state doesn't register
by party, ethnicity everywhere), so this is a separate, clearly labeled
"modeled" view, not mixed into the official counts. L2's dashboard: "Any news
organization, publication, or third party using or referencing these data must
attribute the source as L2" -- the page credits L2.

Writes data/<st>/l2.json (aggregates only): per unit {"ret": {field: [[label, n]]},
"reg": {...}, "n": [returned, requested, registered]}. A state's file is
fetched again only when L2's timestamp for it changes; the index is checked
about hourly.

Run:  python scripts/l2_update.py [--force] [ST ...]
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

BASE = "https://www.l2datamapping.com/json/early/2026/"
PAGE = "https://early.l2datamapping.com/?state=%s&type=returned"
STATES_PATH = os.path.join(ROOT, "assets", "states.json")

AGE_BANDS = (("18–29", ("18 to 19", "20 to 24", "25 to 29")), ("30–44", ("30 to 34", "35 to 39", "40 to 44")),
             ("45–64", ("45 to 49", "50 to 54", "55 to 64")), ("65+", ("65 and over",)))
ETHNIC = {"European": "White (European)", "Hispanic and Portuguese": "Hispanic", "Likely African-American": "Black",
          "East and South Asian": "Asian", "Other": "Other"}
GENDER = {"F": "Female", "M": "Male"}
FIELDS = (("party", "Parties_Description"), ("age", "Voters_AgeRange"), ("gender", "Voters_Gender"),
          ("ethnicity", "EthnicGroups_EthnicGroup1Desc"), ("area", "ConsumerData_RUS_Code"))


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower().replace("saint", "st").replace("county", "").replace("parish", ""))


def group(field, counts):
    """L2's categories for one field -> [[label, n], ...] in display order ('Unknown' last)."""
    counts = {k: int(v or 0) for k, v in (counts or {}).items()}
    if field == "age":
        out = [[label, sum(counts.get(k, 0) for k in keys)] for label, keys in AGE_BANDS]
        rest = sum(v for k, v in counts.items() if not any(k in keys for _, keys in AGE_BANDS))
    elif field == "gender":
        out = [[GENDER[k], counts.get(k, 0)] for k in ("F", "M")]
        rest = sum(v for k, v in counts.items() if k not in GENDER)
    elif field == "ethnicity":
        out = [[ETHNIC[k], counts.get(k, 0)] for k in ETHNIC]
        rest = sum(v for k, v in counts.items() if k not in ETHNIC)
    elif field == "area":
        out = [[k, counts.get(k, 0)] for k in ("Urban", "Suburban", "Rural")]
        rest = sum(v for k, v in counts.items() if k not in ("Urban", "Suburban", "Rural"))
    else:   # party: the three biggest named parties, the rest together
        named = sorted(((k, v) for k, v in counts.items() if k), key=lambda kv: -kv[1])
        main = [kv for kv in named if kv[0] in ("Democratic", "Republican")]
        others = [kv for kv in named if kv not in main]
        out = [[k, v] for k, v in main] + [[k, v] for k, v in others[:1]]
        rest = sum(v for _, v in others[1:]) + counts.get("", 0)
        if rest:
            out.append(["Other", rest])
        return [x for x in out if x[1]]
    if rest:
        out.append(["Unknown", rest])
    return [x for x in out if x[1] or x[0] != "Unknown"]


def unit(rec):
    def kind(k):
        return {name: group(name, (rec.get(k) or {}).get(src)) for name, src in FIELDS}
    n = [int((rec.get(k) or {}).get("_total") or 0) for k in ("early_returned", "early_requested", "all")]
    return {"ret": kind("early_returned"), "reg": kind("all"), "n": n}


def cd_number(key):
    """'AZ##09' / 'TX##2026 PROPOSED CONG DIST 15 (EST.)' -> '9' / '15'; anything else (CO's level holds special
    districts such as 'PROPOSED SABELL METRO 1') -> None."""
    k = key.split("##", 1)[-1].strip()
    m = re.fullmatch(r"0*(\d{1,2})", k) or re.search(r"CONG(?:RESSIONAL)?\s*DIST(?:RICT)?\s*0*(\d{1,2})(?!\d)", k, re.I)
    return str(int(m.group(1))) if m else None


def build(st, d, cfg_state):
    """-> the state's l2.json body, matching counties to the state's map by name."""
    geo = json.load(open(os.path.join(ROOT, cfg_state["geojson"]), encoding="utf-8"))
    gidx = {_norm(f["properties"]["name"]): f["properties"]["name"] for f in geo["features"]}
    meta = d.get("_metadata") or {}
    state_rec = next(iter((d.get("State") or {}).values()), None)
    if not state_rec:
        return None
    counties = {}
    town_or_cd = re.search(r"-(towns|cd)\.geojson$", cfg_state["geojson"])   # CT/MA/RI towns share county names
    for k, rec in ([] if town_or_cd else (d.get("County") or {}).items()):
        name = gidx.get(_norm(k.split("##", 1)[-1]))
        if name:
            counties[name] = unit(rec)
    cds = {}
    for k, rec in (d.get("US_Congressional_District") or {}).items():
        num = cd_number(k)
        if num is not None:
            cds[num] = unit(rec)
    # keep the districts only if they are exactly the state's (an at-large state's one district is renumbered)
    cd_geo = os.path.join(ROOT, "assets", "%s-cd.geojson" % st.lower())
    want = sorted(str(f["properties"]["district_number"]) for f in
                  json.load(open(cd_geo, encoding="utf-8"))["features"]) if os.path.exists(cd_geo) else []
    if len(want) == 1 and len(cds) == 1:
        cds = {want[0]: next(iter(cds.values()))}
    if sorted(cds) != want:
        if cds:
            print("l2: %s districts don't match the map (%s) -- left out" % (st, sorted(cds, key=int)[:8]), file=sys.stderr)
        cds = {}
    return {"state": st.lower(), "source": "L2 early-vote dashboard (voter-file data with L2's modeled fields)",
            "url": PAGE % st.upper(), "as_of": meta.get("early_date", ""), "timestamp": meta.get("timestamp", ""),
            "fields": [[name, {"party": "Party", "age": "Age", "gender": "Gender", "ethnicity": "Ethnicity",
                               "area": "Area"}[name]] for name, _ in FIELDS],
            "statewide": unit(state_rec), "counties": counties, "cd": cds}


def main():
    force = "--force" in sys.argv
    only = {a.upper() for a in sys.argv[1:] if not a.startswith("--")}
    if not force and not only and C.checked_recently(os.path.join(ROOT, "data"), "l2", minutes=55):
        print("l2: checked under an hour ago.")
        return 0
    states = {s["code"].upper(): s for s in json.load(open(STATES_PATH, encoding="utf-8"))["states"]}
    try:
        idx = json.loads(C.http_get(BASE + "US-DATA.json", no_cache=True, retries=2, timeout=120))
    except Exception as e:  # noqa: BLE001
        print("l2: index unavailable (%s)" % str(e)[:100], file=sys.stderr)
        return 0
    stamps = ((idx.get("_metadata") or {}).get("states")) or {}
    done = []
    for st, m in sorted(stamps.items()):
        if st not in states or (only and st not in only):
            continue
        out = os.path.join(ROOT, "data", st.lower(), "l2.json")
        prev = (json.load(open(out, encoding="utf-8")) if os.path.exists(out) else {}) or {}
        if not force and prev.get("timestamp") == m.get("timestamp"):
            continue
        try:
            d = json.loads(C.http_get(BASE + st + "-DATA.json", no_cache=True, retries=2, timeout=300))
            body = build(st, d, states[st])
        except Exception as e:  # noqa: BLE001
            print("l2: %s unavailable (%s)" % (st, str(e)[:100]), file=sys.stderr)
            continue
        if not body or not body["statewide"]["n"][0]:
            continue
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(body, f, separators=(",", ":"), ensure_ascii=False)
        done.append("%s %s (%d counties, %d districts)" % (st, body["as_of"], len(body["counties"]), len(body["cd"])))
    print("l2: updated %d state(s)%s" % (len(done), (": " + "; ".join(done[:12])) if done else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
