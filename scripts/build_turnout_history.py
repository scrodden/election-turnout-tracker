#!/usr/bin/env python3
"""Build data/<st>/turnout_history.json -- final turnout in the 2018, 2020,
2022 and 2024 general elections, statewide and (where it can be matched to
the state's map) by county, so the site can show how far this year's early
vote has come relative to each past final total.

Statewide (all four years): UF Election Lab (M. McDonald) TOTAL_BALLOTS_COUNTED
-- official ballots counted as reported by states / to the EAC --
Turnout_1980_2022_v1.1.csv and Turnout_2024G_v0.4.csv, CC BY 4.0. Where a figure
is the Lab's rounded estimate (states that don't report total ballots, e.g.
AL, MS, MO, OK, PA, TN, TX in 2024, KS in 2018), the votes cast for the top
race in the official precinct returns are used instead and labelled so (2020
has no precinct file here, so a 2020 estimate stays, labelled as one).

County: MIT Election Data + Science Lab official precinct returns on GitHub
(2018-elections-official, 2022-elections-official, 2024-elections-official;
CC0), totalled by county for the state's top race that year (president; in
midterms the statewide race -- U.S. Senate or governor -- with the most votes,
else all U.S. House districts together). Overvote/undervote/registration
pseudo-candidates are excluded; where a county reports both a TOTAL row and
per-method rows, only TOTAL is used. These are votes for one race, a little
below total ballots cast. Counties are kept only if at least 90% of the map's
units match (New England states mapped by town use jurisdiction names) AND
the county totals add up to 93-101% of the official statewide count (a guard
against duplicate or missing rows in a state's precinct file). 2020
county totals: MIT's county presidential file sits behind a Dataverse
guestbook form, so it's read only if a local copy is supplied
(--countypres path/to/countypres_2000-2024.tab).

Heavy (about 330 MB of zipped precinct files, processed in memory); run once
or when a source is revised:
  python scripts/build_turnout_history.py [--states fl,ga] [--cache DIR] [--countypres FILE]
"""
import csv
import io
import json
import os
import re
import sys
import unicodedata
import zipfile
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

csv.field_size_limit(10 ** 8)
LAB = "https://election.lab.ufl.edu/data-downloads/turnoutdata/"
LAB_FILES = {"hist": "Turnout_1980_2022_v1.1.csv", "2024": "Turnout_2024G_v0.4.csv"}
MEDSL = {
    "2018": "https://raw.githubusercontent.com/MEDSL/2018-elections-official/master/individual_states/2018-{st}-precinct-general.zip",
    "2022": "https://raw.githubusercontent.com/MEDSL/2022-elections-official/main/individual_states/2022-{st}-local-precinct-general.zip",
    "2024": "https://raw.githubusercontent.com/MEDSL/2024-elections-official/main/individual_states/{st}24.zip",
}
YEARS = ("2018", "2020", "2022", "2024")
TOP_OFFICES = ("US PRESIDENT", "US SENATE", "GOVERNOR")
PSEUDO = re.compile(r"^(over ?votes?|under ?votes?|blank|blanks|registered voters|ballots cast|total votes?|total|"
                    r"times (over|under) voted|write-in totals?|scattering|none of these candidates|void)$", re.I)


def norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(county|parish|borough|census area|municipality|city and borough)\b", "", s)
    s = s.replace("saint ", "st ").replace("ste. ", "ste ").replace("st. ", "st ")
    return re.sub(r"[^a-z0-9]", "", s)


