#!/usr/bin/env python3
"""Florida Election Day ballots by congressional district, merged with 305 Data Girl's mail and early votes.

TQV doesn't report party by precinct on Election Day, only by polling place (Turnout.LocationParty.ElectionDay),
along with each precinct split's Election Day total and the polling place it votes at. So each polling place's
check-ins by party go to the district(s) of the precincts it serves (assets/crosswalks/fl-precinct-cd.json): all of
them when its precincts lie in one district; split by its precincts' Election Day totals (same party mix assumed)
when they straddle a line. Counties with no polling-place party data are estimated: Broward (off TQV; its own
dashboard has Election Day by party countywide and by precinct without party) and any county whose Election Day
comes from outside TQV (Lake, config/fl_manual_ed.json). Their countywide party totals are spread over their
districts in proportion to each district's early in-person party mix (iterative proportional fitting, so the
county's party totals and each district's ballot total both hold).

fl_update.py calls build() on every run, after it writes the county data, and it writes
data/fl/districts_cd_live.json: 305 Data Girl's district tables (data/fl/districts_cd.json, mail and early in person
through the prior day) plus Election Day, which the site's Congressional view reads (assets/states.json).
"""
import json
import os
import re
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
import common as C  # noqa: E402

DATA_DIR = os.path.join(ROOT, "data", "fl")
XWALK_PATH = os.path.join(ROOT, "assets", "crosswalks", "fl-precinct-cd.json")
DG_PATH = os.path.join(DATA_DIR, "districts_cd.json")
OUT_PATH = os.path.join(DATA_DIR, "districts_cd_live.json")
BRO_LOC_PATH = os.path.join(DATA_DIR, "broward_eday_locations.json")   # fl_update.py, from Broward's dashboard
PARTIES = ("rep", "dem", "oth", "npa")
PARTY_MAP = {"REP": "rep", "DEM": "dem", "NPA": "npa"}


