#!/usr/bin/env python3
"""Update live 2026 election results (House / Senate / Governor / ballot measures).

Reads the canonical race slate (config/results_races.json) and writes
data/results/{senate,governor,house,measures}.json for the results page, filling
candidates/votes/reporting from each state's official feed (scripts/results_feeds.py).
It NEVER fabricates results, and it guards what it publishes:
  - a state's feed isn't read until its polls close (feeds carry test data before that);
  - a feed that fails or drops a race keeps the last figures we published;
  - a race whose votes suddenly fall by over 20% keeps its last figures, for up to 3 hours;
  - a race with no one from the 2026 ballot is rejected (a feed showing another election).
A file is rewritten only when its figures change, so "updated" is when results last moved
and the 2-minute Election-Day loop commits only real changes.

data/results/sources.json says what each state's feed is doing (for the status page).

Run:  python scripts/results_update.py
"""
import os
import sys
import json
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import results_feeds as F  # noqa: E402
SLATE_PATH = os.path.join(ROOT, "config", "results_races.json")
CHAMBERS_PATH = os.path.join(ROOT, "config", "chambers.json")
MEASURES_PATH = os.path.join(ROOT, "config", "measures.json")
CANDIDATES_PATH = os.path.join(ROOT, "config", "candidates_2026.json")   # who's on the ballot (build_ballot_2026.py)
LINKS_PATH = os.path.join(ROOT, "config", "results_links.json")          # each state's official results page
OUT_DIR = os.path.join(ROOT, "data", "results")
DROP_HOLD_HOURS = 3


