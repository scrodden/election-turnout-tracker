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
    d = None
    for fmt in ("%m/%d/%Y", "%B %d, %Y"):   # "09/30/2026" or, since October, "October 20, 2026"
        try:
            d = datetime.strptime(mdy, fmt)
            break
        except (TypeError, ValueError):
            pass
    if d is None:
        return ""
    offset = 5 if (d.month, d.day) >= (11, 1) or d.month in (12, 1, 2) else 4   # EST from Nov 1, 2026
    return (d + timedelta(hours=23 + offset, minutes=59)).strftime("%Y-%m-%dT%H:%M:%SZ")


def unit(rec):
    """One district (or the statewide row): vote-by-mail returned and, since early voting opened, early
    in-person votes, each by party ('<party>Returned' / '<party>Early'); cast = their '<party>Cast', the two
    together with a voter found in both files counted once (10/24: one Broward Democrat), so it may run a few
    below returned + early -- never above, and never by more than 5 or 0.1% (checked)."""
    def parts(suffix):
        return {p: int(rec.get(k + suffix) or 0) for p, k in (("rep", "rep"), ("dem", "dem"), ("npa", "npa"), ("oth", "other"))}
    ret, ev, theirs = parts("Returned"), parts("Early"), parts("Cast")
    both = {p: ret[p] + ev[p] for p in ret}
    if any(theirs.values()):
        gap = sum(both.values()) - sum(theirs.values())
        if any(theirs[p] > both[p] for p in theirs) or gap > max(5, sum(both.values()) // 1000):
            raise ValueError("district %s: cast %s vs returned + early %s" % (rec.get("district"), theirs, both))
    else:
        theirs = both
    mail = C.party_block(ret["rep"], ret["dem"], ret["oth"], ret["npa"])
    early = C.party_block(ev["rep"], ev["dem"], ev["oth"], ev["npa"])
    cast = C.party_block(theirs["rep"], theirs["dem"], theirs["oth"], theirs["npa"])
    for b in (mail, early, cast):
        b.pop("compiled", None)
        b.pop("compiled_iso", None)
    sent, returned = int(rec.get("sent") or 0), int(rec.get("returned") or mail["total"])
    u = {"mail_voted": mail, "cast": cast, "registered": 0, "turnout_pct": None, "in_both": sum(both.values()) - cast["total"],
         "mail": {"requested": sent, "returned": returned, "outstanding": max(0, sent - returned),
                  "return_rate": C.pct(returned, sent), "parties": {}}}
    if early["total"]:
        u["early_voted"] = early
    return u


def main():
    force = "--force" in sys.argv
    cfg = load(CONFIG_PATH, {}) or {}
    src = cfg.get("districts_305", {})
    if not src:
        return 0
    probe = os.path.join(DATA_DIR, "districts_cd.json")
    # they publish once a day (late morning ET, after the state's county files):
    # check the small index every ~20 minutes, district files only when it changes
    if not force and os.path.exists(probe) and C.checked_recently(DATA_DIR, "districts305", minutes=20):
        print("fl districts: checked under 20 minutes ago.")
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
    prev_cd = load(probe, {}) or {}
    if not force and idx.get("generatedAt") and idx.get("generatedAt") == prev_cd.get("source_generated"):
        print("fl districts: NOCHANGE (their tables generated %s, through %s)" % (idx["generatedAt"], through))
        return 0
    cov = idx.get("coverage") or {}
    statewide = unit(idx.get("statewide") or {})
    in_both = statewide.pop("in_both", 0)
    has_early = bool(statewide.get("early_voted"))
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
            u = unit(r)
            u.pop("in_both")
            units[gidx[int(d)]["name"]] = dict(u, fips=gidx[int(d)]["fips"])
        if unknown or len(units) < 0.9 * len(gidx):
            print("FL districts: %s doesn't fit the map (unknown %s, %d of %d) — keeping the previous file."
                  % (key, unknown[:5], len(units), len(gidx)), file=sys.stderr)
            flag("%s table doesn't fit the map (%d of %d districts) -- previous file kept" % (key, len(units), len(gidx)))
            continue
        in_districts = sum(u["cast"]["total"] for u in units.values())
        note = ("%s through the close of %s (prior day) — not live like the county view. "
                "District totals from 305 Data Girl's Florida tracker, built from the state's county voter-level "
                "vote-by-mail and early-voting files%s." % ("Vote-by-mail ballots returned and early in-person votes" if has_early
                                                          else "Vote-by-mail ballots returned", through, short))
        if in_both:
            note += (" %s voter%s found in both the mail and early-voting files %s counted once in the combined totals."
                     % (format(in_both, ","), "" if in_both == 1 else "s", "is" if in_both == 1 else "are"))
        if in_districts < statewide["cast"]["total"]:
            note += (" %s ballots have no district assignment and appear only in the statewide total."
                     % format(statewide["cast"]["total"] - in_districts, ","))
        body = {"state": "fl", "election": cfg.get("election", {}), "unit_label": label, "unit_label_plural": plural,
                "partisan": True,
                "source": "305 Data Girl, Florida 2026 General district tables (%s/general/districts), from the "
                          "state's county voter-level vote-by-mail and early-voting files; data through %s" % (base, through),
                "methods_present": ["mail_voted"] + (["early_voted"] if has_early else []),
                "method_labels": {"cast": "Ballots cast (mail + early in person)" if has_early else "Vote-by-mail returned",
                                  "mail_voted": "Vote-by-mail returned", "early_voted": "Voted early in person"},
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
                           generated_at=now, source_generated=idx.get("generatedAt")), f,
                      separators=(",", ":"))
        print("fl districts %s: CHANGED %d districts, %d returned (through %s)" % (suffix, len(units), in_districts, through))
    return 0


def flag(msg):
    """Mark Florida's entry in data/_health.json (written by run_all_states just before this step) so a district
    failure shows on the status page instead of the district maps silently going stale (10/23-25)."""
    path = os.path.join(ROOT, "data", "_health.json")
    h = load(path, {}) or {}
    rec = h.setdefault("states", {}).setdefault("fl", {})
    rec.update(status="error", error=("district maps: " + msg)[:300], last_error_at=C.utc_now_iso())
    with open(path, "w", encoding="utf-8") as f:
        json.dump(h, f, separators=(",", ":"))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        flag("%s: %s" % (type(e).__name__, str(e)[:200]))
        raise