def load(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _keys(p):
    """Ways one precinct id can be written: ' 1A' / '001' / '101C' (TQV) vs '1' / '101A' (boundary file)."""
    a = str(p).strip().upper()
    b = a.lstrip("0") or "0"
    return [a, b, re.sub(r"[A-Z]+$", "", b) or b]


class Crosswalk:
    def __init__(self, doc):
        self.idx = {}
        for code, precs in (doc or {}).get("counties", {}).items():
            m, loose = {}, defaultdict(set)
            for pid, sh in precs.items():
                if pid == "_county":
                    continue
                k = _keys(pid)
                m.setdefault(k[0], sh); m.setdefault(k[1], sh)
                loose[k[2]].add(pid)
            for k, pids in loose.items():   # TQV '103' -> boundary '103A' (Volusia); '103A'..'103C' -> their average
                if k not in m:
                    avg = defaultdict(float)
                    for pid in pids:
                        for d, f in precs[pid].items():
                            avg[d] += f / len(pids)
                    m[k] = dict(avg)
            self.idx[code] = (m, precs.get("_county") or {})

    def shares(self, code, prec):
        m = self.idx.get(code, ({}, {}))[0]
        for k in _keys(prec):
            if k in m:
                return m[k]
        return None

    def county(self, code):
        return self.idx.get(code, ({}, {}))[1]


def rake(rows, cols, seed, iters=60):
    """Iterative proportional fitting: cells x[r][c] with row sums `rows` and column sums `cols`, starting from
    rows[r] * seed[r][c]. -> {r: {c: x}}"""
    x = {r: {c: rows[r] * max(seed.get(r, {}).get(c, 0.0), 1e-6) for c in cols} for r in rows}
    for _ in range(iters):
        for c in cols:
            s = sum(x[r][c] for r in rows)
            if s:
                for r in rows:
                    x[r][c] *= cols[c] / s
        for r in rows:
            s = sum(x[r].values())
            if s:
                for c in cols:
                    x[r][c] *= rows[r] / s
    return x


def early_mix(dg):
    """Each district's early in-person party shares (305 Data Girl), the seed for the estimated counties."""
    out = {}
    for name, ent in (dg.get("counties") or {}).items():
        b = ent.get("early_voted") or ent.get("cast") or {}
        t = sum(b.get(p, 0) for p in PARTIES)
        if t:
            out[name[2:] if name.startswith("CD") else name] = {p: b.get(p, 0) / t for p in PARTIES}
    return out


def broward_as_tqv():
    """Broward's dashboard (Election Day by party per polling place, with its precincts; precinct Election Day totals)
    in the shape fl_update gives TQV counties, so it takes the same polling-place path. -> dict or None"""
    locs = load(BRO_LOC_PATH)
    prec = (load(os.path.join(DATA_DIR, "precincts", "BRO.json"), {}) or {}).get("precincts") or []
    if not locs or not locs.get("rows"):
        return None
    return {"last_updated": locs.get("asof", ""),
            "eday_loc": {r["location"]: {"DEM": r["dem"], "REP": r["rep"], "NPA": r["npa"], "OTH": r["oth"]}
                         for r in locs["rows"]},
            "split_loc": [(p, p, r["location"]) for r in locs["rows"] for p in r["precincts"]],
            "precincts": prec}


def build(results, counties, counties_out, log=print):
    """results: {county name: (tqv, eid, note)} from fl_update (tqv carries eday_loc / split_loc / precincts).
    Writes OUT_PATH when it changed. -> statewide Election Day total by district, or None."""
    xw = Crosswalk(load(XWALK_PATH))
    dg = load(DG_PATH)
    if not xw.idx or not dg or not dg.get("counties"):
        log("  fl CD Election Day: crosswalk or 305 Data Girl district file missing; skipped")
        return None
    seed = early_mix(dg)
    bro = broward_as_tqv()
    if bro:
        results = dict(results, Broward=(bro, None, "dashboard"))
    cd = defaultdict(lambda: defaultdict(float))   # district -> party -> ballots
    est = defaultdict(float)                        # district -> ballots that were estimated
    stamp, n_exact, n_split, n_loose, n_est = "", 0.0, 0.0, 0.0, 0.0
    rake_jobs = []

    for county in counties:
        code, name = county["code"], county["name"]
        tqv = (results.get(name) or (None,))[0]
        ent = counties_out.get(name) or {}
        ed_party = {p: (ent.get("election_day") or {}).get(p, 0) for p in PARTIES}
        ed_total = sum(ed_party.values())
        loc_party = (tqv or {}).get("eday_loc") or {}
        tqv_ed = sum(sum(v.values()) for v in loc_party.values())
        if tqv and tqv.get("last_updated", "") > stamp and tqv_ed:
            stamp = tqv["last_updated"]
        if not tqv or tqv_ed < 0.5 * ed_total:
            if ed_total:   # Broward (no TQV) or a county whose Election Day comes from its own site (Lake)
                rake_jobs.append((code, name, tqv, ed_party))
            continue
        split_ed = {str(r["precinct"]).strip(): (r.get("election_day") or 0, r.get("eligible") or 0) for r in tqv["precincts"]}
        by_loc = defaultdict(list)
        for sid, prec, loc in tqv["split_loc"]:
            by_loc[str(loc)].append((sid, prec))
        # the county's matched Election Day spread, for polling places none of whose precincts match
        fallback = defaultdict(float)
        for sid, prec, _loc in tqv["split_loc"]:
            sh = xw.shares(code, prec)
            for d, f in (sh or {}).items():
                fallback[d] += (split_ed.get(sid, (0, 0))[0] or 0.001) * f
        cty = xw.county(code)
        for loc, parties in loc_party.items():
            n = sum(parties.values())
            if not n:
                continue
            w, w_elig, matched = defaultdict(float), defaultdict(float), True
            for sid, prec in by_loc.get(loc, []):
                sh = xw.shares(code, prec)
                if not sh:
                    matched = False
                    continue
                ed, elig = split_ed.get(sid, (0, 0))
                for d, f in sh.items():
                    w[d] += ed * f
                    w_elig[d] += elig * f
            w = w if sum(w.values()) else w_elig   # precinct totals a step behind the polling place's
            exact = matched and w and max(w.values()) >= 0.95 * sum(w.values())
            if not w:
                exact = len(cty) == 1
                w = dict(cty) if exact else dict(fallback)
            W = sum(w.values())
            if not W:
                continue
            for d, x in w.items():
                for pc, k in parties.items():
                    cd[d][PARTY_MAP.get(pc, "oth")] += k * x / W
                if not exact:
                    est[d] += n * x / W
            if exact:
                n_exact += n
            elif matched:
                n_split += n
            else:
                n_loose += n

    # estimated counties: countywide party totals spread over districts by each district's early in-person mix
    estimated = [name for _c, name, _t, _p in rake_jobs]
    for code, name, tqv, ed_party in rake_jobs:
        tot = sum(ed_party.values())
        rows = defaultdict(float)
        if code == "BRO":   # Broward's own precinct table: Election Day ballots by precinct, no party
            for r in (load(os.path.join(DATA_DIR, "precincts", "BRO.json"), {}) or {}).get("precincts", []):
                for d, f in (xw.shares(code, r["precinct"]) or {}).items():
                    rows[d] += (r.get("election_day") or 0) * f
        if not sum(rows.values()) and tqv:   # no precinct Election Day: registered voters by district
            for r in tqv["precincts"]:
                prec = next((p for s, p, _l in tqv["split_loc"] if s == str(r["precinct"]).strip()), r["precinct"])
                for d, f in (xw.shares(code, prec) or {}).items():
                    rows[d] += (r.get("eligible") or 0) * f
        if not sum(rows.values()):
            rows = defaultdict(float, xw.county(code))
        s = sum(rows.values())
        if not s:
            continue
        rows = {d: v * tot / s for d, v in rows.items() if v}
        cells = rake(rows, ed_party, seed)
        for d, ps in cells.items():
            for p, v in ps.items():
                cd[d][p] += v
            est[d] += rows[d]
        n_est += tot
        log("  fl CD Election Day: %s estimated over CD %s" % (name, ", ".join(sorted(rows, key=int))))

    total = n_exact + n_split + n_loose + n_est
    if not total:
        return None
    blocks = {d: C.party_block(*(int(round(cd[d][p])) for p in PARTIES), compiled=stamp, compiled_iso=stamp) for d in cd}
    write_live(dg, blocks, est, stamp, {"exact": n_exact, "split": n_split, "unmatched": n_loose, "estimated": n_est},
               estimated)
    log("  fl CD Election Day: %d ballots in %d districts (%.0f%% at polling places in one district, %.0f%% split, "
        "%.0f%% unmatched, %.0f%% estimated counties)" % (total, len(blocks), 100 * n_exact / total, 100 * n_split / total,
                                                          100 * n_loose / total, 100 * n_est / total))
    return blocks


def write_live(dg, blocks, est, stamp, mix, estimated):
    total = sum(mix.values())
    many = len(estimated) > 1
    est_note = ("" if not estimated else " %s, which report%s Election Day party only countywide, %s estimated: %s party "
                "totals are spread over %s districts in line with each district's early-vote party mix (%.0f%%)." % (
                    " and ".join(estimated), "" if many else "s", "are" if many else "is", "their" if many else "its",
                    "their" if many else "its", 100 * mix["estimated"] / total))
    doc = json.loads(json.dumps(dg))
    for k in ("data_hash", "generated_at"):
        doc.pop(k, None)
    for name, ent in doc["counties"].items():
        b = blocks.get(name[2:] if name.startswith("CD") else name)
        if not b:
            continue
        ent["election_day"] = b
        ent["cast"] = C.add_blocks(ent.get("cast"), b)
        d_est = est.get(name[2:] if name.startswith("CD") else name, 0)
        ent["election_day_estimated_pct"] = round(100 * d_est / b["total"], 1) if b["total"] else None
    sw = doc["statewide"]
    sw["election_day"] = C.add_blocks(*blocks.values())
    sw["cast"] = C.add_blocks(sw.get("cast"), sw["election_day"])
    doc["methods_present"] = list(dg.get("methods_present") or []) + ["election_day"]
    doc["method_labels"] = dict(dg.get("method_labels") or {}, election_day="Election Day (by polling place)",
                                cast="Ballots cast (mail + early through the prior day, plus Election Day)")
    doc["party_hints"] = dict(dg.get("party_hints") or {}, election_day=(
        "Party registration of Election Day voters by district. Counties report Election Day check-ins by party for each "
        "polling place, not each precinct, so each polling place's count goes to the district of the precincts it serves "
        "(%.0f%% of ballots); where its precincts straddle a district line it's split by their Election Day totals, assuming "
        "the same party mix (%.0f%%). Polling places serving only precincts newer than the boundary file are spread over "
        "their county's districts (%.0f%%).%s Updated every 10 minutes from the county TQV feeds and Broward's turnout "
        "dashboard." % (100 * mix["exact"] / total, 100 * mix["split"] / total, 100 * mix["unmatched"] / total, est_note)))
    doc["map_note"] = ((dg.get("map_note") or "") + " Election Day: check-ins by party at each polling place (county TQV "
                       "feeds; Broward's turnout dashboard), assigned to districts by the precincts it serves%s." % (
                           "; %s estimated (see the Election Day tab's note)" % " and ".join(estimated) if estimated else "")).strip()
    doc["source"] = ((dg.get("source") or "") + "; Election Day from the county Turnout Quick View feeds and Broward's "
                     "turnout dashboard (by polling place)")
    doc["election_day_mix"] = {k: int(round(v)) for k, v in mix.items()}
    doc["election_day_as_of"] = stamp
    doc["data_hash"] = C.data_hash(doc)
    old = load(OUT_PATH, {})
    if old.get("data_hash") == doc["data_hash"]:
        return False
    doc["generated_at"] = C.utc_now_iso()
    with open(OUT_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, separators=(",", ":"))
    return True