def load(p, default=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        if default is not None:
            return default
        raise


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_if_changed(name, doc):
    """Write data/results/<name>.json unless only its timestamps differ. -> True if written."""
    path = os.path.join(OUT_DIR, name + ".json")
    stamps = ("generated_at", "updated")
    old = load(path, {})
    if old and {k: v for k, v in old.items() if k not in stamps} == {k: v for k, v in doc.items() if k not in stamps}:
        return False
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"))
    return True


def gather(registry, health, key):
    """Run the feeds whose polls have closed, one bad state never sinking the rest. -> {usps: {id: result}}"""
    live = {}
    for usps, fn in registry.items():
        if not F.polls_closed(usps):
            health.setdefault(usps, {})[key] = "waiting"
            continue
        try:
            live[usps] = fn() or {}
            health.setdefault(usps, {})[key] = "ok"
        except Exception as e:  # noqa: BLE001
            print("  ! %s %s feed failed: %s" % (usps, key, str(e)[:100]), file=sys.stderr)
            health.setdefault(usps, {})[key] = "failed: " + str(e)[:100]
    return live


def race_total(r):
    return sum(c.get("votes", 0) for c in (r or {}).get("candidates") or [])


def apply_feed(rc, src, old, ballot, held):
    """Fill one race from its feed, or keep what we last published. -> True if it has results."""
    if src and not F.names_match(src.get("candidates", []), ballot):
        print("  ! %s: no one from the 2026 ballot in the feed; ignored" % rc["id"], file=sys.stderr)
        held.append(rc["id"])
        src = None
    if src and old and race_total(old) >= 1000 and race_total(src) < 0.8 * race_total(old):
        now = datetime.now(timezone.utc)
        since = old.get("held_since") or utc_now()
        t = datetime.strptime(since, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if now - t < timedelta(hours=DROP_HOLD_HOURS):
            print("  ! %s: votes fell from %s to %s; keeping the last figures"
                  % (rc["id"], format(race_total(old), ","), format(race_total(src), ",")), file=sys.stderr)
            held.append(rc["id"])
            rc["held_since"] = since
            src = None
    use = src or (old if old and old.get("candidates") else None)
    if not use:
        return False
    rc["candidates"] = F.ballot_names(use.get("candidates", []), ballot)
    rc["reporting_pct"] = use.get("reporting_pct")
    rc["called"] = use.get("called")
    rc["leader_party"] = leader(rc["candidates"])
    return True


def write_measures(health, links):
    slate = load(MEASURES_PATH, {}).get("measures", [])
    live = gather(F.MEASURES, health, "measures")
    held = []
    old_doc = load(os.path.join(OUT_DIR, "measures.json"), {})
    prev = {m.get("id"): m for m in old_doc.get("races", [])}
    passed = failed = und = 0
    out = []
    for m in slate:
        r = dict(m); r["office"] = "Ballot Measure"
        r["feed"] = m.get("state") in F.MEASURES and not str(health.get(m.get("state"), {}).get("measures", "")).startswith("failed")
        if links.get(m.get("state")):
            r["official_url"] = links[m["state"]]
        src = live.get(m.get("state"), {}).get(m.get("id"))
        old = prev.get(m.get("id")) or {}
        was = sum(c.get("votes") or 0 for c in old.get("candidates") or [])
        if src and was >= 1000 and src.get("yes", 0) + src.get("no", 0) < 0.8 * was:
            held.append(m.get("id"))
            src = None
        yes = no = None; reporting = called = None
        if src:
            yes = int(src.get("yes", 0)); no = int(src.get("no", 0))
            reporting = src.get("reporting"); called = src.get("called")
        elif old.get("candidates"):   # a feed hiccup: keep the last figures
            yes, no = (c.get("votes") for c in old["candidates"][:2])
            reporting, called = old.get("reporting_pct"), old.get("called")
        tot = (yes or 0) + (no or 0)
        r["candidates"] = ([{"name": "Yes", "party": "Y", "votes": yes}, {"name": "No", "party": "N", "votes": no}] if tot else [])
        r["reporting_pct"] = reporting
        r["called"] = called
        r["yes_pct"] = (round(100.0 * yes / tot, 1) if tot else None)
        if called == "Pass" or (tot and yes > no):
            passed += 1
        elif called == "Fail" or (tot and no > yes):
            failed += 1
        else:
            und += 1
        out.append(r)
    now = utc_now()
    doc = {"office": "measures", "election": {"date": "2026-11-03", "name": "2026 General"},
           "generated_at": now, "updated": (now if any(x.get("candidates") for x in out) else ""),
           "note": "2026 statewide ballot measures; results from official state feeds where one can be read.",
           "list_source": {"label": "Ballotpedia: 2026 ballot measures", "url": "https://ballotpedia.org/2026_ballot_measures"},
           "summary": {"total": len(out), "passed": passed, "failed": failed, "undecided": und},
           "races": out}
    wrote = write_if_changed("measures", doc)
    print("measures   count=%d  passed=%d failed=%d undecided=%d%s" % (len(out), passed, failed, und, "" if wrote else "  (no change)"))


def leader(cands):
    if not cands:
        return None
    s = sorted(cands, key=lambda c: c.get("votes", 0), reverse=True)
    tot = sum(c.get("votes", 0) for c in s)
    return (s[0].get("party", "") or "").upper()[:1] if tot else None


def write_sources(health):
    """data/results/sources.json: each wired state's feed status; "changed" is when that status last changed."""
    path = os.path.join(OUT_DIR, "sources.json")
    old = load(path, {}).get("states", {})
    states = {}
    for usps in sorted(set(F.SOURCES) | set(F.MEASURES)):
        h = health.get(usps, {})
        e = {"polls_close": F.POLLS_CLOSE[usps], "races": h.get("races", "none"), "measures": h.get("measures", "none")}
        was = old.get(usps) or {}
        same = {k: was.get(k) for k in e} == e
        e["changed"] = was.get("changed") if same and was.get("changed") else utc_now()
        states[usps] = e
    if states != old:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"states": states}, f, separators=(",", ":"))


