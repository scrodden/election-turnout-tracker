#!/usr/bin/env python3
"""Build data/results/history/<office>_<year>.json -- past statewide results
for the results page (and, later, 2026-vs-prior comparisons; race ids match
the 2026 files: pres-ST, sen-ST[-special], gov-ST).

  President 2020, 2024      MIT Election Data + Science Lab 'U.S. President
                            1976-2024' (Dataverse doi:10.7910/DVN/42MVDX), CC0
  U.S. Senate 2018-2024     MIT 'U.S. Senate statewide 1976-2024'
                            (doi:10.7910/DVN/PEJ5QU), CC0 -- general results,
                            with runoffs (GA 2020 -> Jan 2021, GA 2022) as the
                            deciding round
  Governor 2018/2022/2024   MIT official precinct returns (GitHub, CC0),
                            summed statewide -- used only for states whose
                            precinct file reproduces that year's clean
                            Senate or presidential statewide result within
                            0.5% (or, with no such race, whose leading D and
                            R candidates match official totals recorded in
                            the manual file's verify_top2 within 0.2%);
                            other states come from official state results
                            in config/results_history_manual.json
  Governor 2020 + off-cycle config/results_history_manual.json (official
                            state results, each with its source URL)

Candidates on several party lines (NY fusion) are combined; write-ins are
grouped. Run (uses the turnout-history precinct cache if given):
  python scripts/build_results_history.py [--cache DIR]
"""
import csv
import io
import json
import os
import re
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402
import build_turnout_history as TH  # noqa: E402

OUT_DIR = os.path.join(ROOT, "data", "results", "history")
MANUAL = os.path.join(ROOT, "config", "results_history_manual.json")
DATAVERSE = {"president": ("doi:10.7910/DVN/42MVDX", "1976-2024-president.csv"),
             "senate": ("doi:10.7910/DVN/PEJ5QU", "1976-2024-senate-state.tab")}
NAMES = {}


def dataverse_rows(kind):
    doi, fname = DATAVERSE[kind]
    meta = json.loads(C.http_get("https://dataverse.harvard.edu/api/datasets/:persistentId/?persistentId=" + doi,
                                 retries=2, timeout=60))
    fid = next(f["dataFile"]["id"] for f in meta["data"]["latestVersion"]["files"] if f["dataFile"]["filename"] == fname)
    raw = C.http_get("https://dataverse.harvard.edu/api/access/datafile/%d?format=original" % fid,
                     binary=True, retries=2, timeout=180)
    txt = raw.decode("utf-8-sig", "replace")
    delim = "\t" if txt[:500].count("\t") > txt[:500].count(",") else ","
    return list(csv.DictReader(io.StringIO(txt), delimiter=delim))


SUFFIX = re.compile(r"^(JR|SR|II|III|IV|V)\.?$", re.I)


def title(name):
    """'BIDEN, JOSEPH R. JR' -> 'Joseph R. Biden Jr.'; 'DONALD J. TRUMP' -> 'Donald J. Trump'."""
    n = re.sub(r"\s+", " ", (name or "").strip())
    if n.count(",") == 1:
        last, rest = [x.strip() for x in n.split(",")]
        words = rest.split(" ")
        suf = [w for w in words if SUFFIX.match(w)]
        first = " ".join(w for w in words if not SUFFIX.match(w))
        if first and not SUFFIX.match(rest):   # 'KING, JR.' style (no first name) stays as is
            n = first + " " + last + ("" if not suf else " " + suf[0])
    initials = lambda w: len(w) == 2 and w.isupper() and not set(w) & set("AEIOUY") and w not in ("DR", "MR", "MS", "ST")
    out = " ".join(w if initials(w) else   # 'JD Vance'
                   w.capitalize() if (w.isupper() or w.islower()) and not SUFFIX.match(w) else
                   (w.upper().rstrip(".") + ("." if w.upper().rstrip(".") in ("JR", "SR") else "") if SUFFIX.match(w) else w)
                   for w in n.split(" "))
    out = re.sub(r"\b(Jr|Sr)\b(?!\.)", r"\1.", out.replace("JR.", "Jr.").replace("SR.", "Sr.").replace("JR", "Jr").replace("SR", "Sr"))
    return re.sub(r"\bMc(\w)", lambda m: "Mc" + m.group(1).upper(), out)


