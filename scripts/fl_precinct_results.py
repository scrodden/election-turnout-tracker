#!/usr/bin/env python3
"""Florida 2026 Senate / Governor results by precinct, matched to the precinct map (assets/fl-precincts.geojson).

Source: each county's VR Systems election night reporting site (enr.electionsfl.org/<CODE>/<id>/; ids in
config/fl_enr_ids.json). A race's "Precinct Details" page lists every precinct's votes per candidate. The race's page
id is found once from the county's Summary page and cached in config/fl_enr_ids.json "races".

Precinct names on the results site are matched to the map's precinct ids per county (exact, then leading zeros
dropped, then the map's split suffix '12.1' -> '12' summed). A county's figures are kept only when its precincts add
up to its Florida Election Watch totals; results the map can't place are counted under `unmatched`.

Writes data/fl/precinct_results.json:
  {updated, races, counties:{CODE:{name, source, precincts:{map id:{sen:[rep,dem,other], gov:[...]}},
   unmatched:{sen:[...], gov:[...]}, total_ok:{sen,gov}}}, missing:{County: why}}
Self-throttled (about hourly); a county is re-read only when its Election Watch totals have changed.

Run:  python scripts/fl_precinct_results.py [--force] [--only CODE,CODE]
"""
import difflib
import html
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

DATA_DIR = os.path.join(ROOT, "data", "fl")
OUT_PATH = os.path.join(DATA_DIR, "precinct_results.json")
IDS_PATH = os.path.join(ROOT, "config", "fl_enr_ids.json")
GEO_PATH = os.path.join(ROOT, "assets", "fl-precincts.geojson")
TURNOUT_PATH = os.path.join(DATA_DIR, "precincts_all.json")   # TQV ballots cast per precinct: fields[1] = cast
ENR = "https://enr.electionsfl.org/"
TQV = "https://s3.amazonaws.com/turnoutquickview.electionsfl.org/data/FL/"
RACE_RX = (("sen", re.compile(r"^(United States Senator|U\.?\s?S\.? Senat)", re.I)), ("gov", re.compile(r"^Gov(ernor)?\b", re.I)))


def load(p, default=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def text(h):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h)).strip()


def race_pages(code, eid):
    """{sen: page id, gov: page id} from the county's Summary page (each race's Precinct Details link)."""
    s = C.http_get("%s%s/%d/Summary/" % (ENR, code, eid), no_cache=True, timeout=60)
    out = {}
    for m in re.finditer(r'<div class="row RaceName">.*?<label>([^<]+)</label>', s, re.S):
        title = m.group(1).strip()
        key = next((k for k, rx in RACE_RX if rx.search(title)), None)
        if key and key not in out:
            nxt = re.search(r'/%s/%d/Precincts/(\d+)' % (code, eid), s[m.end():m.end() + 8000])
            if nxt:
                out[key] = int(nxt.group(1))
    return out


def precinct_votes(code, eid, page):
    """{precinct name: [rep, dem, other]} from a race's Precinct Details page (its graphical rows: one per choice).
    Big counties (Miami-Dade, Orange, Hillsborough) first ask for a precinct range; "All Precincts" is the last one."""
    url = "%s%s/%d/Precincts/%d/" % (ENR, code, eid, page)
    s = C.http_get(url, no_cache=True, timeout=90)
    if 'class="PrecinctName"' not in s:
        sel = re.search(r'<select[^>]*id="precinctSelect"[^>]*>(.*?)</select>', s, re.S)
        ranges = re.findall(r'<option[^>]*value="(\d+/\d+)"', sel.group(1)) if sel else []
        if ranges:
            s = C.http_get(url + ranges[-1] + "/", no_cache=True, timeout=240)
    out = {}
    for blk in re.split(r'<div id="[^"]*" class="Race row">', s)[1:]:
        m = re.search(r'<span class="PrecinctName">(.*?)</span>', blk, re.S)
        g = blk.find('class="row GraphicalResults"')
        if not m or g < 0:
            continue
        name = html.unescape(text(m.group(1)))
        v = [0, 0, 0]
        for row in re.findall(r'<div class="row ResultsRow">(.*?)(?=<div class="row ResultsRow">|$)', blk[g:], re.S):
            who = re.search(r'class="ChoiceColumn[^"]*">(.*?)</div>', row, re.S)
            cols = re.findall(r'class="ResultsColumn[^"]*"[^>]*>(.*?)</div>', row, re.S)
            if not who or not cols:
                continue
            party = (re.search(r"\((\w+)\)\s*$", text(who.group(1))) or [None, ""])[1].upper()
            n = int(re.sub(r"[^\d]", "", text(cols[-1])) or 0)
            v[0 if party == "REP" else 1 if party == "DEM" else 2] += n
        out[name] = [a + b for a, b in zip(out.get(name, [0, 0, 0]), v)]
    return out


