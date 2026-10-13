#!/usr/bin/env python3
"""Who and what is on the 2026 general-election ballot, for the results page
before Election Night (official results replace it as they're reported).

  config/candidates_2026.json   general-election candidates for every Senate,
                                governor and House race in config/results_races.json,
                                from Wikipedia's "2026 United States Senate /
                                gubernatorial / House of Representatives elections"
                                articles (CC BY-SA 4.0; the race tables cite each
                                state's certified candidate list)
  config/measures.json          statewide ballot measures on the November 3, 2026
                                ballot, from Ballotpedia's "2026 ballot measures"
                                list (each with its Ballotpedia page)

A file is only replaced when the new parse is complete enough (every race found
and most with candidates / a plausible number of measures), so a bad edit or a
changed page layout keeps the previous list.

  python scripts/build_ballot_2026.py [--candidates] [--measures] [--force]   (default: both)

In CI it runs every cycle but rebuilds a list at most once every 20 hours, and
not at all from Election Day on (official results take over then, and the
articles start marking winners).
"""
import html
import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import unquote

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402

SLATE = os.path.join(ROOT, "config", "results_races.json")
CANDS_OUT = os.path.join(ROOT, "config", "candidates_2026.json")
MEAS_OUT = os.path.join(ROOT, "config", "measures.json")
WIKI = {"senate": "2026 United States Senate elections",
        "governor": "2026 United States gubernatorial elections",
        "house": "2026 United States House of Representatives elections"}

STATES = {"Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA", "Colorado": "CO",
          "Connecticut": "CT", "Delaware": "DE", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
          "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA",
          "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
          "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
          "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
          "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
          "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
          "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
          "Wisconsin": "WI", "Wyoming": "WY"}


def text(x):
    x = re.sub(r"<sup[^>]*class=\"reference\"[^>]*>.*?</sup>", "", x, flags=re.S)
    x = re.sub(r"<style[^>]*>.*?</style>|<link[^>]*>", "", x, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", x))).strip()


def wiki_html(title):
    u = "https://en.wikipedia.org/w/api.php?action=parse&format=json&prop=text|revid&redirects=1&page=" + title.replace(" ", "_")
    j = json.loads(C.http_get(u, retries=2, timeout=90))
    return j["parse"]["text"]["*"], j["parse"]["revid"]


def candidates_of(cell):
    out = []
    for li in re.findall(r"<li[^>]*>(.*?)</li>", cell, re.S):
        t = text(li).replace("▌", "").strip()
        m = re.match(r"^(.*?)\s*\(([^()]+)\)\s*(?:\[.*)?$", t)
        if not m or not m.group(1):
            continue
        name, party = m.group(1).strip(), m.group(2).strip()
        if re.match(r"(?i)^(y|✓)\s+", name):   # winner tick (only in already-held races)
            name = re.sub(r"(?i)^(y|✓)\s+", "", name)
        out.append({"name": name, "party": party})
    return out


def race_rows(h, office):
    """-> {race_id: [candidates]} from the article's race tables."""
    found = {}
    for t in re.findall(r"<table[^>]*wikitable[^>]*>(.*?)</table>", h, re.S):
        if "Candidates" not in t or "This race" in t[:4000]:   # 'This race' = already-held special elections
            continue
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", t, re.S):
            cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.S)
            if len(cells) < 3:
                continue
            label = text(cells[0])
            cand_cell = next((c for c in reversed(cells) if "<li" in c), None)
            if cand_cell is None:
                continue
            if office == "house":
                m = re.match(r"^(.+?)\s+(\d+|at-large)$", label, re.I)
                if not m or m.group(1) not in STATES:
                    continue
                d = m.group(2).lower()
                rid = "house-%s-%s" % (STATES[m.group(1)], "AL" if d == "at-large" else "%02d" % int(d))
            else:
                m = re.match(r"^(.+?)(?:\s*\((special|class \d)\))?\s*(?:\[.*)?$", label, re.I)
                st = m and m.group(1).strip()
                if st not in STATES:
                    continue
                pre = "sen" if office == "senate" else "gov"
                rid = "%s-%s%s" % (pre, STATES[st], "-special" if m.group(2) and "special" in m.group(2).lower() else "")
            found[rid] = candidates_of(cand_cell)
    return found


