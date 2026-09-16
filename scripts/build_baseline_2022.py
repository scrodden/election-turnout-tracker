#!/usr/bin/env python3
"""Build a static 2022 General Election partisan-turnout baseline for comparison.

2022 is final and never changes, so this runs ONCE (manually) and commits
data/fl/baseline_2022.json. It combines two official FL DOS sources:
  1. 2022 GE vote-by-mail / early-voting archive (PDF, election #26906):
     by county & party (Rep/Dem/Other/NPA) for mail-provided, mail-voted, early-voted.
  2. 2022 GE official Voter Registration & Turnout report (registration, total
     ballots, turnout %) by county.

NOTE: this builder needs `pypdf` (pip install pypdf) to read the PDF; it is a
one-time tool, NOT part of the 10-minute updater (which stays stdlib-only). The
committed baseline_2022.json has no runtime dependency.
"""
import os
import re
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

PDF_URL = "https://files.floridados.gov/media/706191/2022-ge-rptstatuscountsarchive-ev-vbm.pdf"
TURNOUT_URL = "https://results.elections.myflorida.com/TurnoutRpt.asp?ElectionDate=11/8/2022&DATAMODE="
OUT_PATH = os.path.join(ROOT, "data", "fl", "baseline_2022.json")
COUNTIES_PATH = os.path.join(ROOT, "config", "fl_counties.json")

METHOD_BY_LABEL = [
    ("Vote-by-Mail Provided", "mail_provided"),
    ("Voted Vote-by-Mail", "mail_voted"),
    ("Voted Early", "early_voted"),
]
DATA_RE = re.compile(r"^([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)(.+)$")
COUNTY_RE = re.compile(r"TOTAL(STATE TOTAL|[A-Z][A-Za-z .'\-]+?)\s*$")


def n(s):
    return int(str(s).replace(",", "").strip() or 0)


def keyname(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def block(r, d, o, npa):
    return {"rep": r, "dem": d, "oth": o, "npa": npa, "total": r + d + o + npa}


def parse_pdf():
    try:
        from pypdf import PdfReader
    except ImportError:
        sys.exit("pypdf required: pip install pypdf")
    import io
    raw = C.http_get(PDF_URL, binary=True, timeout=120, retries=3)
    reader = PdfReader(io.BytesIO(raw))
    text = "\n".join((p.extract_text() or "") for p in reader.pages)

    out = {}
    current = None
    for line in text.splitlines():
        line = line.rstrip()
        cm = COUNTY_RE.search(line)
        if cm:
            current = cm.group(1).strip()
            out.setdefault(current, {})
        dm = DATA_RE.match(line.strip())
        if dm and current:
            label = dm.group(6)
            mkey = next((k for lbl, k in METHOD_BY_LABEL if lbl in label), None)
            if mkey:
                out[current][mkey] = block(n(dm.group(1)), n(dm.group(2)),
                                           n(dm.group(3)), n(dm.group(4)))
    return out


def parse_turnout():
    html = C.http_get(TURNOUT_URL, retries=3)
    txt = re.sub(r"<[^>]+>", " ", html)
    txt = re.sub(r"\s+", " ", txt)
    out = {}
    for m in re.finditer(r"([A-Za-z][A-Za-z .'\-]+?)\s+([\d,]+)\s+([\d,]+)\s+([\d.]+)%", txt):
        name = m.group(1).strip()
        if keyname(name) in ("county", "total", "statetotal"):
            continue
        out[keyname(name)] = {"registered": n(m.group(2)), "turnout_total": n(m.group(3)),
                              "turnout_pct": float(m.group(4))}
    return out


def main():
    counties = json.load(open(COUNTIES_PATH, encoding="utf-8"))["counties"]
    by_key = {keyname(c["name"]): c for c in counties}

    pdf = parse_pdf()
    turnout = parse_turnout()

    result_counties = {}
    statewide = None
    matched = 0
    for name, methods in pdf.items():
        if keyname(name) in ("statetotal",):
            statewide = dict(methods)
            continue
        c = by_key.get(keyname(name))
        if not c:
            print("  ! unmatched county from PDF:", name)
            continue
        matched += 1
        ent = dict(methods)
        ent["fips"] = c["fips"]; ent["code"] = c["code"]
        t = turnout.get(keyname(name), {})
        ent.update(t)
        # cast = mail voted + early voted (no election-day-by-party available for 2022)
        mv, ev = ent.get("mail_voted"), ent.get("early_voted")
        ent["cast"] = block((mv or {}).get("rep", 0) + (ev or {}).get("rep", 0),
                            (mv or {}).get("dem", 0) + (ev or {}).get("dem", 0),
                            (mv or {}).get("oth", 0) + (ev or {}).get("oth", 0),
                            (mv or {}).get("npa", 0) + (ev or {}).get("npa", 0))
        result_counties[c["name"]] = ent

    # statewide cast + turnout (sum registration/turnout across counties)
    if statewide:
        mv, ev = statewide.get("mail_voted"), statewide.get("early_voted")
        statewide["cast"] = block((mv or {}).get("rep", 0) + (ev or {}).get("rep", 0),
                                 (mv or {}).get("dem", 0) + (ev or {}).get("dem", 0),
                                 (mv or {}).get("oth", 0) + (ev or {}).get("oth", 0),
                                 (mv or {}).get("npa", 0) + (ev or {}).get("npa", 0))
        statewide["registered"] = sum(e.get("registered", 0) for e in result_counties.values())
        statewide["turnout_total"] = sum(e.get("turnout_total", 0) for e in result_counties.values())
        statewide["turnout_pct"] = round(100.0 * statewide["turnout_total"] / statewide["registered"], 2) \
            if statewide["registered"] else None

    out = {
        "election": {"name": "2022 General", "number": "26906", "date": "2022-11-08"},
        "source": "FL DOS 2022 GE vote-by-mail/early-voting archive (#26906) + official turnout report",
        "note": "By-party figures cover mail + early voting only (election-day-by-party is not published free). 'cast' = mail voted + early voted.",
        "statewide": statewide, "counties": result_counties,
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    json.dump(out, open(OUT_PATH, "w", encoding="utf-8"), separators=(",", ":"))
    print("Matched %d/67 counties, turnout rows %d. Wrote %s (%d bytes)."
          % (matched, len(turnout), os.path.relpath(OUT_PATH, ROOT), os.path.getsize(OUT_PATH)))
    sw = statewide or {}
    print("Statewide 2022: mail_voted=%s early_voted=%s cast=%s turnout=%s%%"
          % ((sw.get("mail_voted") or {}).get("total"), (sw.get("early_voted") or {}).get("total"),
             (sw.get("cast") or {}).get("total"), sw.get("turnout_pct")))


if __name__ == "__main__":
    main()