# ---- matching results-site precinct names to the map's ids ----
def norm(s):
    s = re.sub(r"(?i)^(precinct|pct\.?|prec\.?)\s*", "", str(s).strip()).upper()
    return re.sub(r"\b0+(\d)", r"\1", s)


def candidates(name, loc=None):
    """Keys a results-site precinct name might match on the map: the name, a number at its start ('101 - Harold Ave
    Regional Park', 'PCT101', '001 Seminole Community Church'), one at its end ('Baxter 4A'), and the precincts its
    polling place serves per the county's turnout feed (Gadsden names results by polling place only)."""
    n = name.strip()
    out = [n]
    n = re.sub(r"[^\w.]+$", "", n)
    m = re.match(r"(?i)^(?:pct|precinct|prec)?\.?\s*#?\s*(\d+(?:[.\-]\d+)?[A-Z]?)\b", n)
    if m:
        out.append(m.group(1))
    m = re.search(r"\b(\d+[A-Z]?)$", n)
    if m:
        out.append(m.group(1))
    if loc:
        key = place_key(n)
        hit = loc.get(key)
        if hit is None and key:   # 'Centenary Church' vs the feed's 'Centenary United Methodist Church'
            best = max(loc, key=lambda k: place_score(key, k), default=None)
            hit = loc[best] if best and place_score(key, best) >= 0.6 else []
        out.extend(hit or [])
    return out


GENERIC = {"CHURCH", "CENTER", "CENTRE", "COMMUNITY", "THE", "OF", "BUILDING", "HALL", "REC", "RECREATION", "BAPTIST",
           "METHODIST", "UNITED", "FIRST", "FIRE", "STATION", "LIBRARY", "PUBLIC", "SCHOOL", "ELEMENTARY", "PARK", "AND"}


def place_key(n):
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]", " ", html.unescape(n).upper())).strip()


def place_score(a, b):
    """How alike two polling-place names are: shared distinctive words count most ('Centenary'), then spelling."""
    ta, tb = set(a.split()) - GENERIC, set(b.split()) - GENERIC
    words = len(ta & tb) / max(1, min(len(ta), len(tb))) if ta and tb else 0
    return 0.7 * words + 0.3 * difflib.SequenceMatcher(None, a, b).ratio()


def tqv_locations(code):
    """{polling place, upper case: [precincts]} from the county's TQV feed (PrecinctSplit: split -> Precinct, Location)."""
    eid = (load(os.path.join(DATA_DIR, "_tqv_ids.json"), {}) or {}).get(code)
    if not eid:
        return {}
    d = json.loads(C.http_get("%s%s/%s/data.json" % (TQV, code, eid), no_cache=True, timeout=60))
    names = {str(k).strip(): (v or {}).get("Name", "") for k, v in ((d.get("Location") or {}).get("ElectionDay") or {}).items()}
    out = {}
    for v in (d.get("PrecinctSplit") or {}).values():
        lid, p = str(v.get("Location", "")).strip(), str(v.get("Precinct", "")).strip()
        for key in {place_key(names.get(lid, "")), place_key(lid)} - {""}:
            if p and p not in out.setdefault(key, []):
                out[key].append(p)
    return out