def party_label(detailed, simplified):
    d = (detailed or "").strip().upper()
    s = (simplified or "").strip().upper()
    if "DEMOCRAT" in d or s == "DEMOCRAT":
        return "Democratic"
    if "REPUBLICAN" in d or s == "REPUBLICAN":
        return "Republican"
    if d.startswith("LIBERTARIAN") or s == "LIBERTARIAN":
        return "Libertarian"
    if d in ("INDEPENDENT", "NO PARTY AFFILIATION", "NONPARTISAN", "UNAFFILIATED") or not d:
        return "Independent" if d else "Other"
    short = {"G": "Green", "GRN": "Green", "PSL": "Party for Socialism and Liberation", "OTHER CANDIDATES": "Other",
             "US TAXPAYERS": "U.S. Taxpayers", "US TAXPAYERS PARTY": "U.S. Taxpayers"}
    return short.get(d, d.title().replace(" And ", " and ").replace(" For ", " for ").replace(" Of ", " of ").replace(" The ", " the "))


NOT_A_CANDIDATE = re.compile(r"(?i)^(blank( votes?| ballots?)?|spoiled( votes?| ballots?)?|over ?votes?|under ?votes?|"
                             r"undervotes?|overvotes?|invalid( votes?)?|rejected( ballots?)?|total( votes| ballots)?( cast)?|"
                             r"ballots cast|registered voters|times (blank|over|under) voted|blank write-?ins?)$")


def merge_candidates(rows):
    """rows: [(name, party, votes, writein)] -> candidate list, fusion lines combined."""
    acc, parties, write = {}, defaultdict(set), 0
    for name, party, votes, writein in rows:
        if TH.PSEUDO.match((name or "").strip()) or NOT_A_CANDIDATE.match((name or "").strip()):
            continue   # over / under / blank votes etc. are not candidates
        if (writein and (not (name or "").strip() or party == "Other")) or re.match(r"(?i)^(write[- ]?ins?|scatter(ing)?)$", (name or "").strip()):
            write += votes   # a named candidate with a party isn't a write-in even if flagged (DC 2020)
            continue
        key = re.sub(r"[^A-Z]", "", (name or "").upper())
        if not key:
            continue
        acc[key] = acc.get(key, (name, 0))[0], acc.get(key, (name, 0))[1] + votes
        parties[key].add(party)
    out = []
    for key, (name, votes) in acc.items():
        ps = parties[key]
        party = "Democratic" if "Democratic" in ps else "Republican" if "Republican" in ps else sorted(ps)[0]
        out.append({"name": title(name), "party": party, "votes": int(votes)})
    if write:
        out.append({"name": "Write-ins", "party": "Other", "votes": int(write)})
    return sorted(out, key=lambda c: -c["votes"])


def race(office, st, cands, year, special=False, source="", runoff=None, note=""):
    tot = sum(c["votes"] for c in cands)
    deciding = runoff["candidates"] if runoff else cands
    win = max(deciding, key=lambda c: c["votes"]) if deciding else None
    r = {"id": {"President": "pres", "U.S. Senate": "sen", "Governor": "gov"}[office] + "-" + st + ("-special" if special else ""),
         "office": office, "state": st, "state_name": NAMES.get(st, st), "special": special, "year": year,
         "candidates": deciding, "total_votes": sum(c["votes"] for c in deciding), "reporting_pct": 100,
         "called": (win["party"] or "O")[0].upper() if win else None, "winner": win["name"] if win else None,
         "source": source}
    if runoff:
        r["first_round"] = {"candidates": cands, "total_votes": tot}
        r["runoff_note"] = runoff.get("note", "Decided in a runoff")
    if note:
        r["note"] = note
    return r


FIXES = {}


def load_fixes():
    m = json.load(open(MANUAL, encoding="utf-8")) if os.path.exists(MANUAL) else {}
    for f in m.get("party_fixes", []):
        FIXES[(str(f["year"]), f["office"], f["state"], re.sub(r"[^A-Z]", "", f["candidate"].upper()))] = f["party"]