def build_candidates():
    slate = json.load(open(SLATE, encoding="utf-8"))
    out = {"_comment": ("General-election candidates per race, from Wikipedia's 2026 election articles (CC BY-SA 4.0), "
                        "whose race tables cite each state's certified candidate list. Shown on the results page "
                        "until official results arrive. Built by scripts/build_ballot_2026.py."),
           "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "sources": {}, "races": {}}
    problems = []
    for office, title in WIKI.items():
        h, rev = wiki_html(title)
        rows = race_rows(h, office)
        want = [r["id"] for r in slate[office]]
        # a Senate special can be listed under the plain state name when it's the state's only 2026 race
        for r in slate[office]:
            if r["id"] not in rows and r["id"].endswith("-special") and r["id"][:-8] in rows:
                rows[r["id"]] = rows.pop(r["id"][:-8])
        missing = [i for i in want if i not in rows]
        empty = [i for i in want if i in rows and not rows[i]]
        out["sources"][office] = {"title": title, "url": "https://en.wikipedia.org/w/index.php?oldid=%d" % rev,
                                  "revision": rev, "races": len(want), "found": len(want) - len(missing),
                                  "with_candidates": len(want) - len(missing) - len(empty),
                                  "missing": missing, "no_candidates": empty}
        for i in want:
            if rows.get(i):
                out["races"][i] = rows[i]
        if len(missing) > max(1, 0.02 * len(want)):
            problems.append("%s: %d of %d races not found (%s)" % (office, len(missing), len(want), missing[:6]))
    if problems:
        print("candidates: NOT written — " + "; ".join(problems))
        return 1
    with open(CANDS_OUT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
        f.write("\n")
    for o, s in out["sources"].items():
        print("candidates %s: %d/%d races with candidates%s" % (o, s["with_candidates"], s["races"],
              (" | missing " + ", ".join(s["missing"])) if s["missing"] else "") +
              ((" | none listed: " + ", ".join(s["no_candidates"][:12])) if s["no_candidates"] else ""))
    return 0


def build_measures():
    url = "https://ballotpedia.org/2026_ballot_measures"
    h = C.http_get(url, retries=2, timeout=90)
    a, b = h.find('id="List_of_state_ballot_measures"'), h.find('id="List_of_election_dates"')
    if a < 0 or b < 0:
        print("measures: NOT written — page layout changed")
        return 1
    measures = []
    for part in re.split(r"<h3[^>]*>", h[a:b])[1:]:
        state = text(part[:part.find("</h3>")]) if "</h3>" in part else ""
        state = re.sub(r"\s*See also.*$", "", state).strip()
        if state not in STATES:
            continue
        st = STATES[state]
        # each table is preceded by its election date ("November 3", "May 19", ...)
        for m in re.finditer(r"(November 3|January \d+|February \d+|March \d+|April \d+|May \d+|June \d+|July \d+|"
                             r"August \d+|September \d+|October \d+|November \d+|December \d+)\s*(?:</[^>]+>\s*)*"
                             r"(?:<[^t][^>]*>\s*)*<table[^>]*>(.*?)</table>", part, re.S):
            if m.group(1) != "November 3":
                continue
            for row in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(2), re.S):
                cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
                if len(cells) < 4:
                    continue
                typ, title_cell, subj, desc = cells[0], cells[1], cells[2], cells[3]
                link = re.search(r'href="((?:https?://ballotpedia\.org)?/[^"#]+)"', title_cell)
                title = text(title_cell)
                if not title:
                    continue
                n = sum(1 for x in measures if x["state"] == st) + 1
                page = (link.group(1).rsplit("/", 1)[-1] if link else "")
                name = re.sub(r"\s*\(2026\)$", "", unquote(page).replace("_", " ")) if page else title
                measures.append({"id": "meas-%s-%d" % (st, n), "state": st, "state_name": state, "title": title, "name": name,
                                 "type": text(typ), "subject": text(subj).replace(" ; ", "; "),
                                 "description": text(desc),
                                 "url": (link.group(1) if link.group(1).startswith("http") else "https://ballotpedia.org" + link.group(1)) if link else url})
    if len(measures) < 40:
        print("measures: NOT written — only %d found (expected dozens)" % len(measures))
        return 1
    doc = {"_comment": ("2026 statewide ballot measures on the November 3 ballot, from Ballotpedia's '2026 ballot measures' "
                        "list (each with its Ballotpedia page). Vote results (yes/no, reporting %, Pass/Fail) are filled "
                        "by results_update.py from official state feeds near the election. Built by "
                        "scripts/build_ballot_2026.py; types: LRCA = legislatively referred constitutional amendment, "
                        "LRSS = legislatively referred state statute, CICA / CISS = citizen-initiated amendment / statute, "
                        "VR = veto referendum, ABR = advisory question, CCR = commission-referred, BI = bond issue."),
           "source": url, "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "measures": measures}
    with open(MEAS_OUT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, indent=1, ensure_ascii=False)
        f.write("\n")
    by = {}
    for x in measures:
        by[x["state"]] = by.get(x["state"], 0) + 1
    print("measures: %d on the November 3 ballot in %d states %s" % (len(measures), len(by), by))
    return 0


FREEZE_AT = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)   # Election Day, before polls open


def due(path, force):
    """Rebuild only if forced, or the list is over 20 hours old and it's not yet Election Day."""
    if force:
        return True
    if datetime.now(timezone.utc) >= FREEZE_AT:
        print("%s: frozen for Election Day" % os.path.basename(path))
        return False
    try:
        built = datetime.strptime(json.load(open(path, encoding="utf-8"))["built_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (OSError, KeyError, ValueError):
        return True
    if (datetime.now(timezone.utc) - built).total_seconds() < 20 * 3600:
        print("%s: built %s, not due yet" % (os.path.basename(path), built.strftime("%Y-%m-%d %H:%M UTC")))
        return False
    return True


def main():
    args = sys.argv[1:]
    force = "--force" in args
    both = not ("--candidates" in args or "--measures" in args)
    rc = 0
    if (both or "--candidates" in args) and due(CANDS_OUT, force):
        rc |= build_candidates()
    if (both or "--measures" in args) and due(MEAS_OUT, force):
        rc |= build_measures()
    return rc


if __name__ == "__main__":
    sys.exit(main())
