#!/usr/bin/env python3
"""Fetch Colorado ballots-returned turnout by county and registered party.

Source: CO Secretary of State daily "General Election Activity" XLSX, posted as
press-release attachments during the election:
  https://www.coloradosos.gov/pubs/newsRoom/<YYYYMMDD>GeneralElectionActivity.xlsx
The <YYYYMMDD> is the data-as-of day (posted each weekday once ballots start
mailing, ~Oct 13 2026). These files are SEASONAL -- removed off-season -- so
there is nothing to validate against until the 2026 files appear. This connector
probes the most recent weekdays for a file and, when found, parses the county x
party grid. CO is all-mail, so `cast` = ballots returned.

STATUS: staged / PENDING VERIFICATION. The exact sheet name and column layout of
the live 2026 XLSX are not confirmable off-season; the parser below is a
best-effort heuristic (find the sheet whose header row has a county column plus
party columns) and fails safe to an empty snapshot (reads 0) on any mismatch.
The Friday rollout routine confirms the layout against the first live file.

Run:  python scripts/co_update.py [--force] [--date YYYYMMDD]
"""
import os
import io
import re
import sys
import json
import zipfile
import datetime as dt
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "co"
CONFIG_PATH = os.path.join(ROOT, "config", "co.json")
GEO_PATH = os.path.join(ROOT, "assets", "co-counties.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def base_key(name):
    return re.sub(r"[^a-z0-9]", "", name.lower().replace(" county", ""))


def geo_index():
    idx = {}
    for ft in load(GEO_PATH)["features"]:
        p = ft["properties"]
        idx[base_key(p["name"])] = {"name": p["name"], "fips": p["fips"]}
    return idx


def read_xlsx(raw):
    """Minimal stdlib .xlsx reader -> {sheet_name: [[cell, ...], ...]}."""
    z = zipfile.ZipFile(io.BytesIO(raw))
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(NS + "si"):
            shared.append("".join(t.text or "" for t in si.iter(NS + "t")))
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    rid_to_target = {r.get("Id"): r.get("Target") for r in rels}
    RNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    sheets = {}
    for s in wb.iter(NS + "sheet"):
        name = s.get("name")
        tgt = rid_to_target.get(s.get(RNS + "id"), "")
        path = "xl/" + tgt.lstrip("/") if not tgt.startswith("xl/") else tgt
        if path not in z.namelist():
            continue
        rows = []
        for row in ET.fromstring(z.read(path)).iter(NS + "row"):
            cells = []
            for c in row.iter(NS + "c"):
                v = c.find(NS + "v")
                val = ""
                if v is not None and v.text is not None:
                    val = shared[int(v.text)] if c.get("t") == "s" else v.text
                cells.append(val)
            rows.append(cells)
        sheets[name] = rows
    return sheets


def parse_activity(raw, idx, party_map):
    """Best-effort: find county x party counts in any sheet. Returns
    {county_name: {rep,dem,npa,oth}} or {} if nothing recognizable."""
    sheets = read_xlsx(raw)
    want = {k.upper(): v for k, v in party_map.items()}
    for _name, rows in sheets.items():
        # find a header row containing a county column + >=2 party columns
        for hi, hdr in enumerate(rows[:8]):
            up = [str(x).strip().upper() for x in hdr]
            county_col = next((i for i, x in enumerate(up) if x in ("COUNTY", "COUNTY NAME")), None)
            party_cols = {i: want[x] for i, x in enumerate(up) if x in want}
            if county_col is None or len(party_cols) < 2:
                continue
            out = {}
            for r in rows[hi + 1:]:
                if county_col >= len(r):
                    continue
                m = idx.get(base_key(str(r[county_col])))
                if not m:
                    continue
                slot = {"rep": 0, "dem": 0, "npa": 0, "oth": 0}
                for ci, party in party_cols.items():
                    if ci < len(r):
                        slot[party] += C.parse_number(str(r[ci]))
                if any(slot.values()):
                    out[m["name"]] = {"fips": m["fips"], **slot}
            if out:
                return out
    return {}


def discover(cfg):
    """Probe recent weekdays for a posted file; return (raw_bytes, label) or (None,'')."""
    base, suffix = cfg["source"]["base"], cfg["source"]["suffix"]
    gstart = cfg["general_start"].replace("-", "")
    if "--date" in sys.argv:
        d = sys.argv[sys.argv.index("--date") + 1]
        try:
            return C.http_get(base + d + suffix, binary=True, no_cache=True, retries=1), d
        except Exception:  # noqa: BLE001
            return None, ""
    today = dt.date.today()
    for back in range(cfg["source"].get("probe_days", 12)):
        d = today - dt.timedelta(days=back)
        if d.weekday() >= 5:            # skip weekends (posted weekdays)
            continue
        stamp = d.strftime("%Y%m%d")
        if stamp < gstart:              # gate: nothing before ballots mail
            break
        try:
            return C.http_get(base + stamp + suffix, binary=True, no_cache=True, retries=1), stamp
        except Exception:  # noqa: BLE001
            continue
    return None, ""


def main():
    force = "--force" in sys.argv
    cfg = load(CONFIG_PATH)
    idx = geo_index()

    counties_out = {}
    src_label = ""
    raw, stamp = discover(cfg)
    if raw:
        try:
            parsed = parse_activity(raw, idx, cfg["source"]["party_map"])
            for name, s in parsed.items():
                counties_out[name] = {"fips": s["fips"],
                                      "cast": C.party_block(s["rep"], s["dem"], s["oth"], s["npa"])}
            if counties_out:
                src_label = stamp
        except Exception as e:  # noqa: BLE001 - unverified layout -> fail safe to empty
            print("parse failed (layout PENDING verification): %s" % str(e)[:80])

    statewide = {"cast": C.add_blocks(*[c["cast"] for c in counties_out.values()]) if counties_out
                 else C.party_block(0, 0, 0, 0)}
    snap = {
        "state": STATE, "state_name": cfg["state_name"], "election": cfg["election"],
        "source": {"primary": "CO Secretary of State daily General Election Activity (ballots returned), by registered party"},
        "source_compiled": src_label, "source_compiled_iso": (C.utc_now_iso() if counties_out else ""),
        "methods_present": [], "method_labels": cfg.get("method_labels", {}),
        "statewide": statewide, "counties": counties_out,
    }
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
        cast = statewide["cast"]
        if cast["total"]:
            with open(HISTORY_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"generated_at": snap["generated_at"], "compiled": src_label,
                                    "data_hash": snap["data_hash"], "cast": cast["total"],
                                    "margin": cast["margin"]}, separators=(",", ":")) + "\n")
        print("CHANGED  counties=%d  cast=%s (R%s D%s NPA%s) margin=%s"
              % (len(counties_out), cast["total"], cast["rep"], cast["dem"], cast["npa"], cast["margin"]))
    else:
        print("NOCHANGE  (hash %s)" % (prev or "")[:12])
    return 0


if __name__ == "__main__":
    sys.exit(main())