def lab_totals():
    out = defaultdict(dict)   # st -> year -> (n, basis)
    hist = list(csv.DictReader(io.StringIO(C.http_get(LAB + LAB_FILES["hist"], retries=2, timeout=120).lstrip("﻿"))))
    for r in hist:
        st, y = (r.get("STATE_ABV") or "").strip().lower(), r.get("YEAR")
        n = C.parse_number(r.get("TOTAL_BALLOTS_COUNTED") or "")
        if st and y in YEARS and n:   # a round thousand = the Lab's estimate for a state that doesn't report it
            out[st][y] = (n, "estimate" if n % 1000 == 0 else "total ballots counted")
    rows = list(csv.DictReader(io.StringIO(C.http_get(LAB + LAB_FILES["2024"], retries=2, timeout=120).lstrip("﻿"))))
    for r in rows:
        st = (r.get("STATE_ABV") or "").strip().lower()
        n = C.parse_number(r.get("TOTAL_BALLOTS_COUNTED") or "")
        if st and st != "us" and n:
            rounded = n % 1000 == 0
            out[st]["2024"] = (n, "estimate" if rounded else "total ballots counted")
    return out


def fetch_zip(url, cache):
    name = url.rsplit("/", 1)[-1]
    path = os.path.join(cache, name) if cache else None
    if path and os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    raw = C.http_get(url, binary=True, retries=2, timeout=300)
    if path:
        with open(path, "wb") as f:
            f.write(raw)
    return raw


def precinct_rows(raw):
    z = zipfile.ZipFile(io.BytesIO(raw))
    for name in z.namelist():
        if name.startswith("__MACOSX") or not name.lower().endswith((".csv", ".tab", ".txt")):
            continue
        with z.open(name) as f:
            head = f.read(4096).decode("utf-8", "replace")
        delim = "\t" if head.count("\t") > head.count(",") else ","
        with z.open(name) as f:
            for r in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", errors="replace"), delimiter=delim):
                yield {(k or "").strip().lower(): v for k, v in r.items()}


def office_key(raw):
    """MEDSL standardises offices, but some files don't (WI 2018: 'Governor /
    Lt. Governor', 'US Senate')."""
    o = re.sub(r"\s+", " ", (raw or "").upper().replace(".", "")).strip()
    if re.match(r"^GOVERNOR(\s*(/|&|AND)\s*(LT|LIEUTENANT)\b.*)?$", o):   # not 'GOVERNOR'S COUNCIL' (MA)
        return "GOVERNOR"
    if o in ("US SENATE", "UNITED STATES SENATOR", "US SENATOR"):
        return "US SENATE"
    if o in ("US HOUSE", "US REPRESENTATIVE", "UNITED STATES REPRESENTATIVE", "US REP"):
        return "US HOUSE"
    if o.startswith("US PRESIDENT") or o.startswith("PRESIDENT"):
        return "US PRESIDENT"
    return o


def race_label(race):
    """'US SENATE (SPECIAL)' -> 'U.S. Senate (special)' for the 'votes for ...' basis text."""
    if not race:
        return "the top race"
    base, special = race.replace(" (SPECIAL)", ""), " (SPECIAL)" in race
    label = {"US PRESIDENT": "president", "US HOUSE": "U.S. House (all districts)", "US SENATE": "U.S. Senate",
             "GOVERNOR": "Governor"}.get(base, base.title())
    return label + (" (special)" if special else "")


UNIT_FIELDS = (("county_name", "county"), ("jurisdiction_name", "jurisdiction"))


