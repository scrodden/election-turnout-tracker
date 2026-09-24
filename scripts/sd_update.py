#!/usr/bin/env python3
"""South Dakota absentee ballots -- STATEWIDE ONLY, by registered party.

Source: SD Secretary of State "2026 Weekly Absentee Numbers" page (updated
Fridays). Each election gets a section headed 'South Dakota 2026 <election>
Absentee Ballot Weekly Statistics' with
  a weekly table:  Date | Ballots Sent | Ballots Received | Walk-in Voters | UOCAVA
  a party table:   Party | Ballots Sent | Ballots Received | Walk-in Voters | UOCAVA
                   (DEM, IND, LIB, NPA, OTH, REP; 'as of' the latest week)
South Dakota does not publish these by county, so counties stay empty and the
state contributes statewide totals (and party mix) only. Received includes
walk-in (in-person absentee) ballots, so everything is reported as 'cast'.

REP->rep, DEM->dem, IND+NPA->npa, LIB+OTH->oth. mail_voted = received,
mail_provided = sent - received (outstanding) -> ballot chase by party.

Run:  python scripts/sd_update.py [--force] [--section "Primary"]  (--section: test on another election)
"""
import html as H
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402
import lab_standin as LAB  # noqa: E402

STATE = "sd"
CONFIG_PATH = os.path.join(ROOT, "config", "sd.json")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
GEO_PATH = os.path.join(ROOT, "assets", "sd-counties.geojson")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")

PARTY = {"REP": "rep", "DEM": "dem", "IND": "npa", "NPA": "npa", "LIB": "oth", "OTH": "oth"}


def load(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def _num(s):
    s = re.sub(r"[^\d]", "", s)   # the page has typos like '21.718'
    return int(s) if s else 0


def _tables(fragment):
    """Each <table> in the fragment as a list of rows of cell text."""
    out = []
    for t in re.findall(r"<table.*?</table>", fragment, re.S | re.I):
        rows = []
        for tr in re.findall(r"<tr.*?</tr>", t, re.S | re.I):
            cells = [re.sub(r"\s+", " ", H.unescape(re.sub(r"<[^>]+>", "", c))).strip()
                     for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I)]
            if cells:
                rows.append(cells)
        out.append(rows)
    return out


def parse(page, section_word):
    heads = [(m.start(), H.unescape(re.sub(r"<[^>]+>", "", m.group(1))))
             for m in re.finditer(r"<strong>(South Dakota 2026 .*?Absentee Ballot Weekly Statistics)</strong>", page, re.S)]
    for i, (pos, title) in enumerate(heads):
        if section_word.lower() not in title.lower():
            continue
        end = heads[i + 1][0] if i + 1 < len(heads) else len(page)
        tabs = _tables(page[pos:end])
        if len(tabs) < 2:
            return None
        weekly = [r for r in tabs[0] if re.match(r"\d{1,2}/\d{1,2}/\d{2,4}", r[0])]
        as_of = re.search(r"as of\s*([\d/]+)", page[pos:end])
        parties = {}
        for r in tabs[1]:
            code = r[0].strip().upper()
            if code in PARTY and len(r) >= 3:
                p = PARTY[code]
                s, rc = _num(r[1]), _num(r[2])
                cur = parties.setdefault(p, [0, 0])
                cur[0] += s
                cur[1] += rc
        return {"title": title.strip(), "latest": weekly[-1] if weekly else None,
                "as_of": as_of.group(1) if as_of else (weekly[-1][0] if weekly else ""), "parties": parties}
    return None


def _pb(d):
    return C.party_block(d.get("rep", 0), d.get("dem", 0), d.get("oth", 0), d.get("npa", 0))


def official_statewide(res):
    voted = {p: v[1] for p, v in res["parties"].items()}
    out = {p: max(0, v[0] - v[1]) for p, v in res["parties"].items()}
    statewide = {"mail_voted": _pb(voted), "mail_provided": _pb(out), "cast": _pb(voted),
                 "registered": 0, "turnout_pct": None}
    m = C.compute_mail(statewide)
    if m:
        statewide["mail"] = m
    return statewide