def main():
    slate = load(SLATE_PATH)
    health = {}
    live = gather(F.SOURCES, health, "races")
    chambers = load(CHAMBERS_PATH, {})
    ballot = load(CANDIDATES_PATH, {})
    links = load(LINKS_PATH, {})
    ballot_races = ballot.get("races", {})
    ballot_src = {o: {"label": "Wikipedia: " + v.get("title", ""), "url": v.get("url")}
                  for o, v in (ballot.get("sources") or {}).items()}

    any_data = False
    held = []
    for office, races in slate.items():
        old_doc = load(os.path.join(OUT_DIR, office + ".json"), {})
        prev = {r.get("id"): r for r in old_doc.get("races", [])}
        out_races = []
        tally = {"dem_called": 0, "rep_called": 0, "other_called": 0,
                 "dem_lead": 0, "rep_lead": 0, "other_lead": 0, "undecided": 0}
        for race in races:
            rc = dict(race)
            if ballot_races.get(rc["id"]):   # names on the ballot, shown until results arrive (never as votes)
                rc["ballot"] = ballot_races[rc["id"]]
            # a wired feed that can't be read once polls have closed reads as "No live feed", with the official link
            rc["feed"] = rc["state"] in F.SOURCES and not str(health.get(rc["state"], {}).get("races", "")).startswith("failed")
            if links.get(rc["state"]):
                rc["official_url"] = links[rc["state"]]
            if apply_feed(rc, live.get(rc["state"], {}).get(rc["id"]), prev.get(rc["id"]), ballot_races.get(rc["id"]), held):
                any_data = True
            called = rc.get("called")
            lead = rc.get("leader_party") or leader(rc.get("candidates") or [])
            if called in ("D", "R"):
                tally[("dem" if called == "D" else "rep") + "_called"] += 1
            elif called:
                tally["other_called"] += 1
            elif lead == "D":
                tally["dem_lead"] += 1
            elif lead == "R":
                tally["rep_lead"] += 1
            elif lead:
                tally["other_lead"] += 1
            else:
                tally["undecided"] += 1
            out_races.append(rc)
        d = tally["dem_called"] + tally["dem_lead"]
        r = tally["rep_called"] + tally["rep_lead"]
        other = tally["other_called"] + tally["other_lead"]
        now = utc_now()
        out = {
            "office": office, "election": {"date": "2026-11-03", "name": "2026 General"},
            "generated_at": now,
            "updated": (now if any(rr.get("candidates") or rr.get("called") for rr in out_races) else ""),
            "note": "Live results from official state feeds where one can be read; other states link to their official results.",
            "summary": {"total": len(out_races), "called_dem": tally["dem_called"], "called_rep": tally["rep_called"],
                        "called_other": tally["other_called"], "uncalled": tally["undecided"] + tally["dem_lead"] + tally["rep_lead"] + tally["other_lead"],
                        "no_feed": sum(1 for x in out_races if not x["feed"])},
            "races": out_races,
        }
        if office in ballot_src:
            out["ballot_source"] = ballot_src[office]
        if office in chambers:
            ch = chambers[office]; nu = ch.get("not_up", {"D": 0, "R": 0, "other": 0})
            out["balance"] = {
                "total": ch.get("total"), "control": ch.get("control"), "not_up": nu,
                "dem_called": nu.get("D", 0) + tally["dem_called"], "rep_called": nu.get("R", 0) + tally["rep_called"],
                "other_called": nu.get("other", 0) + tally["other_called"],
                "dem_lead": tally["dem_lead"], "rep_lead": tally["rep_lead"], "other_lead": tally["other_lead"],
                "dem_total": nu.get("D", 0) + tally["dem_called"] + tally["dem_lead"],
                "rep_total": nu.get("R", 0) + tally["rep_called"] + tally["rep_lead"],
                "other_total": nu.get("other", 0) + tally["other_called"] + tally["other_lead"],
                "undecided": tally["undecided"],
            }
        wrote = write_if_changed(office, out)
        print("%-9s races=%d  D=%d R=%d other=%d undecided=%d%s" % (office, len(out_races), d, r, other, tally["undecided"], "" if wrote else "  (no change)"))
    if not any_data:
        print("no results yet (feeds wait for each state's polls to close).")
    if held:
        print("held back: " + ", ".join(held))
    write_measures(health, links)
    write_sources(health)
    return 0


if __name__ == "__main__":
    sys.exit(main())