def match(results, shapes, loc=None):
    """results {name: [r,d,o]}, shapes [map ids] -> ({map id: [r,d,o]}, [r,d,o] unplaced, {map id: [map ids sharing
    its result]}, count of names unplaced). Tries each candidates() key as is,
    without leading zeros, then as the base of the map's split ids ('12' -> '12.0', '12.1', ...: votes go to the
    first, which the page draws with its splits merged) and the reverse (results '12.1' -> map '12')."""
    exact = {s: s for s in shapes}
    loose = {}
    for s in shapes:
        loose.setdefault(norm(s), s)
    base = {}
    for s in shapes:
        b = re.split(r"[.\-]", norm(s))[0]
        base.setdefault(b, s)
    def find(c):
        k = exact.get(c) or loose.get(norm(c)) or base.get(norm(c)) or loose.get(re.split(r"[.\-]", norm(c))[0])
        m = None if k else re.match(r"^(\d+)[A-Z]\d*$", norm(c))   # sub-precincts: 5B, 07A1 -> 5, 7
        return k or (m and (loose.get(m.group(1)) or base.get(m.group(1))))
    got, un, shared, nun = {}, [0, 0, 0], {}, 0
    for name, v in results.items():
        cands = candidates(name, loc)
        n_named = len(candidates(name))
        k = next((find(c) for c in cands[:n_named] if find(c)), None)
        keys = [k] if k else list(dict.fromkeys(x for x in (find(c) for c in cands[n_named:]) if x))
        if keys:   # a polling place's result covers every precinct it serves
            got[keys[0]] = [a + b for a, b in zip(got.get(keys[0], [0, 0, 0]), v)]
            if len(keys) > 1:
                shared[keys[0]] = sorted(set(shared.get(keys[0], [])) | set(keys[1:]))
        else:
            un = [a + b for a, b in zip(un, v)]
            nun += 1
    return got, un, shared, nun


