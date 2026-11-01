#!/usr/bin/env python3
"""Past national House vote by state, for the results page's national House vote card.

Source: the Clerk of the House's "Statistics of the Presidential and Congressional Election" (official counts), whose
last pages have a table of "Votes Cast for Representatives" by state and party. Run once per election; the output,
config/house_vote_past.json, is committed. Needs pypdf, so it isn't part of the automated update.

Run:  python scripts/build_house_vote_past.py
"""
import io
import json
import os
import re
import sys
import urllib.request

import pypdf

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "config", "house_vote_past.json")
URL = "https://clerk.house.gov/member_info/electionInfo/%s/statistics%s.pdf"
YEARS = ("2024", "2022")
USPS = {"Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA", "Colorado": "CO", "Connecticut": "CT",
        "Delaware": "DE", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA",
        "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI",
        "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH",
        "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY", "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
        "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN",
        "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY"}
ROW = re.compile(r"^([A-Z][A-Za-z. ]+?)\s*\.{2,}\s*(.+)$")
HEAD = "State Republican Democratic"


def read_year(year):
    """-> ({usps: {"rep", "dem", "total"}}, the table's own national "rep", "dem", "total")."""
    url = URL % (year, year)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    pdf = pypdf.PdfReader(io.BytesIO(urllib.request.urlopen(req, timeout=120).read()))
    pages = [p.extract_text() or "" for p in pdf.pages]
    # the state-and-party tables run Senate, then Representatives: the Representatives table is the one with all 50 states
    starts = [i for i, t in enumerate(pages) if HEAD in t]
    states, national = {}, None
    for i in starts[-1:]:
        for t in pages[i:i + 2]:
            for line in t.splitlines():
                m = ROW.match(line.strip())
                if not m:
                    continue
                # a blank column prints as dots (a state with no Republican running), so R and D keep their places at the front
                cells = [int(c.replace(",", "")) if re.fullmatch(r"[\d,]+", c) else 0 for c in m.group(2).split()]
                row = {"rep": cells[0], "dem": cells[1], "total": cells[-1]}
                if m.group(1).strip() == "Total":
                    national = row
                elif m.group(1).strip() in USPS:
                    states[USPS[m.group(1).strip()]] = row
    return url, states, national


def main():
    out = {"_comment": "Official House votes by state from the Clerk of the House; written by scripts/build_house_vote_past.py. "
                       "Margins on the page = (R - D) / (R + D): the Clerk's totals include blank ballots in some states.", "years": {}}
    for year in YEARS:
        url, states, nat = read_year(year)
        if len(states) != 50:
            sys.exit("%s: read %d states, missing %s" % (year, len(states), sorted(set(USPS.values()) - set(states))))
        total = {k: sum(s[k] for s in states.values()) for k in ("rep", "dem", "total")}
        print(year, "states", total, "| table's national row", nat, "| R+%.2f" % (100.0 * (total["rep"] - total["dem"]) / total["total"]))
        out["years"][year] = {"source": "Clerk of the House, Statistics of the Presidential and Congressional Election", "url": url,
                              "national": total, "states": states}
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, indent=0, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
