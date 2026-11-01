#!/usr/bin/env python3
"""Georgia early voters by last party primary, from the Secretary of State's voter files.

Georgia has no party registration, so like Texas (scripts/tx_rosters.py) party here means the last party primary each
voter took part in. Two official files, joined on voter registration number:

  * Voter History Files (mvp.sos.ga.gov/s/voter-history-files): every primary voter, with the party ballot they took,
    however they voted. Each file is one election.
  * Voter Absentee Files (mvp.sos.ga.gov/s/voter-absentee-files), the general's STATEWIDE.csv: everyone who has voted
    early or by mail, with county, method, congressional district and date. Ballot Status "A" = accepted.

Absentee files alone wouldn't do for the primaries: only 49.8% of May 2026's primary voters voted early or by mail.

Georgia's download page has a reCAPTCHA, so a person downloads the files; this is never scripted. Zips named A-*.zip in
Downloads are picked up by their column headers and copied to LOCAL_DIR, so a fresher absentee file or another primary
is folded in on the next run (local_ga_reader runs this every ~20 minutes). The newest absentee file wins; every
history file counts.

Voter-level files stay in LOCAL_DIR, outside the repo. Only totals are written: data/ga/roster_party.json (per county
and district: voters by party and method, plus the statewide day-by-day series) and data/ga/party_share.json.
ga_update.py folds them into the page. A voter whose last primary ballot was nonpartisan counts as no party primary.

Run:  python scripts/ga_rosters.py [--force]
"""
import csv
import glob
import io
import json
import os
import re
import shutil
import sys
import zipfile
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

LOCAL_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects", "Politics", "ga-rosters")
DOWNLOADS = os.path.join(os.path.expanduser("~"), "Downloads")
OUT_PATH = os.path.join(ROOT, "data", "ga", "roster_party.json")
SHARE_PATH = os.path.join(ROOT, "data", "ga", "party_share.json")
GEO_PATH = os.path.join(ROOT, "assets", "ga-counties.geojson")
ELECTION_DAY = "2026-11-03"
HISTORY_URL = "https://mvp.sos.ga.gov/s/voter-history-files"
PARTY = {"DEMOCRAT": 1, "DEMOCRATIC": 1, "REPUBLICAN": 2}   # anything else (NON-PARTISAN, blank) carries no party
csv.field_size_limit(10 ** 7)


def kind_of(path):
    """(kind, member names) of a zip by its columns: "history", "absentee" or None."""
    try:
        with zipfile.ZipFile(path) as z:
            names = [i.filename for i in z.infolist() if i.filename.lower().endswith(".csv")]
            pick = next((n for n in names if n.upper() == "STATEWIDE.CSV"), names[0] if names else None)
            if not pick:
                return None, None
            with z.open(pick) as f:
                head = f.read(2000).decode("utf-8", "replace").splitlines()[0]
    except (zipfile.BadZipFile, OSError, IndexError):
        return None, None
    if "Voter Registration Number" in head and "Election Date" in head and "Party" in head:
        return "history", pick
    if "Voter Registration #" in head and "Ballot Status" in head:
        return "absentee", pick
    return None, None


def collect():
    """Copy new voter-file zips from Downloads into LOCAL_DIR. -> list of (path, kind, member)."""
    os.makedirs(LOCAL_DIR, exist_ok=True)
    for src in glob.glob(os.path.join(DOWNLOADS, "A-*.zip")):
        dst = os.path.join(LOCAL_DIR, os.path.basename(src))
        if not os.path.exists(dst) and kind_of(src)[0]:
            shutil.copy2(src, dst)
    out = []
    for p in sorted(glob.glob(os.path.join(LOCAL_DIR, "*.zip"))):
        k, m = kind_of(p)
        if k:
            out.append((p, k, m))
    return out


def rows_of(path, member):
    with zipfile.ZipFile(path) as z, z.open(member) as f:
        for r in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", errors="replace", newline="")):
            yield r


def read_history(files):
    """-> ({voter number: 1 Democrat or 2 Republican, from their latest party primary}, {election label: voters})."""
    latest, counts = {}, {}
    for path, _k, member in files:
        for r in rows_of(path, member):
            try:
                d = datetime.strptime(r["Election Date"], "%m/%d/%Y").strftime("%Y%m%d")
            except ValueError:
                continue
            label = d[:4] + "-" + d[4:6]
            c = counts.setdefault(label, {"dem": 0, "rep": 0, "other": 0})
            p = PARTY.get((r.get("Party") or "").strip().upper())
            c["dem" if p == 1 else "rep" if p == 2 else "other"] += 1
            if not p:
                continue
            v = r["Voter Registration Number"].strip().lstrip("0")
            n = int(d) * 10 + p   # date first, so the later ballot wins
            if n > latest.get(v, 0):
                latest[v] = n
    return {v: n % 10 for v, n in latest.items()}, counts


def blank():
    return {"voters": 0, "dem": 0, "rep": 0, "none": 0}


def add(b, key):
    b["voters"] += 1
    b[key] += 1


def county_names():
    with open(GEO_PATH, encoding="utf-8") as f:
        return {re.sub(r"[^A-Z]", "", ft["properties"]["name"].upper()): ft["properties"]["name"] for ft in json.load(f)["features"]}