def main():
    """South Dakota: the Election Lab's county x party file (built from the SoS's
    data) is the primary source because the SoS's own weekly page is statewide
    only (Sam, 2026-09-24). If the SoS page ever shows MORE ballots than the Lab,
    its statewide totals are used and the Lab's county map is kept but flagged
    as trailing until the Lab catches up (Sam, 2026-09-24)."""
    force = "--force" in sys.argv
    test = "--section" in sys.argv
    cfg = load(CONFIG_PATH, {}) or {}
    src = cfg.get("source", {})
    section = sys.argv[sys.argv.index("--section") + 1] if test else src.get("section_match", "General")
    prev = load(LATEST_PATH, {}) or {}
    # both sources change at most daily (the SoS weekly): check about hourly
    from datetime import datetime, timezone
    if not (test or force) and prev.get("statewide") and datetime.now(timezone.utc).minute >= 12:
        print("sd: next check at the top of the hour.")
        return 0

    try:
        res = parse(C.http_get(src["page"], retries=2), section)
    except Exception as e:  # noqa: BLE001
        print("SD SoS page fetch failed: %s" % str(e)[:140], file=sys.stderr)
        res = None
    off = official_statewide(res) if res and res["parties"] else None
    if test:
        if not off:
            print("TEST: no %r section" % section)
            return 0
        v, m = off["cast"], off.get("mail") or {}
        print("TEST %s (as of %s): received=%d sent=%d  R%d D%d NPA%d O%d"
              % (res["title"], res["as_of"], v["total"], m.get("requested", 0), v["rep"], v["dem"], v["npa"], v["oth"]))
        return 0

    lab = LAB.build(STATE, cfg, GEO_PATH, True,
                    role="primary source for South Dakota (county detail; the SoS page is statewide only)")
    lab_cast = lab["statewide"]["cast"]["total"] if lab else -1
    off_cast = off["cast"]["total"] if off else -1

    if off and off_cast > lab_cast:
        counties = (lab or {}).get("counties") or {}
        lab_asof = (lab or {}).get("source", {}).get("as_of", "")
        note = ("County figures are from the UF Election Lab as of %s and trail the Secretary of State's statewide "
                "totals (as of %s) until the Lab catches up." % (lab_asof, res["as_of"])) if counties else ""
        snap = {
            "state": STATE, "state_name": cfg.get("state_name", "South Dakota"), "election": cfg.get("election", {}),
            "partisan": True, "statewide_only": not counties, "county_stale": bool(counties), "map_note": note,
            "source": {"primary": "South Dakota SoS weekly absentee statistics (statewide, by party) as of %s%s"
                                  % (res["as_of"], ("; county detail: UF Election Lab as of %s (CC BY-NC-ND 4.0), "
                                                    "trailing the SoS totals" % lab_asof) if counties else ""),
                       "section": res["title"], "as_of": res["as_of"], "lab_as_of": lab_asof},
            "source_compiled": res["as_of"],
            "methods_present": [k for k in ("mail_voted", "mail_provided") if off[k]["total"]],
            "method_labels": cfg.get("method_labels", {}), "statewide": off, "counties": counties,
        }
        if lab and lab.get("demographics"):
            snap["demographics"] = lab["demographics"]
        which = "SoS statewide (ahead of the Lab: %d vs %d)%s" % (off_cast, lab_cast, " + Lab counties (trailing)" if counties else "")
    elif lab:
        snap = {k: v for k, v in lab.items() if k not in ("source_compiled_iso", "data_hash")}
        which = "Election Lab (county x party)"
    elif off:
        snap = {
            "state": STATE, "state_name": cfg.get("state_name", "South Dakota"), "election": cfg.get("election", {}),
            "partisan": True, "statewide_only": True,
            "source": {"primary": "South Dakota SoS weekly absentee statistics (statewide, by party)",
                       "section": res["title"], "as_of": res["as_of"]},
            "source_compiled": res["as_of"],
            "methods_present": [k for k in ("mail_voted", "mail_provided") if off[k]["total"]],
            "method_labels": cfg.get("method_labels", {}), "statewide": off, "counties": {},
        }
        which = "SoS statewide only (no Lab data)"
    else:
        print("sd: nothing yet from the Election Lab or the SoS '%s' section." % section)
        return 0

    snap["data_hash"] = C.data_hash({k: v for k, v in snap.items() if k not in ("source_compiled_iso", "generated_at")})
    snap["source_compiled_iso"] = C.utc_now_iso()
    changed = force or snap["data_hash"] != prev.get("data_hash")
    snap["generated_at"] = C.utc_now_iso() if changed else prev.get("generated_at", C.utc_now_iso())
    c = snap["statewide"]["cast"]
    if not changed:
        print("NOCHANGE  %s: received=%d" % (which, c["total"]))
        return 0
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LATEST_PATH, "w", encoding="utf-8") as f:
        json.dump(snap, f, separators=(",", ":"))
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"generated_at": snap["generated_at"],
                            "statewide": {"cast": [c["rep"], c["dem"], c["oth"], c["npa"], c["total"]]}},
                           separators=(",", ":")) + "\n")
    print("CHANGED  %s: received=%d (R%d D%d NPA%d) margin=%s, %d counties"
          % (which, c["total"], c["rep"], c["dem"], c["npa"], c["margin"], len(snap["counties"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
