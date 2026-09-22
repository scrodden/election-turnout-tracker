#!/usr/bin/env python3
"""Run every state's turnout connector once and record per-state health.

Replaces the long list of per-state workflow steps with a single, health-tracked
pass: for each state in assets/states.json (skipping frozen ones) it runs the
connector's main(), captures success/failure and whether the data changed, and
writes data/_health.json {generated_at, states:{code:{status,checked_at,changed,
error}}}. build_status.py folds this into status.json so the Feed-Status page can
show "last checked" for every state and flag any feed that actually errored
(instead of a silent failure looking the same as "source unchanged").

Every state is checked every cycle, so a state updates as soon as its source
publishes new data; this file makes that provable and surfaces breakage.

Run:  python scripts/run_all_states.py
"""
import os
import sys
import io
import json
import contextlib
import importlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common as C  # noqa: E402


def latest_hash(code):
    try:
        with open(os.path.join(ROOT, "data", code, "latest.json"), encoding="utf-8") as f:
            return json.load(f).get("data_hash")
    except (OSError, ValueError):
        return None


def main():
    reg = json.load(open(os.path.join(ROOT, "assets", "states.json"), encoding="utf-8"))
    codes = [s["code"] for s in reg.get("states", [])]
    health = {}
    ok = changed = errors = frozen = 0
    for code in codes:
        if os.path.exists(os.path.join(ROOT, "data", code, "_frozen.json")):
            health[code] = {"status": "frozen", "checked_at": C.utc_now_iso()}
            frozen += 1
            continue
        rec = {"checked_at": C.utc_now_iso()}
        before = latest_hash(code)
        try:
            mod = importlib.import_module("%s_update" % code)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                rc = mod.main()
            after = latest_hash(code)
            if rc in (0, None):
                rec["status"] = "ok"; ok += 1
            else:
                rec["status"] = "error"; rec["error"] = "exit %s" % rc; errors += 1
            rec["changed"] = (before != after)
            if rec["changed"]:
                changed += 1
        except Exception as e:  # noqa: BLE001 - one bad connector must not sink the rest
            rec["status"] = "error"; rec["error"] = str(e)[:200]; errors += 1
            print("!! %s failed: %s" % (code, str(e)[:140]), file=sys.stderr)
        health[code] = rec
    with open(os.path.join(ROOT, "data", "_health.json"), "w", encoding="utf-8") as f:
        json.dump({"generated_at": C.utc_now_iso(), "states": health}, f, separators=(",", ":"))
    print("state run: ok=%d changed=%d frozen=%d errors=%d" % (ok, changed, frozen, errors))
    return 0


if __name__ == "__main__":
    sys.exit(main())
