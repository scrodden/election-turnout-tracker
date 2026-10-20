#!/usr/bin/env python3
"""Arkansas early-voting turnout by county (turnout-only; Arkansas has no party
registration).

No public state feed during early voting: county boards report early/absentee
counts to the Secretary of State officially only after the polls close
(Ark. Code 7-5-416, 7-5-701), and sos.arkansas.gov posts no early-vote page or
file (its elections, news and research pages were checked 2026-10-14 and
2026-10-20). The SoS does share daily county counts with the UF Election Lab,
whose 2026 files cite "Arkansas Secretary of State" as the source (first posted
2026-10-20: 56,950 in-person early votes on day 1, Oct 19, matching the SoS's
announced figure, plus 3,380 mail ballots accepted, in 73 of 75 counties).
So this writes the Election Lab stand-in (scripts/lab_standin.py: US.csv +
AR_county.csv, used unaltered with attribution, county rows only if they all
match the map and add up to the statewide row). An official feed, if one is
wired in fetch_counties(), replaces it automatically.

The Lab's party columns (most voters "none": Arkansas records only which
party's primary ballot a voter took) aren't used.

Run:  python scripts/ar_update.py [--force]
"""
import os
import re
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "ar"
CONFIG_PATH = os.path.join(ROOT, "config", "ar.json")
GEO_PATH = os.path.join(ROOT, "assets", "ar-counties.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


RESEARCH_PAGE = "https://www.sos.arkansas.gov/elections/research"
REG_CACHE = os.path.join(DATA_DIR, "_registration.json")


def _key(name):
    return re.sub(r"[^a-z]", "", str(name).lower().replace("county", ""))


def registration(names):
    """Registered voters by county from the SoS's "Registered Voters [As of m/d/yyyy]" PDF (ES&S VR Statistics
    Count Report: a county per line, its Grand Total last; a "Grand Total" line at the end) linked on the
    research page -> ({county: n}, as_of, url). Cached in data/ar/_registration.json; re-read when the link changes."""
    try:
        with open(REG_CACHE, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    try:
        page = C.http_get(RESEARCH_PAGE, no_cache=True, retries=1, timeout=60)
        m = re.search(r'href="([^"]+?\.pdf)"[^>]*>\s*Registered Voters\s*\[As of ([\d/]+)\]', page, re.I)
    except Exception as e:  # noqa: BLE001
        print("ar: research page unavailable (%s)" % str(e)[:100], file=sys.stderr)
        m = None
    if not m:
        return cache.get("counties") or {}, cache.get("as_of", ""), cache.get("url", "")
    url = m.group(1) if m.group(1).startswith("http") else "https://www.sos.arkansas.gov" + m.group(1)
    if cache.get("url") == url and cache.get("counties"):
        return cache["counties"], cache["as_of"], url
    try:
        import io
        from pypdf import PdfReader
        text = "\n".join(pg.extract_text() or "" for pg in PdfReader(io.BytesIO(C.http_get(url, binary=True, timeout=90))).pages)
        rows = re.findall(r"(?m)^([A-Z][A-Za-z.' ]*?[a-z.])\s+((?:\d+ )*\d+)\s*$", text)
        idx = {_key(n): n for n in names}
        out, grand = {}, None
        for name, nums in rows:
            total = int(nums.split()[-1])
            if name.strip().lower() == "grand total":
                grand = total
            elif _key(name) in idx:
                out[idx[_key(name)]] = total
        if len(out) != len(names) or grand != sum(out.values()):
            raise ValueError("%d of %d counties, sum %d vs grand total %s" % (len(out), len(names), sum(out.values()), grand))
    except Exception as e:  # noqa: BLE001
        print("ar: registration report not used (%s)" % str(e)[:120], file=sys.stderr)
        return cache.get("counties") or {}, cache.get("as_of", ""), cache.get("url", "")
    mm, dd, yy = m.group(2).split("/")
    cache = {"url": url, "as_of": "%04d-%02d-%02d" % (int(yy) + (2000 if len(yy) == 2 else 0), int(mm), int(dd)), "counties": out}
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(REG_CACHE, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cache, f, separators=(",", ":"))
    return out, cache["as_of"], url


def add_registration():
    """Registered voters and turnout % on the written snapshot (the Lab's Arkansas rows carry none)."""
    d = load(LATEST_PATH)
    geo = load(GEO_PATH)
    reg, as_of, url = registration([f["properties"]["name"] for f in geo["features"]])
    if not reg:
        return
    before = json.dumps(d, sort_keys=True)
    for name, c in (d.get("counties") or {}).items():
        r = reg.get(name, 0)
        c["registered"] = r
        c["turnout_pct"] = round(100.0 * (c.get("cast") or {}).get("total", 0) / r, 2) if r else None
    sw = d.get("statewide") or {}
    sw["registered"] = sum(reg.values())
    sw["turnout_pct"] = round(100.0 * (sw.get("cast") or {}).get("total", 0) / sw["registered"], 2)
    d.setdefault("source", {})["registration"] = {"url": url, "as_of": as_of,
                                                  "label": "Arkansas Secretary of State, registered voters by county"}
    if json.dumps(d, sort_keys=True) != before:
        with open(LATEST_PATH, "w", encoding="utf-8", newline="\n") as f:
            json.dump(d, f, separators=(",", ":"))


def fetch_counties(cfg):
    """Return {county_name: {fips, cast_total, registered}} from an official Arkansas feed.

    None exists during early voting (see the module docstring), so {} -> the Election Lab stand-in.
    """
    return {}


def sw_block(t):
    return {"rep": 0, "dem": 0, "oth": 0, "npa": 0, "total": t, "rep_pct": None,
            "dem_pct": None, "npa_pct": None, "oth_pct": None, "margin": None}


def main():
    force = "--force" in sys.argv
    cfg = load(CONFIG_PATH)

    rows = fetch_counties(cfg)
    if not rows:   # no official feed -> UF Election Lab stand-in (the SoS's own daily county counts)
        import lab_standin as LAB
        if LAB.run(STATE, cfg, GEO_PATH, LATEST_PATH, HISTORY_PATH, partisan=False, force=force):
            try:
                add_registration()
            except Exception as e:  # noqa: BLE001 - turnout % is extra; never sink the update
                print("ar: registration not added (%s)" % str(e)[:120], file=sys.stderr)
            return 0
    counties_out = {}
    reg_total = cast_total = 0
    for name, r in rows.items():
        ev = int(r.get("cast_total") or 0)
        reg = int(r.get("registered") or 0)
        blk = sw_block(ev)
        counties_out[name] = {"fips": r["fips"], "cast": blk, "early_voted": dict(blk),
                              "registered": reg,
                              "turnout_pct": (round(100.0 * ev / reg, 2) if reg else None)}
        reg_total += reg
        cast_total += ev

    statewide = {"cast": sw_block(cast_total), "early_voted": sw_block(cast_total),
                 "registered": reg_total,
                 "turnout_pct": (round(100.0 * cast_total / reg_total, 2) if reg_total else None)}
    snap = {
        "state": STATE, "state_name": cfg["state_name"], "election": cfg["election"],
        "partisan": False,
        "source": {"primary": "Arkansas ballot/turnout data (turnout-only)"},
        "source_compiled": "", "source_compiled_iso": (C.utc_now_iso() if cast_total else ""),
        "methods_present": (["early_voted"] if cast_total else []),
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
                f.write(json.dumps({"generated_at": snap["generated_at"], "cast": cast_total,
                                    "registered": reg_total, "data_hash": snap["data_hash"]},
                                   separators=(",", ":")) + "\n")
        print("CHANGED  counties=%d  cast=%s  turnout=%s%%"
              % (len(counties_out), cast_total, statewide["turnout_pct"]))
    else:
        print("NOCHANGE  (hash %s)" % (prev or "")[:12])
    return 0


if __name__ == "__main__":
    sys.exit(main())
