#!/usr/bin/env python3
"""Fetch Louisiana early-vote turnout by parish and registered party.

Source: LA Secretary of State early-voting statistics, STATEWIDE PDF
(electionstatistics.sos.la.gov). One clean row per parish; after the
"PARISH-##" token come 15 numbers: VOTES, WHITE, BLACK, OTHER, MALE, FEMALE,
DEM, REP, OTH, IN, OUT, INPER, ABS, D, I. VOTES == DEM+REP+OTH, so the party
split fully partitions the early-vote electorate (LA "OTH" party bucket holds
independents/other; there is no separate NPA).

We report `cast` = early ballots cast (in person + absentee), by party. LA gives
the in-person / absentee split only as totals (no party), so we do not split
`cast` by method here.

Race and sex (WHITE / BLACK / OTHER, MALE / FEMALE; as recorded on each voter's
registration) go into the demographics card, statewide and per parish. Race
over time: the PDF is cumulative ("Begin Date 1/1/1900 AND End Date <date>")
and the SoS keeps no daily copies, so each day's statewide race totals are
recorded in data/la/_race_days.json (keyed by the report's End Date) and turned
into data/la/race_share.json (ballots per day by race) for the page's "share of
ballots over time" chart -- the days before the first one recorded (10/25)
are folded into it. Past generals (config source.race_compare) come from their
final statewide files, lined up at Election Day.

The 2026 general file (2026_1103_StatewideStats.pdf) publishes when early voting
opens (~late Oct); until then the fetch 404s and this writes an empty snapshot
(reads 0), lighting up automatically once the file appears.

Requires pypdf (the LA workflow step installs it).

Run:  python scripts/la_update.py [--force]
      python scripts/la_update.py --validate   (parse the persisted 2024 file to prove the parser)
"""
import os
import re
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "la"
CONFIG_PATH = os.path.join(ROOT, "config", "la.json")
GEO_PATH = os.path.join(ROOT, "assets", "la-parishes.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")

