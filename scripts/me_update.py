#!/usr/bin/env python3
"""Maine absentee ballots by county, congressional district and party.

Source: Maine Secretary of State, Bureau of Corporations, Elections &
Commissions "Voter Data" page (source.page):
  - Statewide Absentee Voter Data File (pipe-delimited text, one row per
    absentee request; file name carries the election and as-of date, e.g.
    "11-3-26 AB Voter File as of 9-29-26.txt"). Columns used: RES MUNICIPALITY,
    P (party: D, R, U = unenrolled, G, L), CG (congressional district), SS / SR /
    CC (state senate / house / county commissioner district), Status (ACT =
    returned & accepted, REJ = rejected). Footer lines ("Total Number of
    Records: n", "Total Returned & Accepted: n", ...) are checked against our
    counts.
  - Statewide Registered and Enrolled file (xlsx: COUNTY, MUNICIPALITY, W/P,
    CG, SS, SR, CC, D, G, L, R, U, TOTAL): the town -> county crosswalk and
    active registered voters (turnout %).
The absentee file has no county, so each town is placed by the Registered and
Enrolled list; unorganized townships it doesn't list are placed by
source.township_counties (verified) or, failing that, by the only county whose
towns share the row's senate / house / commissioner districts (commissioner
districts are numbered within a county).

requested = rows not rejected; mail_voted = ACT; mail_provided = requested -
accepted (outstanding) -> ballot chase. Party: D -> dem, R -> rep, U -> npa,
G / L -> oth. Also writes data/me/districts.json (by CG) for the Districts map.

Run:  python scripts/me_update.py [--force]
"""
import collections
import csv
import html
import io
import json
import os
import re
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "me"
CONFIG_PATH = os.path.join(ROOT, "config", "me.json")
GEO_PATH = os.path.join(ROOT, "assets", "me-counties.geojson")
CD_GEO_PATH = os.path.join(ROOT, "assets", "me-cd.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
DISTRICTS_PATH = os.path.join(DATA_DIR, "districts.json")
PARTY = {"D": "dem", "R": "rep", "U": "npa", "G": "oth", "L": "oth"}
REG_COLS = {"D": "dem", "R": "rep", "U": "npa", "G": "oth", "L": "oth"}


def load(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _zero():
    return {"rep": 0, "dem": 0, "oth": 0, "npa": 0}


def find_files(src):
    """-> (absentee file url, as-of text, registered & enrolled url) from the Voter Data page."""
    page = C.http_get(src["page"], no_cache=True, retries=2)
    ab = [html.unescape(h) for h in re.findall(r'href="([^"]*AB%20Voter%20File[^"]*\.txt)"', page)
          if urllib.parse.unquote(h).split("/")[-1].startswith(src["file_prefix"])]
    er = [html.unescape(h) for h in re.findall(r'href="([^"]*E%26R[^"]*ACTIVE[^"]*\.xlsx)"', page)]
    if not ab or not er:
        raise RuntimeError("ME: absentee (%s) or registration (%s) file not linked" % (len(ab), len(er)))
    ab_url, er_url = urllib.parse.urljoin(src["page"], ab[0]), urllib.parse.urljoin(src["page"], er[0])
    m = re.search(r"as of ([\d-]+)", urllib.parse.unquote(ab_url))
    return ab_url, (m.group(1) if m else ""), er_url


def read_registration(raw):
    """-> (town -> county code, (ss, sr, cc) -> {county codes}, registered by county, by CG)."""
    rows = next(iter(C.read_xlsx(raw, positional=True).values()))
    head = [c.strip().upper() for c in rows[0]]
    ix = {k: head.index(k) for k in ("COUNTY", "MUNICIPALITY", "CG", "SS", "SR", "CC", "D", "G", "L", "R", "U")}
    town, combo = {}, collections.defaultdict(set)
    reg_cty, reg_cd = collections.defaultdict(_zero), collections.defaultdict(_zero)
    for r in rows[1:]:
        g = (lambda k: (r[ix[k]] if ix[k] < len(r) else "").strip())
        if not g("COUNTY") or not g("MUNICIPALITY"):
            continue
        town[_norm(g("MUNICIPALITY"))] = g("COUNTY")
        combo[(g("SS"), g("SR"), g("CC"))].add(g("COUNTY"))
        for col, p in REG_COLS.items():
            n = C.parse_number(g(col))
            reg_cty[g("COUNTY")][p] += n
            if g("CG").isdigit():
                reg_cd[int(g("CG"))][p] += n
    return town, combo, reg_cty, reg_cd


def read_absentee(text, place):
    """Aggregate the absentee file. place(row) -> county code or None.
    -> (by county, by CG, statewide, unplaced count, footer totals)."""
    footer = {}
    lines = []
    for ln in text.splitlines():
        m = re.match(r"\s*Total ([^:]+):\s*([\d,]+)\s*$", ln)
        if m:
            footer[m.group(1).strip()] = C.parse_number(m.group(2))
        elif ln.strip():
            lines.append(ln)
    def acc():
        return {"req": _zero(), "ret": _zero()}
    cty, cd, sw = collections.defaultdict(acc), collections.defaultdict(acc), acc()
    n = accepted = rejected = unplaced = no_party = 0
    for r in csv.DictReader(io.StringIO("\n".join(lines)), delimiter="|", quoting=csv.QUOTE_NONE):
        p = (r.get("P") or "").strip()
        if not (r.get("RES MUNICIPALITY") or "").strip():
            continue
        n += 1
        if not p:
            no_party += 1
        status = (r.get("Status") or "").strip()
        if status == "REJ":
            rejected += 1
            continue
        party = PARTY.get(p, "oth")
        targets = [sw]
        code = place(r)
        if code:
            targets.append(cty[code])
        else:
            unplaced += 1
        cg = (r.get("CG") or "").strip()
        if cg.isdigit():
            targets.append(cd[int(cg)])
        for t in targets:
            t["req"][party] += 1
            if status == "ACT":
                t["ret"][party] += 1
        if status == "ACT":
            accepted += 1
    checks = {"Number of Records": n, "Returned & Accepted": accepted}
    for k, v in checks.items():
        if k in footer and footer[k] != v:
            raise RuntimeError("ME: file footer says %s = %d, rows give %d" % (k, footer[k], v))
    rej_footer = footer.get("Returned & Rejected", 0) + footer.get("Not Returned & Rejected", 0)
    if footer and rej_footer != rejected:
        raise RuntimeError("ME: file footer rejected = %d, rows give %d" % (rej_footer, rejected))
    return cty, cd, sw, unplaced, footer, (no_party < n / 2)


def entity(a, reg, partisan=True):
    """partisan=False when the state's file carries no party enrollment: blocks
    keep only totals (no 0 D / 0 R / 'Even' that would look like real data)."""
    out = {p: max(0, a["req"][p] - a["ret"][p]) for p in a["req"]}

    def pb(d):
        if not partisan:
            return {"rep": 0, "dem": 0, "oth": 0, "npa": 0, "total": sum(d.values()),
                    "rep_pct": None, "dem_pct": None, "npa_pct": None, "oth_pct": None, "margin": None}
        return C.party_block(d["rep"], d["dem"], d["oth"], d["npa"])
    e = {"mail_voted": pb(a["ret"]), "mail_provided": pb(out), "cast": pb(a["ret"])}
    total_reg = sum(reg.values()) if reg else 0
    e["registered"] = total_reg
    e["turnout_pct"] = C.pct(e["cast"]["total"], total_reg) if total_reg else None
    m = C.compute_mail(e)
    if m:
        e["mail"] = m
    return e


def main():
    force = "--force" in sys.argv
    cfg = load(CONFIG_PATH, {}) or {}
    src = cfg.get("source", {})
    prev = load(LATEST_PATH, {}) or {}
    if not force and prev.get("counties") and C.checked_recently(DATA_DIR):
        print("me: checked under an hour ago.")
        return 0
    try:
        ab_url, as_of, er_url = find_files(src)
        if not force and (prev.get("source") or {}).get("files") == [ab_url, er_url]:
            print("NOCHANGE  (Maine absentee file as of %s)" % as_of)
            return 0
        town, combo, reg_cty, reg_cd = read_registration(C.http_get(er_url, binary=True, retries=2))
        text = C.http_get(ab_url, binary=True, retries=2).decode("latin-1")
    except Exception as e:  # noqa: BLE001
        print("ME SoS files unavailable: %s" % str(e)[:160], file=sys.stderr)
        import lab_standin as LAB   # official files unreachable -> UF Election Lab stand-in (statewide)
        LAB.run(STATE, cfg, GEO_PATH, LATEST_PATH, HISTORY_PATH, partisan=True, force=force)
        return 0

    manual = {_norm(k): v for k, v in src.get("township_counties", {}).items()}

    def place(r):
        t = _norm(r.get("RES MUNICIPALITY"))
        if t in town:
            return town[t]
        if t in manual:
            return manual[t]
        cands = combo.get(((r.get("SS") or "").strip(), (r.get("SR") or "").strip(), (r.get("CC") or "").strip()), set())
        return next(iter(cands)) if len(cands) == 1 else None
    cty, cd, sw, unplaced, footer, has_party = read_absentee(text, place)

    geo = load(GEO_PATH, {"features": []})
    by_code = {f["properties"]["name"][:3].upper(): f["properties"] for f in geo["features"]}
    bad = [c for c in cty if c not in by_code]
    if bad:
        print("ME: unknown county codes %s — keeping previous snapshot." % bad, file=sys.stderr)
        return 0
    counties = {by_code[c]["name"]: dict(entity(a, reg_cty.get(c), has_party), fips=by_code[c]["fips"])
                for c, a in cty.items()}
    for c, g in by_code.items():   # counties with no requests yet
        counties.setdefault(g["name"], dict(entity({"req": _zero(), "ret": _zero()}, reg_cty.get(c), has_party),
                                            fips=g["fips"]))
    reg_all = _zero()
    for d in reg_cty.values():
        for p in d:
            reg_all[p] += d[p]
    statewide = entity(sw, reg_all, has_party)
    note = None
    if not has_party:
        note = ("The state's absentee file as of %s leaves voters' party enrollment blank, so Maine is shown as "
                "turnout only until the party column returns." % as_of)
    if unplaced:
        note = ((note + " ") if note else "") + (
            "%s absentee requests from unorganized townships couldn't be placed in a county; they're in the "
            "statewide and district totals." % format(unplaced, ","))
    body = {
        "state": STATE, "state_name": cfg.get("state_name", "Maine"), "election": cfg.get("election", {}),
        "partisan": has_party, "methods_present": [k for k in ("mail_voted", "mail_provided") if statewide[k]["total"]],
        "method_labels": cfg.get("method_labels", {}), "mail_base_label": cfg.get("mail_base_label", "Ballots requested"),
        "statewide": statewide, "counties": counties,
    }
    if note:
        body["map_note"] = note
    snap = dict(body)
    snap["source"] = {"primary": "Maine Secretary of State statewide absentee voter file (as of %s); registered voters: "
                                 "Registered and Enrolled file" % as_of,
                      "url": src.get("page"), "as_of": as_of, "files": [ab_url, er_url]}
    snap["source_compiled"] = as_of
    snap["source_compiled_iso"] = C.utc_now_iso()
    snap["data_hash"] = C.data_hash(body)
    changed = force or snap["data_hash"] != prev.get("data_hash")
    now = C.utc_now_iso()
    snap["generated_at"] = now if changed else prev.get("generated_at", now)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LATEST_PATH, "w", encoding="utf-8") as f:
        json.dump(snap, f, separators=(",", ":"))
    c = statewide["cast"]
    if changed and c["total"]:
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"generated_at": now, "statewide": {"cast": [c["rep"], c["dem"], c["oth"], c["npa"],
                                                                            c["total"]]}}, separators=(",", ":")) + "\n")
    write_districts(cfg, cd, reg_cd, statewide, as_of, force, has_party, note if not has_party else None)
    m = statewide.get("mail") or {}
    print("%s  ME absentee file as of %s: %d counties | returned=%d of %d requested (R%d D%d U%d O%d) margin=%s%s"
          % ("CHANGED" if changed else "NOCHANGE", as_of, len(counties), c["total"], m.get("requested", 0),
             c["rep"], c["dem"], c["npa"], c["oth"], c["margin"], (" | unplaced %d" % unplaced) if unplaced else ""))
    return 0


