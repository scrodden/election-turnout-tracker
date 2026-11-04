#!/usr/bin/env python3
"""Update live 2026 election results (House / Senate / Governor / ballot measures).

Reads the canonical race slate (config/results_races.json) and writes
data/results/{senate,governor,house,measures}.json for the results page, filling
candidates/votes/reporting from each state's official feed (scripts/results_feeds.py), or
for a race no official feed covers, from civicAPI, a free third party, labeled as such.
It NEVER fabricates results, and it guards what it publishes:
  - a state's feed isn't read until its polls close (feeds carry test data before that),
    and civicAPI's counts aren't used while it flags them as test data;
  - a feed that fails or drops a race keeps the last figures we published;
  - a race whose votes suddenly fall by over 20% keeps its last figures, for up to 3 hours;
  - a race with no one from the 2026 ballot is rejected (a feed showing another election);
  - civicAPI's race calls stay apart from "called": labeled, and never counted toward control.
A file is rewritten only when its figures change, so "updated" is when results last moved
and the 2-minute Election-Day loop commits only real changes.

house.json also carries "national_vote", every Democrat's and Republican's votes added up, and
data/results/house_vote_series.json keeps one point each time that total moves.

data/results/sources.json says what each state's feed is doing (for the status page).

Run:  python scripts/results_update.py
"""
import os
import re
import sys
import json
from concurrent.futures import ThreadPoolExecutor
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
HOUSE_PAST_PATH = os.path.join(ROOT, "config", "house_vote_past.json")   # official past House votes (build_house_vote_past.py)
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

    def read(usps):
        try:
            live[usps] = registry[usps]() or {}
            health.setdefault(usps, {})[key] = "ok"
        except Exception as e:  # noqa: BLE001
            print("  ! %s %s feed failed: %s" % (usps, key, str(e)[:100]), file=sys.stderr)
            health.setdefault(usps, {})[key] = "failed: " + str(e)[:100]
    due = []
    for usps in registry:
        if F.polls_closed(usps):
            due.append(usps)
        else:
            health.setdefault(usps, {})[key] = "waiting"
    with ThreadPoolExecutor(max_workers=12) as pool:   # a slow feed doesn't hold up the others
        list(pool.map(read, due))
    return live


def race_total(r):
    # a feed's "Totals" row published as a candidate (Maryland, before 11/4) isn't votes: dropping it isn't a fall
    return sum(c.get("votes", 0) for c in (r or {}).get("candidates") or []
               if not re.match(r"(?i)totals?$", str(c.get("name") or "").strip()))


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
    if use.get("via"):
        rc["via"] = use["via"]
    return True


def apply_call(rc, civic, old):
    """civicAPI's call for a race, kept apart from rc["called"] so it never counts toward control. Shown only while the
    called candidate leads the figures on the page; a contradiction is logged and left off. If civicAPI is down, the
    last call stays; if it withdraws a call, the call goes."""
    call = civic["calls"].get(rc["id"]) if civic.get("ok") else (old or {}).get("civic_call")
    if not call or not rc.get("candidates"):
        return
    lead = max(rc["candidates"], key=lambda c: c.get("votes", 0))
    if F.same_person(call.get("name"), lead.get("name")):
        rc["civic_call"] = {"name": lead.get("name"), "party": lead.get("party")}
    else:
        print("  ! %s: civicAPI calls %s but %s leads here; call left off" % (rc["id"], call.get("name"), lead.get("name")), file=sys.stderr)


def read_civic(health):
    """civicAPI's roster and any live counts. If it's down, races it would fill keep their last figures."""
    try:
        civic = F.civic_fetch()
        health["_civic"] = "ok: %d races and measures listed, live in %d states" % (len(civic["roster"]), len(civic["live"]))
        civic["ok"] = True
        return civic
    except Exception as e:  # noqa: BLE001
        print("  ! civicAPI failed: %s" % str(e)[:100], file=sys.stderr)
        health["_civic"] = "failed: " + str(e)[:100]
        return {"roster": set(), "races": {}, "measures": {}, "calls": {}, "live": set(), "ok": False}


def write_measures(health, links, civic):
    slate = load(MEASURES_PATH, {}).get("measures", [])
    live = gather(F.MEASURES, health, "measures")
    held = []
    old_doc = load(os.path.join(OUT_DIR, "measures.json"), {})
    prev = {m.get("id"): m for m in old_doc.get("races", [])}
    passed = failed = und = 0
    out = []
    for m in slate:
        r = dict(m); r["office"] = "Ballot Measure"
        r["official_feed"] = m.get("state") in F.MEASURES and not str(health.get(m.get("state"), {}).get("measures", "")).startswith("failed")
        r["feed"] = r["official_feed"] or m.get("id") in civic["roster"]
        if links.get(m.get("state")):
            r["official_url"] = links[m["state"]]
        src = live.get(m.get("state"), {}).get(m.get("id")) or civic["measures"].get(m.get("id"))
        old = prev.get(m.get("id")) or {}
        was = sum(c.get("votes") or 0 for c in old.get("candidates") or [])
        if src and was >= 1000 and src.get("yes", 0) + src.get("no", 0) < 0.8 * was:
            held.append(m.get("id"))
            src = None
        yes = no = None; reporting = called = None
        if src:
            yes = int(src.get("yes", 0)); no = int(src.get("no", 0))
            reporting = src.get("reporting"); called = src.get("called")
            if src.get("via"):
                r["via"] = src["via"]
        elif old.get("candidates"):   # a feed hiccup: keep the last figures
            yes, no = (c.get("votes") for c in old["candidates"][:2])
            reporting, called = old.get("reporting_pct"), old.get("called")
            if old.get("via"):
                r["via"] = old["via"]
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


