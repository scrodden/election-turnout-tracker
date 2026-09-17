#!/usr/bin/env python3
"""Iowa turnout by county and registered party.

Source: Iowa SoS "Absentee Ballot Statistics" PDF. Hierarchical layout:
  <County>  Requested Issued Received
    <Party> Requested Issued Received      (Democrat/Republican/No Party/Libertarian/Other)
      <Receipt Method> ...                 (Counter/In-Office, Mail, Drop Box, ...)
We take each party's RECEIVED (returned) as ballots cast. A county's Received
equals the sum of its parties' Received (validated). Iowa early voting is
absentee (in-person at the office/satellite + mail), so this is the full
early-vote electorate by party. The file has no registration totals, so there is
no turnout%.

The 2026 general file publishes under /elections/pdf/2026/general/; until then
this writes an empty snapshot (reads 0) and lights up automatically. Requires
pypdf (the IA workflow step installs it).

Run:  python scripts/ia_update.py [--force]
      python scripts/ia_update.py --validate   (parse the persisted 2024 file to prove the parser)
"""
import os
import re
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

STATE = "ia"
CONFIG_PATH = os.path.join(ROOT, "config", "ia.json")
GEO_PATH = os.path.join(ROOT, "assets", "ia-counties.geojson")
DATA_DIR = os.path.join(ROOT, "data", STATE)
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
ROW_RE = re.compile(r"^(.+?)\s+(\d+)\s+(\d+)\s+(\d+)$")


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def norm(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def geo_index():
    idx = {}
    for ft in load(GEO_PATH)["features"]:
        p = ft["properties"]
        idx[norm(p["name"])] = {"name": p["name"], "fips": p["fips"]}
    return idx


def parse_absentee(full, idx, party_map):
    """Return ({county: {rep,dem,oth,npa}}, {county: total_received}) from the PDF."""
    counties = {}
    county_total = {}
    cur = None
    for line in full.split("\n"):
        m = ROW_RE.match(line.strip())
        if not m:
            continue
        label = m.group(1).strip()
        received = int(m.group(4))
        k = norm(label)
        if k in idx:                              # county line
            name = idx[k]["name"]
            # The PDF concatenates ~daily cumulative snapshots (newest first); once
            # a county repeats we've reached an older snapshot -> stop.
            if name in counties:
                break
            cur = name
            counties[cur] = {"fips": idx[k]["fips"], "rep": 0, "dem": 0, "oth": 0, "npa": 0}
            county_total[cur] = received
        elif cur and label in party_map:          # party line
            counties[cur][party_map[label]] += received
        # receipt-method lines (3 ints, not a county/party) are ignored
    return counties, county_total


def build_snapshot(cfg, counties, src_label):
    counties_out = {}
    for name, s in counties.items():
        counties_out[name] = {"fips": s["fips"], "cast": C.party_block(s["rep"], s["dem"], s["oth"], s["npa"])}
    statewide = {"cast": C.add_blocks(*[c["cast"] for c in counties_out.values()]) if counties_out
                 else C.party_block(0, 0, 0, 0)}
    snap = {
        "state": STATE, "state_name": cfg["state_name"], "election": cfg["election"],
        "source": {"primary": "Iowa Secretary of State Absentee Ballot Statistics (ballots returned by county & registered party)"},
        "source_compiled": src_label, "source_compiled_iso": (C.utc_now_iso() if counties_out else ""),
        "methods_present": [], "method_labels": cfg.get("method_labels", {}),
        "statewide": statewide, "counties": counties_out,
    }
    return snap


def read_pdf_text(raw):
    import io
    import pypdf
    return "\n".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(raw)).pages)


def main():
    force = "--force" in sys.argv
    validate = "--validate" in sys.argv
    cfg = load(CONFIG_PATH)
    idx = geo_index()
    party_map = cfg["source"]["party_map"]

    if validate:
        raw = C.http_get(cfg["source"]["validation_url"], binary=True, no_cache=True)
        counties, totals = parse_absentee(read_pdf_text(raw), idx, party_map)
        snap = build_snapshot(cfg, counties, "validation")
        sw = snap["statewide"]["cast"]
        ok = sum(1 for c, s in counties.items()
                 if (s["rep"] + s["dem"] + s["oth"] + s["npa"]) == totals.get(c))
        print("Validation: matched counties=%d/%d  party-sum==county-total for %d/%d"
              % (len(counties), len(idx), ok, len(counties)))
        print("  statewide received: total=%s R=%s D=%s NPA=%s Oth=%s margin=%s"
              % (sw["total"], sw["rep"], sw["dem"], sw["npa"], sw["oth"], sw["margin"]))
        return 0

    counties = {}
    src_label = ""
    try:
        raw = C.http_get(cfg["source"]["url"], binary=True, no_cache=True, retries=2)
        counties, _ = parse_absentee(read_pdf_text(raw), idx, party_map)
        if counties:
            src_label = cfg["election"]["name"]
    except Exception as e:  # noqa: BLE001 - file not posted yet -> empty snapshot
        print("no live file yet: %s" % str(e)[:70])

    snap = build_snapshot(cfg, counties, src_label)
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
        print("CHANGED  counties=%d  cast=%s (R%s D%s NPA%s) margin=%s"
              % (len(snap["counties"]), cast["total"], cast["rep"], cast["dem"], cast["npa"], cast["margin"]))
    else:
        print("NOCHANGE  (hash %s)" % (prev or "")[:12])
    return 0


if __name__ == "__main__":
    sys.exit(main())