def build(party_of, counts, absentee):
    names = county_names()
    path, _k, member = absentee
    counties, districts, days = {}, {}, {}
    seen, unmatched = set(), {}
    dup = bad_cd = bad_date = 0
    newest = ""
    today = date.today().isoformat()
    for r in rows_of(path, member):
        if r.get("Ballot Status") != "A":
            continue
        v = r["Voter Registration #"].strip().lstrip("0")
        if v in seen:   # a voter counts once
            dup += 1
            continue
        seen.add(v)
        name = names.get(re.sub(r"[^A-Z]", "", (r.get("County") or "").upper()))
        if not name:
            unmatched[r.get("County")] = unmatched.get(r.get("County"), 0) + 1
            continue
        p = party_of.get(v)
        key = "dem" if p == 1 else "rep" if p == 2 else "none"
        method = "inp" if (r.get("Ballot Style") or "").upper().startswith("EARLY") else "mail"
        c = counties.setdefault(name, dict(blank(), mail=blank(), inp=blank(), cd={}))
        add(c, key)
        add(c[method], key)
        m = re.sub(r"\D", "", r.get("CNG") or "")
        cd = str(int(m)) if m and 1 <= int(m) <= 14 else None
        if cd:
            for bucket in (c["cd"].setdefault(cd, dict(blank(), mail=blank(), inp=blank())), districts.setdefault(cd, dict(blank(), mail=blank(), inp=blank()))):
                add(bucket, key)
                add(bucket[method], key)
        else:
            bad_cd += 1
        try:
            d = datetime.strptime(r.get("Ballot Return Date") or "", "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            d = ""
        if "2026-09-01" <= d <= today:   # the file has a few mistyped years (2027, 2029)
            add(days.setdefault(d, blank()), key)
            newest = max(newest, d)
        else:
            bad_date += 1
    series = {"dates": [], "voters": [], "dem": [], "rep": [], "none": []}
    run = blank()
    for d in sorted(days):
        for k in run:
            run[k] += days[d][k]
        series["dates"].append(d)
        for k in run:
            series[k].append(run[k])
    return counties, districts, series, {"voters": sum(c["voters"] for c in counties.values()), "duplicates": dup, "no_district": bad_cd, "undated": bad_date,
                                         "through": newest, "unmatched_counties": unmatched}


def main():
    force = "--force" in sys.argv
    files = collect()
    history = [f for f in files if f[1] == "history"]
    absentee = [f for f in files if f[1] == "absentee"]
    if not history or not absentee:
        print("ga_rosters: need at least one history file and the absentee file in %s (history %d, absentee %d)" % (LOCAL_DIR, len(history), len(absentee)))
        return 0
    newest = max(absentee, key=lambda f: zipfile.ZipFile(f[0]).getinfo(f[2]).date_time)
    stamp = "|".join(sorted("%s:%d" % (os.path.basename(f[0]), os.path.getsize(f[0])) for f in history + [newest]))
    try:
        with open(OUT_PATH, encoding="utf-8") as f:
            prev = json.load(f)
    except (OSError, ValueError):
        prev = {}
    if prev.get("files") == stamp and not force:
        print("ga_rosters: no new files")
        return 0
    party_of, counts = read_history(history)
    counties, districts, series, stats = build(party_of, counts, newest)
    labels = sorted(k for k, c in counts.items() if c["dem"] + c["rep"])
    span = ("the 2024 and 2026 primaries and runoffs%s" % (", including March 2024's presidential primary" if "2024-03" in labels else "")
            if any(k.startswith("2024") for k in labels) else "the May and June 2026 primaries")
    t = {k: sum(c[k] for c in counties.values()) for k in ("voters", "dem", "rep", "none")}
    body = {"v": 1, "files": stamp, "generated_at": C.utc_now_iso(), "span": span, "primaries": counts, "through": stats["through"],
            "file_date": "%04d-%02d-%02d" % zipfile.ZipFile(newest[0]).getinfo(newest[2]).date_time[:3], "match": stats, "totals": t,
            "method": "last party primary voted in (%s), from the Secretary of State's Voter History Files, matched voter by voter to the "
                      "general's Voter Absentee File" % span,
            "state_series": series, "districts": districts, "counties": counties}
    with open(OUT_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(body, f, separators=(",", ":"))
    share = {"state": "ga", "source": "Official voter files: early and mail voters matched to the primary voter history", "url": HISTORY_URL,
             "as_of": stats["through"], "counties": len(counties), "partial_from": "", "compare_default": "",
             "series": {"2026": dict(series, election_day=ELECTION_DAY, span=span)},
             "note": "Georgia has no party registration. Party here is the last party primary each voter took part in (%s), matched voter by voter "
                     "from the Secretary of State's Voter History Files and Voter Absentee File. Primaries are open, so a primary ballot isn't a "
                     "party registration, and None means no party primary in that span, including new voters. The files are downloaded by "
                     "hand, so this updates only when a new file is added." % span,
             "generated_at": C.utc_now_iso()}
    with open(SHARE_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(share, f, separators=(",", ":"))
    print("ga_rosters: %d voters through %s: D %d, R %d, none %d | counties %d | primaries %s | duplicates %d, no district %d, undated %d, unmatched counties %s"
          % (t["voters"], stats["through"], t["dem"], t["rep"], t["none"], len(counties), labels, stats["duplicates"], stats["no_district"], stats["undated"], stats["unmatched_counties"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