def two_party_margin(rep, dem):
    """R minus D in points of their combined vote; + = Republican. Only R + D, because the Clerk's past state totals include
    blank ballots in some states (Massachusetts) and the live feeds don't."""
    return round(100.0 * (rep - dem) / (rep + dem), 2) if rep + dem else None


def house_vote(races, past):
    """The national House vote so far, from the figures published for each race. A feed's unreadable party label falls
    back to the ballot list's party for that candidate.

    "same_states" answers "is this good or bad for this hour?": each state's past margin, weighted by the votes it has
    counted so far, for the states counted so far. A state where one party had no votes that year is left out of that year."""
    rep = dem = other = via_civic = districts = no_rep = no_dem = 0
    by_state = {}
    for r in races:
        on_ballot = {F.party(b.get("party")) for b in r.get("ballot") or []}
        if on_ballot and "REP" not in on_ballot:
            no_rep += 1
        if on_ballot and "DEM" not in on_ballot:
            no_dem += 1
        cands = r.get("candidates") or []
        if not race_total(r):
            continue
        districts += 1
        ballot_party = {F._surname(b.get("name", "")): F.party(b.get("party")) for b in r.get("ballot") or []}
        st = by_state.setdefault(r["state"], [0, 0])
        for c in cands:
            p, v = F.party(c.get("party")), c.get("votes", 0)
            if p == "OTH":
                p = ballot_party.get(F._surname(c.get("name", "")), p)
            if p == "REP":
                rep += v
                st[0] += v
            elif p == "DEM":
                dem += v
                st[1] += v
            else:
                other += v
        if r.get("via"):
            via_civic += race_total(r)
    two = rep + dem
    same = {}
    for year, y in past.items():
        weight = total = 0.0
        for usps, (r_now, d_now) in by_state.items():
            s = y["states"].get(usps) or {}
            if s.get("rep") and s.get("dem"):
                weight += r_now + d_now
                total += (r_now + d_now) * two_party_margin(s["rep"], s["dem"])
        if weight:
            same[year] = round(total / weight, 2)
    return {"rep": rep, "dem": dem, "other": other, "margin": two_party_margin(rep, dem),
            "rep_pct": round(100.0 * rep / two, 1) if two else None, "dem_pct": round(100.0 * dem / two, 1) if two else None,
            "other_pct": round(100.0 * other / (two + other), 1) if two + other else None,
            "districts": districts, "of": len(races), "states": len(by_state), "via_civicapi": via_civic,
            "no_republican": no_rep, "no_democrat": no_dem, "same_states": same,
            "past": {year: {"margin": two_party_margin(y["national"]["rep"], y["national"]["dem"]), "rep": y["national"]["rep"],
                            "dem": y["national"]["dem"], "source": y["source"], "url": y["url"]} for year, y in past.items()}}


def add_house_vote_point(nv, now):
    """data/results/house_vote_series.json: one point each time the national House vote moves, for the page's chart."""
    if not nv["rep"] + nv["dem"]:
        return
    path = os.path.join(OUT_DIR, "house_vote_series.json")
    doc = load(path, {"fields": ["t", "dem", "rep", "other", "districts", "same_states_2024"], "points": []})
    point = [now, nv["dem"], nv["rep"], nv["other"], nv["districts"], nv["same_states"].get("2024")]
    if doc["points"] and doc["points"][-1][1:] == point[1:]:
        return
    doc["points"].append(point)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"))


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
    civic_state = str(health.get("_civic", ""))
    if states != old or civic_state != load(path, {}).get("civicapi"):
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"states": states, "civicapi": civic_state}, f, separators=(",", ":"))


def main():
    slate = load(SLATE_PATH)
    health = {}
    live = gather(F.SOURCES, health, "races")
    civic = read_civic(health)
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
            # a wired feed that can't be read once polls have closed reads as "No live feed", with the official link,
            # unless civicAPI carries the race
            rc["official_feed"] = rc["state"] in F.SOURCES and not str(health.get(rc["state"], {}).get("races", "")).startswith("failed")
            rc["feed"] = rc["official_feed"] or rc["id"] in civic["roster"]
            if links.get(rc["state"]):
                rc["official_url"] = links[rc["state"]]
            rc["polls_close"] = F.POLLS_CLOSE.get(rc["state"])
            src = live.get(rc["state"], {}).get(rc["id"]) or civic["races"].get(rc["id"])   # official first
            if apply_feed(rc, src, prev.get(rc["id"]), ballot_races.get(rc["id"]), held):
                any_data = True
            apply_call(rc, civic, prev.get(rc["id"]))
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
        if office == "house":
            out["national_vote"] = house_vote(out_races, load(HOUSE_PAST_PATH, {}).get("years", {}))
            add_house_vote_point(out["national_vote"], now)
        wrote = write_if_changed(office, out)
        print("%-9s races=%d  D=%d R=%d other=%d undecided=%d%s" % (office, len(out_races), d, r, other, tally["undecided"], "" if wrote else "  (no change)"))
    if not any_data:
        print("no results yet (feeds wait for each state's polls to close).")
    if held:
        print("held back: " + ", ".join(held))
    write_measures(health, links, civic)
    write_sources(health)
    return 0


if __name__ == "__main__":
    sys.exit(main())
