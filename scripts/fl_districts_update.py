#!/usr/bin/env python3
"""Florida vote-by-mail by congressional, Florida House and Florida Senate
district -> data/fl/districts_{cd,sh,ss}.json (the Florida page's district
maps).

Source: the district tables published by 305 Data Girl's "Florida 2026
General" tracker (config/fl.json districts_305.base), built from the state's
county voter-level vote-by-mail activity files. Those files are restricted by
s. 101.62 F.S. to parties, candidates, political committees and canvassing
boards, so we can't build district counts ourselves; this republishes the
tracker's district totals with credit. Per district: sent (provided +
returned), returned, and returned by party. Their counting: voter-file status
P = provided / not yet returned, V = voted / returned. Records without a
district assignment are left out of district tables (still in their
statewide totals). Figures are cumulative through the close of the prior
day, not live like the county view, and coverage can be short a county; both
are stated on the map.

District boundaries: the enacted plans hosted by the Florida Senate's
redistricting office (scripts/build_plan_geo.py): congressional EOGPCRP2026
(2026), House H000H8013 and Senate S027S8058 (2022).

Run:  python scripts/fl_districts_update.py [--force]
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

CONFIG_PATH = os.path.join(ROOT, "config", "fl.json")
DATA_DIR = os.path.join(ROOT, "data", "fl")
KINDS = {  # their file key -> (our suffix, map file, name prefix, labels)
    "congressional": ("cd", "fl-cd", "CD", "Congressional District", "Congressional Districts"),
    "house": ("sh", "fl-sh", "HD", "Florida House District", "Florida House Districts"),
    "senate": ("ss", "fl-ss", "SD", "Florida Senate District", "Florida Senate Districts"),
}


def load(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def close_of_day(mdy):
    """'09/30/2026' -> UTC ISO time of 11:59 PM Eastern that day (the data is
    cumulative through the close of that day), so the page's 'Data updated'
    shows the data's own cut-off rather than when we fetched it."""
    from datetime import datetime, timedelta
    try:
        d = datetime.strptime(mdy, "%m/%d/%Y")
    except (TypeError, ValueError):
        return ""
    offset = 5 if (d.month, d.day) >= (11, 1) or d.month in (12, 1, 2) else 4   # EST from Nov 1, 2026
    return (d + timedelta(hours=23 + offset, minutes=59)).strftime("%Y-%m-%dT%H:%M:%SZ")


def unit(rec):
    ret = {"rep": int(rec.get("repReturned") or 0), "dem": int(rec.get("demReturned") or 0),
           "npa": int(rec.get("npaReturned") or 0), "oth": int(rec.get("otherReturned") or 0)}
    blk = C.party_block(ret["rep"], ret["dem"], ret["oth"], ret["npa"])
    sent, returned = int(rec.get("sent") or 0), int(rec.get("returned") or blk["total"])
    return {"mail_voted": blk, "cast": dict(blk), "registered": 0, "turnout_pct": None,
            "mail": {"requested": sent, "returned": returned, "outstanding": max(0, sent - returned),
                     "return_rate": C.pct(returned, sent), "parties": {}}}