PARISH_RE = re.compile(r"^([A-Z][A-Z .'&]*)-(\d{1,2})$")
NUM_RE = re.compile(r"^-?[\d,]+$")


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def base_key(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def geo_index():
    idx = {}
    for ft in load(GEO_PATH)["features"]:
        p = ft["properties"]
        idx[base_key(p["name"])] = {"name": p["name"], "fips": p["fips"]}
    return idx


def parse_statewide(raw_bytes, cols):
    """Return {PARISH_UPPER: [15 ints]} parsed from the statewide PDF."""
    import io
    import pypdf
    reader = pypdf.PdfReader(io.BytesIO(raw_bytes))
    toks = []
    for pg in reader.pages:
        for line in pg.extract_text().split("\n"):
            s = line.strip()
            if s:
                toks.append(s)
    out = {}
    i = 0
    n = len(toks)
    while i < n:
        m = PARISH_RE.match(toks[i])
        if m:
            name = m.group(1).strip()
            nums, j = [], i + 1
            while j < n and len(nums) < 15 and NUM_RE.match(toks[j]):
                nums.append(int(toks[j].replace(",", "")))
                j += 1
            if len(nums) >= max(cols.values()) + 1:
                out[name] = nums
                i = j
                continue
        i += 1
    return out


RACES = (("White", "white"), ("Black", "black"), ("Other", "other"))


def race_totals(parsed, cols):
    return {label: sum(nums[cols[k]] for nums in parsed.values()) for label, k in RACES}


def demographics(nums_list, cols):
    """Race and sex of early voters (from their registration records) -> the page's demographics block."""
    votes = sum(n[cols["votes"]] for n in nums_list)
    if not votes:
        return None
    race = [{"label": label, "count": sum(n[cols[k]] for n in nums_list)} for label, k in RACES]
    male, female = (sum(n[cols[k]] for n in nums_list) for k in ("male", "female"))
    sex = [{"label": "Female", "count": female}, {"label": "Male", "count": male}]
    if votes > male + female:
        sex.append({"label": "Unknown", "count": votes - male - female})
    return {"race": race, "gender": sex,
            "note": "Race and sex as recorded on each voter's registration (Louisiana's form asks both)."}


def report_end(raw_bytes):
    """The report's End Date ('... Begin Date 1/1/1900 AND End Date 10/25/2026') -> ISO date, or ''."""
    import io
    import pypdf
    text = pypdf.PdfReader(io.BytesIO(raw_bytes)).pages[0].extract_text() or ""
    m = re.search(r"End Date (\d{1,2})/(\d{1,2})/(\d{4})", text)
    return "%s-%02d-%02d" % (m.group(3), int(m.group(1)), int(m.group(2))) if m else ""


def build_snapshot(cfg, parsed, idx, src_label):
    cols = cfg["source"]["cols"]
    counties_out = {}
    unmatched = []
    for pname, nums in parsed.items():
        m = idx.get(base_key(pname))
        if not m:
            unmatched.append(pname)
            continue
        rep = nums[cols["rep"]]; dem = nums[cols["dem"]]; oth = nums[cols["oth"]]
        blk = C.party_block(rep, dem, oth, 0)
        counties_out[m["name"]] = {"fips": m["fips"], "cast": blk}
        d = demographics([nums], cols)
        if d:
            counties_out[m["name"]]["demographics"] = d
    statewide = {"cast": C.add_blocks(*[c["cast"] for c in counties_out.values()]) if counties_out
                 else C.party_block(0, 0, 0, 0)}
    snap = {
        "state": STATE, "state_name": cfg["state_name"], "election": cfg["election"],
        "source": {"primary": "LA Secretary of State early-voting statistics (statewide PDF), by registered party"},
        "source_compiled": src_label, "source_compiled_iso": (C.utc_now_iso() if counties_out else ""),
        "methods_present": [], "method_labels": cfg.get("method_labels", {}),
        "statewide": statewide, "counties": counties_out,
    }
    d = demographics(list(parsed.values()), cols) if counties_out else None
    if d:
        snap["demographics"] = d
    return snap, unmatched


RACE_DAYS_PATH = os.path.join(DATA_DIR, "_race_days.json")
RACE_SHARE_PATH = os.path.join(DATA_DIR, "race_share.json")


def write_race_share(cfg, parsed, end_iso):
    """Record today's statewide race totals (cumulative through the report's End Date) and rewrite
    data/la/race_share.json when anything changed."""
    cols, src = cfg["source"]["cols"], cfg["source"]
    days = (load(RACE_DAYS_PATH) if os.path.exists(RACE_DAYS_PATH) else {}) or {}
    tot = race_totals(parsed, cols)
    if end_iso and sum(tot.values()) and days.get(end_iso) != tot:
        days[end_iso] = tot
        with open(RACE_DAYS_PATH, "w", encoding="utf-8", newline="\n") as f:
            json.dump(dict(sorted(days.items())), f, indent=0)
    if not days:
        return
    dates = sorted(days)
    labels = [label for label, _ in RACES]
    counts = {lab: [days[d][lab] - (days[dates[i - 1]][lab] if i else 0) for i, d in enumerate(dates)] for lab in labels}
    series = {cfg["election"]["date"][:4]: {"election_day": cfg["election"]["date"], "dates": dates, "counts": counts,
                                            "total": sum(days[dates[-1]].values())}}
    prev = (load(RACE_SHARE_PATH) if os.path.exists(RACE_SHARE_PATH) else {}) or {}
    for yr, spec in (src.get("race_compare") or {}).items():   # past generals' final files: fixed, read once
        old = (prev.get("series") or {}).get(yr)
        if old and old.get("file") == spec["file"]:
            series[yr] = old
            continue
        try:
            past = parse_statewide(C.http_get(src["base"] + spec["file"], binary=True, retries=2), cols)
            t = race_totals(past, cols)
            series[yr] = {"election_day": spec["election_day"], "dates": [spec["election_day"]],
                          "counts": {lab: [t[lab]] for lab in labels}, "total": sum(t.values()), "file": spec["file"]}
        except Exception as e:  # noqa: BLE001
            print("LA: %s race comparison unavailable (%s)" % (yr, str(e)[:80]), file=sys.stderr)
    body = {"state": STATE, "series": series, "markers": src.get("race_markers", []),
            "compare_default": src.get("race_compare_default", ""),
            "compare_note": "Past generals: their final early-vote totals (Louisiana keeps no day-by-day copies), shown at "
                            "Election Day.",
            "source": "LA Secretary of State early-voting statistics (statewide PDF): early voters by the race on their "
                      "registration (White, Black, Other). The report is cumulative and not kept day by day, so ballots "
                      "per day come from the daily reports recorded here since %s (earlier days are folded into that "
                      "first one)." % dates[0]}
    h = C.data_hash(body)
    if h == prev.get("data_hash"):
        return
    with open(RACE_SHARE_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(dict(body, as_of=dates[-1], data_hash=h), f, separators=(",", ":"))
    print("la: race_share.json updated (%s)" % ", ".join("%s: %d" % (k, v["total"]) for k, v in sorted(series.items())))


def main():
    force = "--force" in sys.argv
    validate = "--validate" in sys.argv
    cfg = load(CONFIG_PATH)
    idx = geo_index()
    base = cfg["source"]["base"]

    if validate:
        fn = cfg["source"]["validation_file"]
        raw = C.http_get(base + fn, binary=True, no_cache=True)
        parsed = parse_statewide(raw, cfg["source"]["cols"])
        snap, unmatched = build_snapshot(cfg, parsed, idx, fn)
        sw = snap["statewide"]["cast"]
        print("Validation on %s: parsed=%d parishes matched=%d"
              % (fn, len(parsed), len(snap["counties"])))
        print("  statewide cast=%s  R=%s D=%s Oth=%s  margin=%s"
              % (sw["total"], sw["rep"], sw["dem"], sw["oth"], sw["margin"]))
        if unmatched:
            print("  UNMATCHED:", unmatched)
        return 0

    fn = cfg["election"]["file_token"] + cfg["source"]["suffix"]
    parsed = {}
    src_label = ""
    end_iso = ""
    try:
        raw = C.http_get(base + fn, binary=True, no_cache=True, retries=2)
        parsed = parse_statewide(raw, cfg["source"]["cols"])
        src_label = cfg["election"]["name"]
        end_iso = report_end(raw)
    except Exception as e:  # noqa: BLE001 - file not posted yet -> empty snapshot
        print("no live file yet (%s): %s" % (fn, str(e)[:70]))
    if parsed:
        try:
            write_race_share(cfg, parsed, end_iso)
        except Exception as e:  # noqa: BLE001 - the chart file must never sink the turnout update
            print("LA: race_share.json not written (%s)" % str(e)[:120], file=sys.stderr)

    snap, unmatched = build_snapshot(cfg, parsed, idx, src_label)
    snap["data_hash"] = C.data_hash(snap)
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
        cast = snap["statewide"]["cast"]
        if cast["total"]:
            with open(HISTORY_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"generated_at": snap["generated_at"], "compiled": src_label,
                                    "data_hash": snap["data_hash"], "cast": cast["total"],
                                    "margin": cast["margin"]}, separators=(",", ":")) + "\n")
        print("CHANGED  parishes=%d  cast=%s (R%s D%s Oth%s) margin=%s"
              % (len(snap["counties"]), cast["total"], cast["rep"], cast["dem"], cast["oth"], cast["margin"]))
    else:
        print("NOCHANGE  (hash %s)" % (prev or "")[:12])
    if unmatched:
        print("  unmatched parishes:", unmatched[:10])
    return 0


if __name__ == "__main__":
    sys.exit(main())