def county_totals(rows, unit_fields=UNIT_FIELDS):
    """One streaming pass -> {unit_field: (race, {unit: votes}, statewide votes)}
    for the top race (the race with the most votes statewide).

    De-duplication at the precinct level: a precinct reporting a TOTAL row for
    a race uses only that, else its per-method rows; and where a candidate
    has rows with a party AND rows with a blank party in the same precinct
    and method (VT, and NY's fusion lines), the blank-party rows are dropped."""
    # (race, unit_by_field..., precinct, candidate, mode) -> [party-row votes, blank-row votes]
    cell = defaultdict(lambda: [0, 0])
    fields = [names[0] for names in unit_fields]
    for r in rows:
        stage = (r.get("stage") or "GEN").upper()
        if stage not in ("GEN", ""):
            continue
        office = office_key(r.get("office"))
        if office not in TOP_OFFICES and office != "US HOUSE":
            continue
        cand = (r.get("candidate") or "").strip()
        if PSEUDO.match(cand):
            continue
        try:
            v = int(float(r.get("votes") or 0))
        except ValueError:
            continue
        special = str(r.get("special") or "").upper() in ("TRUE", "1", "T", "YES")
        race = "US HOUSE" if office == "US HOUSE" else office + (" (SPECIAL)" if special else "")
        units = tuple(next((r.get(n) for n in names if r.get(n)), "") for names in unit_fields)
        party = (r.get("party_detailed") or r.get("party") or "").strip()
        key = (race, units, r.get("precinct") or "", cand.upper(), (r.get("mode") or "").upper())
        cell[key][0 if party else 1] += v
    # precinct level: candidate votes (party rows win over blank rows), TOTAL mode wins over methods
    prec = defaultdict(lambda: [0, 0, False])   # (race, units, precinct) -> [total-mode, methods, has TOTAL]
    for (race, units, p, cand, mode), (pv, bv) in cell.items():
        v = pv if pv else bv
        a = prec[(race, units, p)]
        if mode == "TOTAL":
            a[0] += v
            a[2] = True
        else:
            a[1] += v
    out = {}
    for i, field in enumerate(fields):
        by_race = defaultdict(lambda: defaultdict(int))
        for (race, units, p), a in prec.items():
            if units[i]:
                by_race[race][units[i]] += a[0] if a[2] else a[1]
        if by_race:
            race = max(by_race, key=lambda k: sum(by_race[k].values()))
            out[field] = (race, dict(by_race[race]), sum(by_race[race].values()))
    return out


def geo_names(st):
    reg = json.load(open(os.path.join(ROOT, "assets", "states.json"), encoding="utf-8"))
    lst = reg["states"] if isinstance(reg, dict) else reg
    e = next((x for x in lst if x.get("code") == st), None)
    if not e or not e.get("geojson"):
        return {}
    g = json.load(open(os.path.join(ROOT, *e["geojson"].split("/")), encoding="utf-8"))
    return {norm(f["properties"]["name"]): f["properties"]["name"] for f in g["features"]}


# election authorities that aren't counties -> the county holding most of them
FOLD = {"mo": {"kansascity": "jackson"}}


def match_units(totals, geo, st=""):
    out, unmatched = {}, []
    for unit, v in totals.items():
        k = norm(unit)
        k = FOLD.get(st, {}).get(k, k)
        if k in geo:
            out[geo[k]] = out.get(geo[k], 0) + v
        else:
            unmatched.append(unit)
    return out, unmatched


def countypres_2020(path):
    out = defaultdict(lambda: defaultdict(lambda: {"total": 0, "modes": 0, "has_total": False}))
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        head = f.readline()
        f.seek(0)
        delim = "\t" if head.count("\t") >= head.count(",") else ","   # Dataverse 'tab' export or the original CSV
        for r in csv.DictReader(f, delimiter=delim):
            r = {(k or "").strip().strip('"').lower(): (v or "").strip().strip('"') for k, v in r.items()}
            if r.get("year") != "2020":
                continue
            st = (r.get("state_po") or "").lower()
            try:
                v = int(float(r.get("candidatevotes") or 0))
            except ValueError:
                continue
            a = out[st][r.get("county_name") or ""]
            if (r.get("mode") or "").upper() == "TOTAL":
                a["total"] += v
                a["has_total"] = True
            else:
                a["modes"] += v
    return {st: {c: (a["total"] if a["has_total"] else a["modes"]) for c, a in d.items()} for st, d in out.items()}


