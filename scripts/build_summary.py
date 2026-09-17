#!/usr/bin/env python3
"""Build data/summary.json — a short auto-generated plain-English recap of the
current state of play (national early vote + results/balance), shown as a "brief"
card on the national page. Regenerated each workflow cycle. Stdlib only."""
import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt(n):
    return "{:,}".format(int(n or 0))


def margin_text(m):
    if m is None:
        return "even"
    return ("R+%.1f" % m) if m > 0 else ("D+%.1f" % -m) if m < 0 else "even"


def main():
    st = load(os.path.join(ROOT, "data", "status.json"), {}) or {}
    states = st.get("states", [])
    counts = st.get("counts", {})
    tot = sum(s.get("cast", 0) for s in states)
    R = sum(s.get("rep", 0) for s in states if s.get("partisan") and s.get("has_data"))
    D = sum(s.get("dem", 0) for s in states if s.get("partisan") and s.get("has_data"))
    lean = round(100.0 * (R - D) / (R + D), 1) if (R + D) else None
    live = [s for s in states if s.get("has_data")]

    bullets = []
    if tot:
        bullets.append("%s early/mail ballots cast so far across %d state%s reporting." %
                       (fmt(tot), len(live), "" if len(live) == 1 else "s"))
        if lean is not None:
            bullets.append("Across party-registration states, ballots cast lean %s by registration." % margin_text(lean))
    else:
        bullets.append("Early voting has not started reporting nationally yet — states light up here as their 2026 feeds open through October.")

    # top states by volume
    top = sorted([s for s in live], key=lambda s: s.get("cast", 0), reverse=True)[:3]
    if top and top[0].get("cast"):
        bullets.append("Most ballots cast: " + ", ".join("%s (%s)" % (s["name"], fmt(s["cast"])) for s in top) + ".")

    # results / balance
    res = st.get("results", {})
    for office, label in (("senate", "Senate"), ("house", "House")):
        b = (res.get(office) or {}).get("balance")
        s = (res.get(office) or {}).get("summary") or {}
        if b:
            if s.get("total") and s.get("uncalled", s["total"]) < s["total"]:
                bullets.append("%s: %d D / %d R called or leading, %d undecided (%d to control)." %
                               (label, b.get("dem_total", 0), b.get("rep_total", 0), b.get("undecided", 0), b.get("control", 0)))

    when = st.get("generated_at", now())[:10]
    headline = ("As of %s: %s early/mail ballots cast nationally across %d of %d states%s." %
                (when, fmt(tot), len(live), counts.get("total", len(states)),
                 (", " + margin_text(lean) + " registration lean" if lean is not None else ""))) if tot else \
        ("As of %s: the 2026 tracker is live; state feeds begin reporting through October." % when)

    out = {"generated_at": now(), "headline": headline, "bullets": bullets,
           "national": {"cast": tot, "states_reporting": len(live), "lean": lean}}
    with open(os.path.join(ROOT, "data", "summary.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    print("summary.json: " + headline)
    return 0


if __name__ == "__main__":
    sys.exit(main())