def fixed_party(year, office, st, cand, party):
    return FIXES.get((str(year), office, st, re.sub(r"[^A-Z]", "", (cand or "").upper())), party)


def build_president(rows):
    out = defaultdict(list)
    by = defaultdict(list)
    for r in rows:
        if r["year"] not in ("2020", "2024"):
            continue
        NAMES.setdefault(r["state_po"], r["state"].title().replace(" Of ", " of "))
        by[(r["year"], r["state_po"])].append((r["candidate"], fixed_party(r["year"], "president", r["state_po"], r["candidate"],
                                                                     party_label(r["party_detailed"], r["party_simplified"])),
                                               int(float(r["candidatevotes"] or 0)), r.get("writein") == "TRUE" or r.get("writein") == "True"))
    for (y, st), rs in sorted(by.items()):
        out[y].append(race("President", st, merge_candidates(rs), int(y),
                           source="MIT Election Data + Science Lab, U.S. President 1976-2024 (CC0)"))
    return out


def build_senate(rows):
    out = defaultdict(list)
    by = defaultdict(list)
    for r in rows:
        y = r["year"]
        stg = (r.get("stage") or "GEN").upper()
        if y not in ("2018", "2020", "2021", "2022", "2024") or not ("GEN" in stg or "RUNOFF" in stg) or "PRI" in stg:
            continue
        if (r.get("mode") or "TOTAL").upper() != "TOTAL":
            continue
        NAMES.setdefault(r["state_po"], r["state"].title().replace(" Of ", " of "))
        special = str(r.get("special")).upper() == "TRUE"
        stage = "runoff" if "RUNOFF" in stg else "gen"
        yr = "2020" if y == "2021" else y   # GA's 2020 races went to Jan 5, 2021 runoffs
        by[(yr, r["state_po"], special, stage)].append(
            (r["candidate"], fixed_party(yr, "senate", r["state_po"], r["candidate"],
                                         party_label(r["party_detailed"], r["party_simplified"])),
             int(float(r["candidatevotes"] or 0)), str(r.get("writein")).upper() == "TRUE"))
    for (y, st, special, stage), rs in sorted(by.items()):
        if stage != "gen":
            continue
        ro = by.get((y, st, special, "runoff"))
        runoff = {"candidates": merge_candidates(ro), "note": "Decided in a runoff" + (" (Jan. 5, 2021)" if y == "2020" else "")} if ro else None
        out[y].append(race("U.S. Senate", st, merge_candidates(rs), int(y), special,
                           "MIT Election Data + Science Lab, U.S. Senate statewide 1976-2024 (CC0)", runoff))
    return out


def precinct_statewide(raw):
    """Statewide candidate totals per race from a precinct zip, de-duplicated
    the same way as the turnout history (TOTAL rows win; blank-party
    duplicates dropped). -> {race: [(name, party, votes, writein)]}"""
    cell = defaultdict(lambda: [0, 0])
    meta = {}
    for r in TH.precinct_rows(raw):
        if (r.get("stage") or "GEN").upper() not in ("GEN", ""):
            continue
        office = TH.office_key(r.get("office"))
        if office not in ("GOVERNOR", "US SENATE", "US PRESIDENT"):
            continue
        cand = (r.get("candidate") or "").strip()
        if TH.PSEUDO.match(cand):
            continue
        try:
            v = int(float(r.get("votes") or 0))
        except ValueError:
            continue
        special = str(r.get("special") or "").upper() in ("TRUE", "1", "T", "YES")
        rc = office + (" (SPECIAL)" if special else "")
        party = (r.get("party_detailed") or r.get("party") or "").strip()
        key = (rc, r.get("county_name") or r.get("county") or "", r.get("precinct") or "", cand.upper(),
               (r.get("mode") or "").upper())
        cell[key][0 if party else 1] += v
        if party:   # every line a candidate appears on (fusion: DEM + WFP ...)
            m = meta.setdefault((rc, cand.upper()), [set(), False])
            m[0].add(party_label(party, r.get("party_simplified")))
            m[1] = m[1] or str(r.get("writein") or "").upper() == "TRUE"
    prec = defaultdict(lambda: defaultdict(lambda: [0, 0, False]))
    for (rc, cty, p, cand, mode), (pv, bv) in cell.items():
        a = prec[(rc, cty, p)][cand]
        if mode == "TOTAL":
            a[0] += pv or bv
            a[2] = True
        else:
            a[1] += pv or bv
    totals = defaultdict(lambda: defaultdict(int))
    for (rc, cty, p), cands in prec.items():
        has_total = any(a[2] for a in cands.values())
        for cand, a in cands.items():
            totals[rc][cand] += a[0] if has_total else a[1]
    def label(ps):
        return "Democratic" if "Democratic" in ps else "Republican" if "Republican" in ps else sorted(ps)[0]
    return {rc: [(c, label(meta[(rc, c)][0]) if (rc, c) in meta else "Other", v,
                  meta[(rc, c)][1] if (rc, c) in meta else False)
                 for c, v in cs.items()] for rc, cs in totals.items()}