def main():
    args = sys.argv[1:]
    only = set(args[args.index("--states") + 1].split(",")) if "--states" in args else None
    cache = args[args.index("--cache") + 1] if "--cache" in args else None
    cpres = args[args.index("--countypres") + 1] if "--countypres" in args else None
    if cache:
        os.makedirs(cache, exist_ok=True)
    reg = json.load(open(os.path.join(ROOT, "assets", "states.json"), encoding="utf-8"))
    codes = [x["code"] for x in (reg["states"] if isinstance(reg, dict) else reg)]
    lab = lab_totals()
    pres20 = countypres_2020(cpres) if cpres and os.path.exists(cpres) else {}
    summary = []
    for st in codes:
        if only and st not in only:
            continue
        geo = geo_names(st)
        doc = {"state": st, "years": {}, "counties": {}, "county_basis": {}, "notes": []}
        for y in YEARS:
            race, units, sw_votes = None, {}, 0
            if y in MEDSL:
                try:
                    raw = fetch_zip(MEDSL[y].format(st=st), cache)
                    best = None
                    for field, (r_, u_, s_) in county_totals(precinct_rows(raw)).items():
                        m_, un_ = match_units(u_, geo, st)
                        cover = len(m_) / len(geo) if geo else 0
                        if not best or cover > best[3]:
                            best = (r_, m_, s_, cover, un_)
                    del raw
                    if best:
                        race, units, sw_votes, cover, unmatched = best
                        if cover < 0.9 and len(geo) - len(units) > 1:   # allow one missing unit (HI: Kalawao)
                            doc["notes"].append("%s: county totals not used (%.0f%% of map units matched; e.g. %s)"
                                                % (y, 100 * cover, unmatched[:3]))
                            units = {}
                except Exception as e:  # noqa: BLE001
                    doc["notes"].append("%s: precinct returns unavailable (%s)" % (y, str(e)[:80]))
            elif y == "2020" and st in pres20:
                race = "US PRESIDENT"
                units, unmatched = match_units(pres20[st], geo, st)
                sw_votes = sum(pres20[st].values())
                if geo and len(units) / len(geo) < 0.9:
                    doc["notes"].append("2020: county totals not used (%d of %d matched)" % (len(units), len(geo)))
                    units = {}
            lab_n, basis = lab.get(st, {}).get(y, (0, ""))
            if basis == "estimate" and sw_votes:
                total, basis = sw_votes, "votes for %s" % race_label(race)
            elif lab_n:
                total = lab_n
                if basis == "estimate":
                    basis = "UF Election Lab estimate (the state doesn't report total ballots)"
            elif sw_votes:
                total, basis = sw_votes, "votes for %s" % race_label(race)
            else:
                total = 0
            if total:
                doc["years"][y] = {"total": int(total), "basis": basis,
                                   "source": "UF Election Lab turnout data (CC BY 4.0)" if basis.startswith(("total ballots", "UF Election Lab"))
                                   else "MIT Election Data + Science Lab official returns (CC0)"}
            if units and total:   # county totals must add up to the official statewide count
                ratio = sum(units.values()) / float(total)
                if not 0.93 <= ratio <= 1.01:
                    doc["notes"].append("%s: county totals not used (they sum to %.0f%% of the statewide %s)"
                                        % (y, 100 * ratio, basis))
                    units = {}
            if units:
                doc["county_basis"][y] = "votes for %s" % race_label(race)
                for name, v in units.items():
                    doc["counties"].setdefault(name, {})[y] = int(v)
        if not doc["years"]:
            continue
        d = os.path.join(ROOT, "data", st)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "turnout_history.json"), "w", encoding="utf-8", newline="\n") as f:
            json.dump(doc, f, separators=(",", ":"))
        summary.append("%s %s counties[%s]%s" % (
            st, " ".join("%s:%s" % (y, format(v["total"], ",")) for y, v in sorted(doc["years"].items())),
            ",".join(sorted(doc["county_basis"])), (" NOTE " + "; ".join(doc["notes"])) if doc["notes"] else ""))
        print(summary[-1], flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
