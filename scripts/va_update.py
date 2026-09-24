#!/usr/bin/env python3
"""Fetch Virginia early-vote turnout by U.S. House district (turnout-only; VA has
no party registration). Source: VPAP's 2026-general "Early Voting by Congressional
District" TopoJSON on S3. Each district geometry carries properties:
  district ("CD1"), district_number, ballots, mail_ballots, in_person,
  vpap_index (+ _description), candidates[].
We read only the properties; assets/va-cd.geojson supplies the map geometry.

VPAP bumps the dated S3 path when it re-cuts the file, so we discover the dataset
URL from the public visual page (config source.visual_url + dataset_pattern) and
fall back to source.dataset_url. Ballots split into by-mail (mail_voted) and
in-person (early_voted); no partisan breakdown exists (VA doesn't tag ballots by
party), so every party field is 0 and only totals are populated.

Run:  python scripts/va_update.py [--force]
      python scripts/va_update.py --validate   (print per-district numbers only)
"""
import os
import re
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402
import va_registration  # noqa: E402

STATE = "va"
CONFIG_PATH = os.path.join(ROOT, "config", "va.json")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def discover_url(cfg):
    """Return the current dataset URL: scrape it from the visual page, else the
    configured fallback. VPAP re-cuts the file under new dated S3 paths."""
    src = cfg["source"]
    pat = src.get("dataset_pattern")
    vis = src.get("visual_url")
    if pat and vis:
        try:
            html = C.http_get(vis, no_cache=True, referer="https://www.vpap.org/")
            m = re.search(pat, html)
            if m:
                return m.group(0)
        except Exception as e:  # noqa: BLE001
            print("  (visual-page discovery failed: %s)" % str(e)[:80], file=sys.stderr)
    return src.get("dataset_url", "")


def fetch_topo(cfg):
    url = discover_url(cfg)
    if not url:
        return None, None
    try:
        return url, json.loads(C.http_get(url, no_cache=True, referer="https://www.vpap.org/"))
    except Exception as e:  # noqa: BLE001
        print("  (dataset fetch failed: %s)" % str(e)[:80], file=sys.stderr)
        return url, None


def _block(total):
    return {"rep": 0, "dem": 0, "oth": 0, "npa": 0, "total": int(total),
            "rep_pct": None, "dem_pct": None, "npa_pct": None, "oth_pct": None, "margin": None}


def main():
    force = "--force" in sys.argv
    validate = "--validate" in sys.argv
    cfg = load(CONFIG_PATH)

    url, topo = fetch_topo(cfg)
    geoms = []
    if topo and isinstance(topo.get("objects"), dict) and topo["objects"]:
        okey = list(topo["objects"].keys())[0]
        geoms = topo["objects"][okey].get("geometries", []) or []

    reg = None if validate else va_registration.get()
    reg_cd = (reg or {}).get("cd", {})

    counties_out = {}
    cast_total = mail_total = inperson_total = 0
    for g in geoms:
        p = g.get("properties", {})
        d = p.get("district")
        if not d:
            continue
        ballots = int(p.get("ballots") or 0)
        mail = int(p.get("mail_ballots") or 0)
        inp = int(p.get("in_person") or 0)
        registered = int(reg_cd.get(d) or 0)
        entry = {"fips": "51-%02d" % int(p.get("district_number") or 0),
                 "cast": _block(ballots), "mail_voted": _block(mail), "early_voted": _block(inp),
                 "registered": registered,
                 "turnout_pct": (round(100.0 * ballots / registered, 2) if registered else None),
                 "lean": p.get("vpap_index"), "lean_desc": p.get("vpap_index_description")}
        cands = p.get("candidates")
        if cands:
            entry["candidates"] = [{"name": c.get("name"), "party": c.get("party")} for c in cands]
        counties_out[d] = entry
        cast_total += ballots; mail_total += mail; inperson_total += inp

    if validate:
        print("VA by-district validation on %s (updated %s):" % (url, topo.get("updated") if topo else "n/a"))
        for d in sorted(counties_out, key=lambda k: int(re.sub(r"\D", "", k) or 0)):
            e = counties_out[d]
            print("  %-5s ballots=%6d  mail=%5d  in_person=%6d  %s"
                  % (d, e["cast"]["total"], e["mail_voted"]["total"], e["early_voted"]["total"], e.get("lean_desc")))
        print("  TOTAL ballots=%d  mail=%d  in_person=%d  districts=%d"
              % (cast_total, mail_total, inperson_total, len(counties_out)))
        return 0

    if not counties_out:
        prev_snap = {}
        try:
            prev_snap = load(LATEST_PATH)
        except (ValueError, OSError):
            pass
        if prev_snap.get("counties"):
            print("VA: no district data this run — keeping the previous snapshot.")
            return 0

    methods_present = []
    if mail_total:
        methods_present.append("mail_voted")
    if inperson_total:
        methods_present.append("early_voted")

    reg_total = sum(e["registered"] for e in counties_out.values())
    statewide = {"cast": _block(cast_total), "mail_voted": _block(mail_total),
                 "early_voted": _block(inperson_total), "registered": reg_total,
                 "turnout_pct": (round(100.0 * cast_total / reg_total, 2) if reg_total else None)}
    snap = {
        "state": STATE, "state_name": cfg["state_name"], "election": cfg["election"],
        "partisan": False,
        "unit_label": cfg.get("unit_label", "District"),
        "unit_label_plural": cfg.get("unit_label_plural", "Districts"),
        "source": {"primary": "VPAP early voting by U.S. House district (2026 general; turnout-only, VA has no party registration)",
                   "registration": ("VA ELECT active registered voters by district, as of %s" % reg["cd_as_of"]) if reg else ""},
        "source_compiled": (topo.get("updated") if topo else "") or "",
        "source_compiled_iso": C.utc_now_iso() if topo else "",
        "methods_present": methods_present,
        "method_labels": cfg.get("method_labels", {}),
        "statewide": statewide, "counties": counties_out,
    }
    snap["data_hash"] = C.data_hash({k: v for k, v in snap.items() if k != "source_compiled_iso"})
    snap["generated_at"] = C.utc_now_iso()

    prev = None
    if os.path.exists(LATEST_PATH):
        try:
            prev = load(LATEST_PATH).get("data_hash")
        except (ValueError, OSError):
            pass
    changed = force or (snap["data_hash"] != prev)
    os.makedirs(DATA_DIR, exist_ok=True)
    if changed:
        with open(LATEST_PATH, "w", encoding="utf-8") as f:
            json.dump(snap, f, separators=(",", ":"))
        if cast_total:
            with open(HISTORY_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"generated_at": snap["generated_at"], "compiled": snap["source_compiled"],
                                    "data_hash": snap["data_hash"], "cast": cast_total,
                                    "mail": mail_total, "in_person": inperson_total},
                                   separators=(",", ":")) + "\n")
        print("CHANGED  districts=%d  early_ballots=%d  (mail=%d, in_person=%d)  updated=%s"
              % (len(counties_out), cast_total, mail_total, inperson_total, snap["source_compiled"]))
    else:
        print("NOCHANGE  (hash %s)" % (prev or "")[:12])
    return 0


if __name__ == "__main__":
    sys.exit(main())
