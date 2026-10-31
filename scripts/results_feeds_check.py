#!/usr/bin/env python3
"""Check every wired results feed without publishing anything: can it be read from here, and does it
cover the ballot? For each state: races found vs. the slate, races whose names don't match the 2026 ballot,
and measures found vs. the list. Run from your PC and from GitHub (results_eday.yml's rehearsal runs it): some
sites treat GitHub's servers differently. Writes nothing; numbers seen before polls close may be test data.

Run:  python scripts/results_feeds_check.py [ST ...]
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import results_feeds as F  # noqa: E402


def main():
    F.KEEP_ZERO = True
    with open(os.path.join(ROOT, "config", "results_races.json"), encoding="utf-8") as f:
        slate = json.load(f)
    with open(os.path.join(ROOT, "config", "candidates_2026.json"), encoding="utf-8") as f:
        ballot = json.load(f).get("races", {})
    with open(os.path.join(ROOT, "config", "measures.json"), encoding="utf-8") as f:
        measures = json.load(f).get("measures", [])
    want = sys.argv[1:] or sorted(set(F.SOURCES) | set(F.MEASURES))
    pc = {}
    for st in F.CLARITY:   # read directly here rather than from the PC reader's files
        try:
            pc[st] = F.clarity_parse(st)
        except Exception as e:  # noqa: BLE001
            pc[st] = e
        def src(st0=st, kind=0):
            if isinstance(pc[st0], Exception):
                raise pc[st0]
            return pc[st0][kind]
        F.SOURCES[st], F.MEASURES[st] = (lambda st0=st: src(st0, 0)), (lambda st0=st: src(st0, 1))
    bad = 0
    print("state  races        measures   notes")
    for st in want:
        slate_ids = {r["id"] for rs in slate.values() for r in rs if r["state"] == st}
        meas_ids = {m["id"] for m in measures if m["state"] == st}
        notes = []
        rtxt = mtxt = "-"
        if st in F.SOURCES:
            try:
                got = F.SOURCES[st]()
                missing = sorted(slate_ids - set(got))
                extra = sorted(set(got) - slate_ids)
                nomatch = [r for r, v in got.items() if not F.names_match(v["candidates"], ballot.get(r))]
                rtxt = "%d/%d" % (len(set(got) & slate_ids), len(slate_ids))
                if missing:
                    notes.append("missing " + ",".join(missing[:6]))
                if extra:
                    notes.append("unknown " + ",".join(extra[:6]))
                if nomatch:
                    notes.append("names off " + ",".join(nomatch[:6]))
                if missing or extra or nomatch:
                    bad += 1
            except Exception as e:  # noqa: BLE001
                rtxt = "FAILED"
                notes.append(str(e)[:110])
                bad += 1
        else:
            notes.append("no race feed (%d races)" % len(slate_ids))
        if st in F.MEASURES:
            try:
                got = F.MEASURES[st]()
                mtxt = "%d/%d" % (len(set(got) & meas_ids), len(meas_ids))
                if meas_ids - set(got):
                    notes.append("measures missing " + ",".join(sorted(meas_ids - set(got))[:4]))
            except Exception as e:  # noqa: BLE001
                mtxt = "FAILED"
                notes.append(str(e)[:110])
        print("%-6s %-12s %-10s %s" % (st, rtxt, mtxt, "; ".join(notes)))
    print("%d states need a look" % bad)
    civic = F.civic_fetch(closed=lambda _st: True)
    all_ids = {r["id"]: r for rs in slate.values() for r in rs}
    mismatch = [i for i, v in civic["races"].items() if i in all_ids and not F.names_match(v["candidates"], ballot.get(i))]
    no_official = [i for i, r in all_ids.items() if r["state"] not in F.SOURCES]
    print("civicAPI: %d of %d races listed; of the %d races in states with no official feed, %d listed; %d of %d measures; names off: %s"
          % (len(set(all_ids) & civic["roster"]), len(all_ids), len(no_official), len(set(no_official) & civic["roster"]),
             len({m["id"] for m in measures} & civic["roster"]), len(measures), ",".join(sorted(mismatch)[:8])))
    print("  not listed, no official feed:", ",".join(sorted(set(no_official) - civic["roster"])[:30]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