def top_by_party(clist):
    out = {}
    for c in clist:
        k = (c["party"] or "O")[0].upper()
        out[k] = max(out.get(k, 0), c["votes"])
    return out


def matches(cands, clean_race):
    """The leading Democratic and Republican candidates (fusion lines combined;
    or, without both, the overall leader) agree with the clean statewide file
    within 0.5%. Per-candidate rather than per-party sums, so a minor candidate
    filed under a major-party label in one file doesn't fail the check."""
    clean = top_by_party(clean_race["candidates"])
    mine = top_by_party(merge_candidates(cands))
    keys = [k for k in ("D", "R") if clean.get(k)] or [max(clean, key=clean.get)]
    return all(mine.get(k) and abs(mine[k] - clean[k]) <= 0.005 * clean[k] for k in keys)


def build_governor_precinct(cache, senate, president, verify):
    """Precinct-summed governors, kept only when the same file reproduces that
    year's clean Senate/president result, or (no such race) when the leading D
    and R governor candidates match official totals entered in the manual
    file's verify_top2 (within 0.2%). Disagreements are printed as CHECK lines
    so a clean-file error (e.g. PA 2018 Senate) can be looked into."""
    out, rejected = defaultdict(list), defaultdict(list)
    for y in ("2018", "2022", "2024"):
        sen = {(r["state"], r["special"]): r for r in senate.get(y, [])}
        pres = {r["state"]: r for r in president.get(y, [])}
        for st in sorted(NAMES):
            try:
                raw = TH.fetch_zip(TH.MEDSL[y].format(st=st.lower()), cache)
            except Exception:  # noqa: BLE001
                continue
            races = precinct_statewide(raw)
            del raw
            gov = races.get("GOVERNOR")
            if not gov:
                continue
            check, note, cands = None, "", merge_candidates(gov)
            src = "MIT Election Data + Science Lab official precinct returns, summed statewide (CC0)"
            if (st, False) in sen and "US SENATE" in races:   # a runoff race is checked against its first round
                ref, kind = sen[(st, False)].get("first_round") or sen[(st, False)], "US SENATE"
            elif st in pres and "US PRESIDENT" in races:
                ref, kind = pres[st], "US PRESIDENT"
            else:
                ref, kind = None, None
            if ref:
                check = matches(races[kind], ref)
                if not check:
                    print("CHECK %s %s %s precinct %s clean %s" % (y, st, kind, top_by_party(merge_candidates(races[kind])),
                                                                   top_by_party(ref["candidates"])))
            else:
                v = verify.get("%s|%s" % (y, st))
                mine = top_by_party(cands)
                check = bool(v) and all(mine.get(k) and abs(mine[k] - v[k]) <= 0.002 * v[k] for k in ("D", "R") if k in v)
                if v:
                    src += "; leading candidates checked against the official totals"
                    note = "Precinct-summed totals; the leading candidates were checked against the official results (%s)." % v["source"]
                if not check:
                    print("CHECK %s %s GOVERNOR (no Senate/president race) precinct %s official %s" % (y, st, mine, v))
            if check:
                out[y].append(race("Governor", st, cands, int(y), source=src, note=note))
            else:
                rejected[y].append(st)
    return out, rejected