def main():
    force = "--force" in sys.argv
    cfg = load(CONFIG_PATH, {}) or {}
    src = cfg.get("districts_305", {})
    if not src:
        return 0
    probe = os.path.join(DATA_DIR, "districts_cd.json")
    if not force and os.path.exists(probe) and C.checked_recently(DATA_DIR, "districts305"):
        print("fl districts: checked under an hour ago.")
        return 0
    base = src["base"].rstrip("/")
    try:
        idx = json.loads(C.http_get(base + src["index"], no_cache=True, retries=2))
    except Exception as e:  # noqa: BLE001
        print("FL districts: index unavailable: %s" % str(e)[:120], file=sys.stderr)
        return 0
    if str(idx.get("electionNumber")) != str(cfg["tqv"]["fvrs_election_number"]):
        print("FL districts: tracker is on election %s, not %s — skipping."
              % (idx.get("electionNumber"), cfg["tqv"]["fvrs_election_number"]), file=sys.stderr)
        return 0
    through = idx.get("dataThrough", "")
    cov = idx.get("coverage") or {}
    sw = idx.get("statewide") or {}
    sw_ret = {"rep": sw.get("repReturned", 0), "dem": sw.get("demReturned", 0),
              "npa": sw.get("npaReturned", 0), "oth": sw.get("otherReturned", 0)}
    statewide = unit(dict(sent=sw.get("sent"), returned=sw.get("returned"), repReturned=sw_ret["rep"],
                          demReturned=sw_ret["dem"], npaReturned=sw_ret["npa"], otherReturned=sw_ret["oth"]))
    loaded, expected = cov.get("countiesLoaded"), cov.get("countiesExpected")
    short = ("; %s of %s counties loaded (%s)" % (loaded, expected, cov.get("note", "").split(".")[1].strip()
                                                  if cov.get("note", "").count(".") > 1 else cov.get("note", ""))
             if loaded and expected and loaded != expected else "")
    for key, (suffix, geo_name, prefix, label, plural) in KINDS.items():
        recs = []
        try:
            for path in idx["files"][key]:
                part = json.loads(C.http_get(base + path, no_cache=True, retries=2))
                recs += part if isinstance(part, list) else part.get("districts", [])
        except Exception as e:  # noqa: BLE001
            print("FL districts: %s files unavailable: %s" % (key, str(e)[:100]), file=sys.stderr)
            continue
        geo = load(os.path.join(ROOT, "assets", geo_name + ".geojson"), {"features": []})
        gidx = {f["properties"]["district_number"]: f["properties"] for f in geo["features"]}
        units, unknown = {}, []
        for r in recs:
            d = str(r.get("district") or "").strip()
            if not d.isdigit() or int(d) not in gidx:
                unknown.append(d)
                continue
            units[gidx[int(d)]["name"]] = dict(unit(r), fips=gidx[int(d)]["fips"])
        if unknown or len(units) < 0.9 * len(gidx):
            print("FL districts: %s doesn't fit the map (unknown %s, %d of %d) — keeping the previous file."
                  % (key, unknown[:5], len(units), len(gidx)), file=sys.stderr)
            continue
        in_districts = sum(u["cast"]["total"] for u in units.values())
        note = ("Vote-by-mail ballots returned through the close of %s (prior day) — not live like the county view. "
                "District totals from 305 Data Girl's Florida tracker, built from the state's county voter-level "
                "vote-by-mail files%s." % (through, short))
        if in_districts < statewide["cast"]["total"]:
            note += (" %s returned ballots have no district assignment and appear only in the statewide total."
                     % format(statewide["cast"]["total"] - in_districts, ","))
        body = {"state": "fl", "election": cfg.get("election", {}), "unit_label": label, "unit_label_plural": plural,
                "partisan": True,
                "source": "305 Data Girl, Florida 2026 General district tables (%s/general/districts), from the "
                          "state's county voter-level vote-by-mail files; data through %s" % (base, through),
                "methods_present": ["mail_voted"], "method_labels": {"cast": "Vote-by-mail returned",
                                                                     "mail_voted": "Vote-by-mail returned"},
                "coverage": {"ok": len(units), "total": len(gidx), "unmatched": []},
                "as_of": through, "statewide": statewide, "counties": units, "map_note": note}
        out = os.path.join(DATA_DIR, "districts_%s.json" % suffix)
        prev = load(out, {}) or {}
        h = C.data_hash(body)
        if h == prev.get("data_hash") and not force:
            print("fl districts %s: NOCHANGE (through %s)" % (suffix, through))
            continue
        now = C.utc_now_iso()
        with open(out, "w", encoding="utf-8") as f:
            json.dump(dict(body, source_compiled=through, source_compiled_iso=close_of_day(through) or now, data_hash=h,
                           generated_at=now), f,
                      separators=(",", ":"))
        print("fl districts %s: CHANGED %d districts, %d returned (through %s)" % (suffix, len(units), in_districts, through))
    return 0


if __name__ == "__main__":
    sys.exit(main())
