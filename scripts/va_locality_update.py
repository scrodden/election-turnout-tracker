#!/usr/bin/env python3
"""Virginia early-vote turnout by LOCALITY (data/va/locality.json).

There is no official public locality feed: VA ELECT's daily absentee list is
sold only to qualified requesters, and VPAP's 133 per-locality pages sit behind
bot protection that blocks automated access (we used to scrape them; CI got ~7
of 133 before being cut off, so that approach is retired).

VPAP does publish its visuals' data as public files on S3 (not bot-protected):
for the April 2026 referendum, datasets/locality_earlyvotes_2026apr.json -- a
TopoJSON whose 133 locality geometries carry {locality, early_votes, ...}, updated
daily. This script watches for the November 2026 equivalent:
  1. reuse the dataset URL found on an earlier run, if it still resolves;
  2. else probe config source.locality_dataset_candidates on S3 (cheap HEADs);
  3. else look once for a new VPAP visual whose slug mentions locality + 2026
     November/general, and read its dataset URL from the visual's JS bundle
     (at most a few requests a run -- well within what VPAP serves).
A file only counts if its 'updated' stamp is on/after source.locality_min_updated
(so the April referendum file can never be mistaken for November).

Registered (active) voters per locality come from VA ELECT's monthly CSV
(va_registration.py) for turnout %. Until the dataset exists, locality.json
carries only the outbound VPAP link directory status (no numbers).

Run:  python scripts/va_locality_update.py [--test-url URL]
      (--test-url parses a given dataset, e.g. the April file, without writing)
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402
import va_registration  # noqa: E402

STATE = "va"
VA_CONFIG = os.path.join(ROOT, "config", "va.json")
LOC_CONFIG = os.path.join(ROOT, "config", "va_localities.json")
GEO_PATH = os.path.join(ROOT, "assets", "va-counties.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
OUT_PATH = os.path.join(DATA_DIR, "locality.json")
VPAP = "https://www.vpap.org"


def load(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower().replace("&", "and"))


def _block(total):
    return {"rep": 0, "dem": 0, "oth": 0, "npa": 0, "total": int(total),
            "rep_pct": None, "dem_pct": None, "npa_pct": None, "oth_pct": None, "margin": None}


def _exists(url):
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": C.USER_AGENT})
        with urllib.request.urlopen(req, timeout=20, context=C._SSL_CTX) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001 - S3 answers 403 for keys that don't exist
        return False


def _updated_ok(topo, min_updated):
    """VPAP stamps datasets like 'Apr 20, 2026 09:01 AM'."""
    s = (topo or {}).get("updated") or ""
    for fmt in ("%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p", "%Y-%m-%d %H:%M:%S %p", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d") >= min_updated
        except ValueError:
            continue
    return False


def discover_from_visuals():
    """One look at VPAP's visuals index for a November-2026 locality visual; if
    found, pull the dataset URL out of its S3-hosted JS bundle."""
    try:
        idx = C.http_get(VPAP + "/visuals/", retries=1, timeout=30)
    except Exception as e:  # noqa: BLE001
        print("  (visuals index unavailable: %s)" % str(e)[:60], file=sys.stderr)
        return []
    slugs = sorted(set(re.findall(r'href="(/visuals/visual/[^"]+/)"', idx)))
    hits = [s for s in slugs if "locality" in s and "2026" in s and ("nov" in s or "general" in s)]
    urls = []
    for slug in hits[:2]:
        try:
            page = C.http_get(VPAP + slug, retries=1, timeout=30)
        except Exception:  # noqa: BLE001
            continue
        found = re.findall(r"https://vpap-production[^\"'<> ]+?\.json", page)
        for js in re.findall(r"https://vpap-production[^\"'<> ]+?/assets/index\.js", page)[:1]:
            try:
                found += re.findall(r"https://vpap-production[^\"'`<> ]+?\.json", C.http_get(js, retries=1))
            except Exception:  # noqa: BLE001
                pass
        urls += [u for u in found if "locality" in u.lower()]
    return urls


def find_dataset(src, prev):
    tried = []
    known = ((prev or {}).get("source_detail") or {}).get("dataset_url")
    if known:
        tried.append(known)
    tried += src.get("locality_dataset_candidates", [])
    for u in tried:
        if _exists(u):
            return u, "known" if u == known else "candidate"
    for u in discover_from_visuals():
        if _exists(u):
            return u, "visual"
    return None, None


def locality_index():
    """VPAP locality label -> FIPS. VPAP uses bare names ('Augusta',
    'Alexandria') and adds City/County only where two localities share a name
    ('Fairfax City' / 'Fairfax County')."""
    locs = (load(LOC_CONFIG, {}) or {}).get("localities", [])
    full = {_norm(L["name"]): L["fips"] for L in locs}

    def match(label):
        n = _norm(label)
        if n in full:
            return full[n]
        keys = [n + "county", n + "city"]
        if n.endswith("county"):       # 'James City County' vs our 'James City'
            keys.append(n[:-len("county")])
        hits = [full[k] for k in keys if k in full]
        return hits[0] if len(hits) == 1 else None
    return match, {L["fips"]: L for L in locs}


def parse(topo):
    """-> ({fips: {total, mail, in_person}}, unmatched_labels)"""
    match, _ = locality_index()
    obj = next(iter((topo.get("objects") or {}).values()), {})
    out, unmatched = {}, []
    for g in obj.get("geometries", []):
        p = g.get("properties") or {}
        label = p.get("locality") or p.get("name") or ""
        fips = match(label)
        if not fips:
            unmatched.append(label)
            continue
        total = next((p[k] for k in ("early_votes", "ballots", "total", "early_ballots") if p.get(k) is not None), None)
        if total is None:
            unmatched.append(label)
            continue
        mail = next((p[k] for k in ("mail_ballots", "mail", "by_mail") if p.get(k) is not None), None)
        inp = next((p[k] for k in ("in_person", "in_person_ballots") if p.get(k) is not None), None)
        out[fips] = {"total": int(total), "mail": None if mail is None else int(mail),
                     "in_person": None if inp is None else int(inp)}
    return out, unmatched


LAB_BASE = "https://election.lab.ufl.edu/data-downloads/earlyvote/2026/"
LAB_PAGE = "https://election.lab.ufl.edu/early-vote/2026-early-voting/"


def lab_rows():
    """UF Election Lab VA_county.csv (133 localities; built from VPAP/ELECT data):
    request_all, accept_all (mail returned), inperson_all, voted_all. Rows must
    match our localities and sum to the Lab's statewide VA row.
    -> ({fips: {total, mail, in_person, requested}}, as_of, unmatched)"""
    import csv
    import io
    match, _ = locality_index()
    state = next((r for r in csv.DictReader(io.StringIO(C.http_get(LAB_BASE + "US.csv", no_cache=True)))
                  if (r.get("state_abbv") or "").upper() == "VA"), None)
    crow = list(csv.DictReader(io.StringIO(C.http_get(LAB_BASE + "VA_county.csv", no_cache=True))))

    def n(r, k):
        v = str((r or {}).get(k, "") or "").replace(",", "").strip()
        try:
            return int(float(v)) if v else 0
        except ValueError:
            return 0
    if not state or not crow:
        return {}, "", []
    for k in ("request_all", "accept_all", "inperson_all", "voted_all"):
        if sum(n(r, k) for r in crow) != n(state, k):
            raise RuntimeError("Lab VA_county %s doesn't sum to the statewide row" % k)
    out, unmatched = {}, []
    for r in crow:
        fips = match(r.get("county", ""))
        if not fips:
            unmatched.append(r.get("county"))
            continue
        out[fips] = {"total": n(r, "voted_all"), "mail": n(r, "accept_all"), "in_person": n(r, "inperson_all"),
                     "requested": n(r, "request_all")}
    return out, state.get("last_update", ""), unmatched


def build(rows, reg, dataset_url, topo):
    _, by_fips = locality_index()
    geo = load(GEO_PATH, {"features": []})
    name_by_fips = {ft["properties"]["fips"]: ft["properties"]["name"] for ft in geo["features"]}
    reg_loc = (reg or {}).get("locality", {})
    counties = {}
    tot = {"cast": 0, "mail": 0, "inp": 0, "reg": 0}
    has_split = any(r["mail"] is not None or r["in_person"] is not None for r in rows.values())
    for fips, r in rows.items():
        key = name_by_fips.get(fips, by_fips.get(fips, {}).get("name", fips))
        registered = int((reg_loc.get(fips) or {}).get("active") or 0)
        e = {"fips": fips, "cast": _block(r["total"]), "registered": registered,
             "turnout_pct": (round(100.0 * r["total"] / registered, 2) if registered else None),
             "url": by_fips.get(fips, {}).get("url")}
        if has_split:
            e["mail_voted"] = _block(r["mail"] or 0)
            e["early_voted"] = _block(r["in_person"] or 0)
            tot["mail"] += r["mail"] or 0
            tot["inp"] += r["in_person"] or 0
        if r.get("requested") is not None:
            e["mail_provided"] = _block(max(0, r["requested"] - (r["mail"] or 0)))
            tot["out"] = tot.get("out", 0) + max(0, r["requested"] - (r["mail"] or 0))
            m = C.compute_mail(e)
            if m:
                e["mail"] = m
        counties[key] = e
        tot["cast"] += r["total"]
        tot["reg"] += registered
    statewide = {"cast": _block(tot["cast"]), "registered": tot["reg"],
                 "turnout_pct": (round(100.0 * tot["cast"] / tot["reg"], 2) if tot["reg"] else None)}
    methods = []
    if has_split:
        statewide["mail_voted"] = _block(tot["mail"])
        statewide["early_voted"] = _block(tot["inp"])
        methods = [m for m, t in (("mail_voted", tot["mail"]), ("early_voted", tot["inp"])) if t]
    if "out" in tot:
        statewide["mail_provided"] = _block(tot["out"])
        m = C.compute_mail(statewide)
        if m:
            statewide["mail"] = m
        methods.append("mail_provided")
    return statewide, counties, methods


def main():
    test_url = None
    if "--test-url" in sys.argv:
        test_url = sys.argv[sys.argv.index("--test-url") + 1]
    src = (load(VA_CONFIG, {}) or {}).get("source", {})
    loc_cfg = load(LOC_CONFIG, {}) or {}
    prev = load(OUT_PATH, {}) or {}
    total_localities = len(loc_cfg.get("localities", []))

    # 1) UF Election Lab locality file (all 133, mail/in-person split, requests)
    lab, lab_asof, lab_unmatched = ({}, "", [])
    if not test_url:
        try:
            lab, lab_asof, lab_unmatched = lab_rows()
        except Exception as e:  # noqa: BLE001
            print("VA locality: Election Lab file unusable: %s" % str(e)[:120], file=sys.stderr)
        if lab_unmatched:
            print("VA locality: Lab localities unmatched %s — not used." % lab_unmatched[:5], file=sys.stderr)
            lab = {}
    if lab:
        reg = va_registration.get()
        statewide, counties, methods = build(lab, reg, None, None)
        status = "ok"
        doc = {
            "state": STATE, "election": loc_cfg.get("election", "2026 November General"),
            "unit_label": "Locality", "unit_label_plural": "Localities", "partisan": False,
            "source": "VPAP locality early-vote data, as republished by the UF Election Lab (VA_county.csv; CC BY-NC-ND 4.0)",
            "source_detail": {"dataset_url": LAB_BASE + "VA_county.csv", "found_via": "election-lab",
                              "dataset_updated": lab_asof, "registration_as_of": (reg or {}).get("as_of"), "status": status},
            "source_compiled": lab_asof, "source_compiled_iso": C.utc_now_iso(),
            "methods_present": methods,
            "method_labels": {"cast": "All early ballots", "mail_voted": "By mail", "early_voted": "In person"},
            "coverage": {"ok": len(lab), "total": total_localities, "unmatched": []},
            "as_of": lab_asof, "statewide": statewide, "counties": counties,
        }
        doc["data_hash"] = C.data_hash({k: v for k, v in doc.items() if k not in ("source_detail", "source_compiled_iso")})
        if doc["data_hash"] == prev.get("data_hash"):
            print("va locality: no change (Election Lab as of %s)" % lab_asof)
            return 0
        doc["generated_at"] = C.utc_now_iso()
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            json.dump(doc, f, separators=(",", ":"))
        print("va locality: Election Lab as of %s | %d/%d localities | cast=%d | turnout=%s%%"
              % (lab_asof, len(lab), total_localities, statewide["cast"]["total"], statewide["turnout_pct"]))
        return 0

    # 2) otherwise watch for VPAP's public locality dataset
    url, how = (test_url, "test") if test_url else find_dataset(src, prev)
    topo = None
    if url:
        try:
            topo = json.loads(C.http_get(url, no_cache=True).lstrip("﻿"))
        except Exception as e:  # noqa: BLE001
            print("VA locality dataset fetch failed: %s" % str(e)[:120], file=sys.stderr)
    if topo and not test_url and not _updated_ok(topo, src.get("locality_min_updated", "2026-09-01")):
        print("VA locality: %s is dated %r — not the November dataset; ignoring." % (url, topo.get("updated")))
        topo = None

    rows, unmatched = parse(topo) if topo else ({}, [])
    reg = va_registration.get()
    statewide, counties, methods = build(rows, reg, url, topo)
    if test_url:
        print("TEST %s (updated %s): %d/%d localities matched, cast=%d, registered=%d, turnout=%s%%, unmatched=%s"
              % (url, topo.get("updated") if topo else None, len(rows), total_localities,
                 statewide["cast"]["total"], statewide["registered"], statewide["turnout_pct"], unmatched))
        return 0

    status = ("ok" if rows else
              "waiting: VPAP has not published a November 2026 locality dataset yet")
    doc = {
        "state": STATE, "election": loc_cfg.get("election", "2026 November General"),
        "unit_label": "Locality", "unit_label_plural": "Localities", "partisan": False,
        "source": "Virginia Public Access Project (VPAP) locality early-vote dataset" if rows else "",
        "source_detail": {"dataset_url": url if rows else None, "found_via": how if rows else None,
                          "dataset_updated": topo.get("updated") if (topo and rows) else None,
                          "registration_as_of": (reg or {}).get("as_of"), "status": status},
        "methods_present": methods,
        "method_labels": {"cast": "All early ballots", "mail_voted": "By mail", "early_voted": "In person"},
        "coverage": {"ok": len(rows), "total": total_localities, "unmatched": unmatched[:20]},
        "as_of": (topo.get("updated") if (topo and rows) else ""),
        "statewide": statewide, "counties": counties,
    }
    doc["data_hash"] = C.data_hash({k: v for k, v in doc.items() if k != "source_detail"})
    if doc["data_hash"] == prev.get("data_hash") and (prev.get("source_detail") or {}).get("status") == status:
        print("va locality: no change (%s)" % status)
        return 0
    doc["generated_at"] = C.utc_now_iso()
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"))
    print("va locality: %s | %d/%d localities | cast=%d | turnout=%s%%"
          % (status, len(rows), total_localities, statewide["cast"]["total"], statewide["turnout_pct"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