RV80FW = "doi:10.7910/DVN/RV80FW"   # US 2020 General Official Election Results in Tabular Format (CC0)
GOV2020 = ("DE", "IN", "MO", "MT", "NH", "NC", "ND", "UT", "VT", "WA", "WV")
_MIDTERM_GOV = ("AL AK AZ AR CA CO CT FL GA HI ID IL IA KS ME MD MA MI MN NE NV NH NM NY OH OK OR PA RI SC SD TN TX "
                "VT WI WY").split()
GOV_RACES = {"2018": _MIDTERM_GOV, "2020": GOV2020, "2022": _MIDTERM_GOV, "2024": GOV2020}   # regular November races


def rv80fw_contest(rows, pattern):
    """Statewide totals for one contest from the 2020 official tabular file:
    the shallowest reporting level present (state, else counties), 'total'
    vote type where given, else the sum of the types."""
    sel = [r for r in rows if re.search(pattern, r.get("Contest") or "", re.I)]
    if not sel:
        return None
    depth = min((r.get("ReportingUnit") or "").count(";") for r in sel)
    cell = defaultdict(lambda: {"total": 0, "types": 0, "has_total": False})
    party = {}
    for r in sel:
        if (r.get("ReportingUnit") or "").count(";") != depth:
            continue
        try:
            v = int(float(r.get("Count") or 0))
        except ValueError:
            continue
        a = cell[(r.get("Selection"), r.get("ReportingUnit"))]
        if (r.get("VoteType") or "").lower() == "total":
            a["total"] += v
            a["has_total"] = True
        else:
            a["types"] += v
        party[r.get("Selection")] = r.get("Party") or ""
    tot = defaultdict(int)
    for (name, unit), a in cell.items():
        tot[name] += a["total"] if a["has_total"] else a["types"]
    out = []
    for name, v in tot.items():
        if TH.PSEUDO.match((name or "").strip()):
            continue
        p = party.get(name, "")
        out.append((name, party_label(p.replace(" Party", ""), "") if p else "Other", v,
                    bool(re.search(r"write", name or "", re.I))))
    return out


def build_governor_2020(president):
    meta = json.loads(C.http_get("https://dataverse.harvard.edu/api/datasets/:persistentId/?persistentId=" + RV80FW,
                                 retries=2, timeout=60))
    fid = {f["dataFile"]["filename"]: f["dataFile"]["id"] for f in meta["data"]["latestVersion"]["files"]}
    pres = {r["state"]: r for r in president.get("2020", [])}
    out, rejected = [], []
    for st in GOV2020:
        try:
            raw = C.http_get("https://dataverse.harvard.edu/api/access/datafile/%d?format=original" % fid[st + "_tabular.tab"],
                             binary=True, retries=2, timeout=180).decode("utf-8-sig", "replace")
        except Exception:  # noqa: BLE001
            rejected.append(st)
            continue
        delim = "\t" if raw[:500].count("\t") > raw[:500].count(",") else ","
        rows = list(csv.DictReader(io.StringIO(raw), delimiter=delim))
        gov = rv80fw_contest(rows, r"^(?!.*\bLieutenant\b)(?!.*\bLt\.?\s).*\bGovernor\b")
        prz = rv80fw_contest(rows, r"President")
        if gov and prz and st in pres and matches(prz, pres[st]):
            out.append(race("Governor", st, merge_candidates(gov), 2020,
                            source="US 2020 General Official Election Results in Tabular Format (Harvard Dataverse "
                                   "doi:10.7910/DVN/RV80FW, CC0)"))
        else:
            rejected.append(st)
    return out, rejected


