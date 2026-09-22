#!/usr/bin/env python3
"""Build data/status.json — a single manifest of every state's turnout feed status
and the results feeds. The front end fetches this ONE file (instead of probing all
~45 hidden states) to decide which staged states to reveal, and the data-status
page renders it. Regenerated each workflow cycle after the connectors run.
"""
import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATES_PATH = os.path.join(ROOT, "assets", "states.json")


def load(p, default=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    reg = load(STATES_PATH, {}) or {}
    health = (load(os.path.join(ROOT, "data", "_health.json"), {}) or {}).get("states", {})
    out_states = []
    live = 0
    for s in reg.get("states", []):
        code = s.get("code")
        d = load(os.path.join(ROOT, *s.get("data", "data/%s/latest.json" % code).split("/"))) or {}
        sw = d.get("statewide", {}) or {}
        castb = sw.get("cast") or {}
        cast = int(castb.get("total", 0) or 0)
        methods = d.get("methods_present", []) or []
        frozen = os.path.exists(os.path.join(ROOT, "data", code, "_frozen.json"))
        has_data = cast > 0 or bool(methods)
        if has_data:
            live += 1
        out_states.append({
            "code": code, "name": s.get("name"), "partisan": s.get("partisan", True) is not False,
            "hidden": bool(s.get("hidden")), "has_data": has_data, "frozen": frozen,
            "cast": cast, "registered": int(sw.get("registered", 0) or 0),
            "rep": int(castb.get("rep", 0) or 0), "dem": int(castb.get("dem", 0) or 0),
            "npa": int(castb.get("npa", 0) or 0), "oth": int(castb.get("oth", 0) or 0),
            "margin": castb.get("margin"),
            "turnout_pct": sw.get("turnout_pct"), "methods": methods,
            "updated": d.get("generated_at", ""), "source_compiled": d.get("source_compiled", ""),
            "checked_at": health.get(code, {}).get("checked_at", ""),
            "feed_status": health.get(code, {}).get("status", ""),
            "feed_error": health.get(code, {}).get("error", ""),
        })

    results = {}
    for office in ("senate", "governor", "house"):
        r = load(os.path.join(ROOT, "data", "results", office + ".json")) or {}
        results[office] = {"updated": r.get("updated", ""), "summary": r.get("summary", {}),
                           "balance": r.get("balance")}

    hgen = (load(os.path.join(ROOT, "data", "_health.json"), {}) or {}).get("generated_at", "")
    errs = sum(1 for s in out_states if s.get("feed_status") == "error")
    status = {"generated_at": now(), "checked_at": hgen or now(),
              "election": {"date": "2026-11-03", "name": "2026 General"},
              "counts": {"total": len(out_states), "live": live, "pending": len(out_states) - live, "errors": errs},
              "states": out_states, "results": results}
    with open(os.path.join(ROOT, "data", "status.json"), "w", encoding="utf-8") as f:
        json.dump(status, f, separators=(",", ":"))
    print("status.json: %d states (%d live, %d pending)" % (len(out_states), live, len(out_states) - live))
    return 0


if __name__ == "__main__":
    sys.exit(main())