def check_turnout(entry, cast):
    """Votes vs ballots cast per map precinct (cast: {map id: ballots}). A polling-place result shared by several
    precincts goes to the one precinct whose ballots fit it best when that fits better than the group; then, where the
    county's votes and ballots agree overall (its turnout feed counts every method by precinct), a precinct whose votes
    are under 0.7 or over 1.4 times its ballots is marked "chk": the county's results precincts don't line up with the
    map's there (renumbered or merged precincts, a sub-precinct missing), so it isn't colored."""
    P = entry["precincts"]

    def votes(k):
        v = P.get(k, {}).get("sen") or P.get(k, {}).get("gov")
        return sum(v) if v else 0

    def off(v, c):
        return abs(v / c - 0.97) if c else 9
    for k in [k for k, e in P.items() if e.get("with")]:
        grp = [k] + P[k]["with"]
        v, best = votes(k), min(grp, key=lambda x: off(votes(k), cast.get(x, 0)))
        if off(v, cast.get(best, 0)) < off(v, sum(cast.get(x, 0) for x in grp)):
            res = {rk: P[k][rk] for rk in ("sen", "gov") if rk in P[k]}
            for x in grp:
                P.pop(x, None)
            P[best] = res
    ratios = sorted(votes(k) / cast[k] for k, e in P.items() if not e.get("of") and cast.get(k) and votes(k) >= 50)
    med = ratios[len(ratios) // 2] if ratios else None
    if med is None or not 0.85 <= med <= 1.1:
        return med
    for k, e in P.items():
        if e.get("of") or votes(k) < 50:
            continue
        r = votes(k) / cast[k] if cast.get(k) else None   # no ballots matched: unchecked, not flagged
        if r is not None and (r < 0.7 or r > 1.4):
            e["chk"] = round(r, 2)
    return med


def main(argv):
    force = "--force" in argv
    only = None
    if "--only" in argv:
        only = set(argv[argv.index("--only") + 1].upper().split(","))
    if not force and not only and C.checked_recently(DATA_DIR, "precinct_results", 55):
        print("fl precinct results: checked under an hour ago; skipping")
        return 0
    cfg = load(IDS_PATH, {}) or {}
    ids, pages = cfg.get("ids", {}), cfg.setdefault("races", {})
    turnout = load(os.path.join(DATA_DIR, "latest.json"), {})
    totals = (load(os.path.join(DATA_DIR, "results_county.json"), {}) or {}).get("counties", {})
    turn = (load(TURNOUT_PATH, {}) or {}).get("counties", {})
    shapes = {}
    for ft in (load(GEO_PATH, {}) or {}).get("features", []):
        shapes.setdefault(ft["properties"]["code"], []).append(str(ft["properties"]["precinct"]))
    old = load(OUT_PATH, {}) or {}
    counties, missing = dict(old.get("counties") or {}), {}
    names = {c["code"]: n for n, c in turnout.get("counties", {}).items()}
    locs = {}   # county -> its TQV polling-place table, fetched only where needed

    def sig(name):
        t = totals.get(name) or {}
        return [(t.get(k) or {}).get(p) for k in ("sen", "gov") for p in ("rep", "dem", "other")]

    def one(code):
        name, eid = names[code], ids.get(code)
        if not eid:
            return code, None, "no VR Systems results site found for 2026"
        prev = counties.get(code)
        if prev and prev.get("sig") == sig(name) and not force:
            return code, prev, None   # totals unchanged since we last read it
        try:
            pg = pages.get(code) or race_pages(code, eid)
        except Exception as e:  # noqa: BLE001
            return code, prev, "summary unreadable (%s)" % str(e)[:40]
        if not pg:
            return code, None, "its results site has no precinct pages for these races"
        pages[code] = pg
        entry = {"name": name, "source": "%s%s/%d/Precincts/%d/" % (ENR, code, eid, pg.get("sen") or pg.get("gov")),
                 "precincts": {}, "unmatched": {}, "total_ok": {}, "sig": sig(name)}
        for rk, page in pg.items():
            try:
                res = precinct_votes(code, eid, page)
            except Exception as e:  # noqa: BLE001
                return code, prev, "precinct page unreadable (%s)" % str(e)[:40]
            want = totals.get(name, {}).get(rk) or {}
            r, d = sum(v[0] for v in res.values()), sum(v[1] for v in res.values())
            entry["total_ok"][rk] = (r == want.get("rep") and d == want.get("dem"))
            got, un, shared, nun = match(res, shapes.get(code, []))
            if sum(un) > 0.1 * max(1, sum(sum(v) for v in res.values())):
                try:
                    loc = locs[code] if code in locs else locs.setdefault(code, tqv_locations(code))
                except Exception:  # noqa: BLE001 - plain matching stands
                    loc = locs.setdefault(code, {})
                if loc:
                    alt = match(res, shapes.get(code, []), loc)
                    if sum(alt[1]) < sum(un):
                        got, un, shared, nun = alt
            for k, v in got.items():
                entry["precincts"].setdefault(k, {})[rk] = v
            for k, others in shared.items():   # drawn on every precinct the polling place serves
                entry["precincts"][k]["with"] = others
                for o in others:
                    entry["precincts"].setdefault(o, {})["of"] = k
            entry["unmatched"][rk] = un + [nun]
        tq = {k: [v[1], 0, 0] for k, v in (turn.get(code) or {}).items()}   # the feed's precincts, matched like results
        cast = {k: v[0] for k, v in match(tq, shapes.get(code, []))[0].items()}
        entry["turnout_ratio"] = check_turnout(entry, cast)
        return code, entry, None

    codes = sorted(c for c in names if not only or c in only)
    with ThreadPoolExecutor(max_workers=3) as pool:
        for code, entry, why in pool.map(one, codes):
            if entry:
                counties[code] = entry
            if why:
                missing[names[code]] = why
                if not entry:
                    counties.pop(code, None)
    for code in names:
        if code not in counties and names[code] not in missing:
            missing[names[code]] = (old.get("missing") or {}).get(names[code], "not read yet")
    out = {"state": "fl", "updated": C.utc_now_iso(),
           "races": {"sen": {"label": "U.S. Senate (special)"}, "gov": {"label": "Governor"}},
           "note": "Precinct results from each county's VR Systems election night reporting site (enr.electionsfl.org), "
                   "unofficial until certified, keyed by the precinct map's ids. [rep, dem, other] votes.",
           "counties": counties, "missing": dict(sorted(missing.items()))}
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    with open(IDS_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=1)
    ok = sum(1 for c in counties.values() if all(c["total_ok"].values()))
    print("fl precinct results: %d counties (%d match their county totals), %d without" % (len(counties), ok, len(missing)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