def main():
    args = sys.argv[1:]
    cache = args[args.index("--cache") + 1] if "--cache" in args else None
    os.makedirs(OUT_DIR, exist_ok=True)
    load_fixes()
    president = build_president(dataverse_rows("president"))
    senate = build_senate(dataverse_rows("senate"))
    mfile = json.load(open(MANUAL, encoding="utf-8")) if os.path.exists(MANUAL) else {}
    for y, races in (mfile.get("senate") or {}).items():   # official replacements for a MIT race
        for r in races:
            built = race("U.S. Senate", r["state"], sorted(r["candidates"], key=lambda c: -c["votes"]), int(y),
                         r.get("special", False), r["source"], note=r.get("note", ""))
            senate[y] = [x for x in senate.get(y, []) if not (x["state"] == r["state"] and x["special"] == r.get("special", False))] + [built]
    if "--reuse-governor" in args:   # keep this run's precinct-validated governor files
        governor, rejected = defaultdict(list), defaultdict(list)
        for y in ("2018", "2022", "2024"):
            p = os.path.join(OUT_DIR, "governor_%s.json" % y)
            if os.path.exists(p):
                d = json.load(open(p, encoding="utf-8"))
                governor[y] = [r for r in d["races"] if "precinct returns" in r.get("source", "")]
                for r in governor[y]:
                    NAMES.setdefault(r["state"], r["state_name"])
                    r["candidates"] = [c for c in r["candidates"] if not NOT_A_CANDIDATE.match(c["name"].strip())]
                    r["total_votes"] = sum(c["votes"] for c in r["candidates"])
                m = re.search(r"included \(no verified source\): (.*)$", d.get("missing_note") or "")
                rejected[y] = [x.strip() for x in m.group(1).split(",")] if m else []
    else:
        governor, rejected = build_governor_precinct(cache, senate, president, mfile.get("verify_top2", {}))
    g20, rej20 = build_governor_2020(president)
    governor["2020"] = g20
    rejected["2020"] = rej20
    manual = mfile.get("governor", {})
    offcycle = {}
    for key, races in manual.items():   # "2020", "offcycle_latest", "offcycle_previous", or a year to fill rejected states
        if key.startswith("_"):
            continue
        for r in races:
            NAMES.setdefault(r["state"], r.get("state_name", r["state"]))
            built = race("Governor", r["state"], sorted(r["candidates"], key=lambda c: -c["votes"]), int(r["year"]),
                         source=r["source"], runoff=r.get("runoff"), note=r.get("note", ""))
            if key.startswith("offcycle"):
                offcycle.setdefault(key, []).append(built)
            else:
                governor[key] = [x for x in governor.get(key, []) if x["state"] != r["state"]] + [built]
    for key, names in (mfile.get("name_fixes") or {}).items():   # "governor|2018|TX": {"Abbott": "Greg Abbott"}
        if key.startswith("_"):
            continue
        off, y, st = key.split("|")
        for r in {"president": president, "senate": senate, "governor": governor}[off].get(y, []):
            if r["state"] == st:
                for c in r["candidates"] + ((r.get("first_round") or {}).get("candidates") or []):
                    c["name"] = names.get(c["name"], c["name"])
                r["winner"] = names.get(r["winner"], r["winner"])
    files = []
    for office, data in (("president", president), ("senate", senate), ("governor", governor)):
        for y, races in sorted(data.items()):
            label = {"president": "President", "senate": "U.S. Senate", "governor": "Governor"}[office]
            gap = sorted((set(GOV_RACES.get(y, ())) | set(rejected.get(y, []))) - {r["state"] for r in races}) \
                if office == "governor" else []
            doc = {"office": label, "year": int(y), "election": {"year": int(y), "name": "%s General" % y},
                   "races": sorted(races, key=lambda r: (r["state_name"], r["special"])),
                   "missing_note": ("Not yet included (no verified source): " + ", ".join(gap)) if gap else ""}
            p = os.path.join(OUT_DIR, "%s_%s.json" % (office, y))
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                json.dump(doc, f, separators=(",", ":"))
            files.append("%s_%s: %d races%s" % (office, y, len(races), (" | " + doc["missing_note"]) if doc["missing_note"] else ""))
    for key, races in offcycle.items():
        gap = sorted({"KY", "LA", "MS", "NJ", "VA"} - {r["state"] for r in races})
        doc = {"office": "Governor", "offcycle": key, "election": {"name": "Off-cycle governor elections"},
               "races": sorted(races, key=lambda r: r["state_name"]),
               "missing_note": ("Not yet included (no verified source): " + ", ".join(gap)) if gap else ""}
        with open(os.path.join(OUT_DIR, "governor_%s.json" % key), "w", encoding="utf-8", newline="\n") as f:
            json.dump(doc, f, separators=(",", ":"))
        files.append("governor_%s: %s" % (key, ", ".join("%s %d" % (r["state"], r["year"]) for r in doc["races"])))
    print("\n".join(files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