def write_districts(cfg, cd, reg_cd, statewide, as_of, force, has_party=True, note=None):
    geo = load(CD_GEO_PATH, {"features": []})
    gidx = {f["properties"]["district_number"]: f["properties"] for f in geo["features"]}
    if not gidx or any(k not in gidx for k in cd):
        print("ME: district file skipped (districts %s vs map %s)" % (sorted(cd), sorted(gidx)), file=sys.stderr)
        return
    units = {gidx[k]["name"]: dict(entity(cd.get(k, {"req": _zero(), "ret": _zero()}), reg_cd.get(k), has_party),
                                   fips=gidx[k]["fips"])
             for k in sorted(gidx)}
    body = {"state": STATE, "election": cfg.get("election", {}), "unit_label": "District", "unit_label_plural": "Districts",
            "partisan": has_party, "source": "Maine Secretary of State statewide absentee voter file, by each voter's "
                                        "congressional district (as of %s)" % as_of,
            "methods_present": [k for k in ("mail_voted", "mail_provided") if statewide[k]["total"]],
            "method_labels": cfg.get("method_labels", {}),
            "coverage": {"ok": len(gidx), "total": len(gidx), "unmatched": []},
            "as_of": as_of, "statewide": statewide, "counties": units}
    if note:
        body["map_note"] = note
    prev = load(DISTRICTS_PATH, {}) or {}
    h = C.data_hash(body)
    if h == prev.get("data_hash") and not force:
        return
    now = C.utc_now_iso()
    with open(DISTRICTS_PATH, "w", encoding="utf-8") as f:
        json.dump(dict(body, source_compiled=as_of, source_compiled_iso=now, data_hash=h, generated_at=now), f,
                  separators=(",", ":"))


if __name__ == "__main__":
    sys.exit(main())
