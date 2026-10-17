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
import re
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


GUARDED = ("latest.json", "history.jsonl")


def read_files(code):
    out = {}
    for name in GUARDED:
        try:
            with open(os.path.join(ROOT, "data", code, name), "rb") as f:
                out[name] = f.read()
        except OSError:
            out[name] = None
    return out


def cast_of(raw):
    try:
        d = json.loads(raw)
        return int(((d.get("statewide") or {}).get("cast") or {}).get("total") or 0), d
    except (TypeError, ValueError):
        return None, {}


def held_back(code, before):
    """A connector that hit an empty or half-loaded source (MT's Tableau mid-
    refresh on 10/17 returned no counties; NV's Lab file on 10/15 was half
    written) must not replace real figures. If ballots cast fell to zero, or
    by more than 20% (lab_standin.suspicious_drop: held up to 36 hours), put
    the previous files back. -> reason string or None."""
    import lab_standin as LAB
    old_cast, old = cast_of(before.get("latest.json"))
    if not old_cast:
        return None
    after = read_files(code)
    if after.get("latest.json") == before.get("latest.json"):
        return None
    new_cast, new = cast_of(after.get("latest.json"))
    if new.get("frozen"):
        return None
    reason = None
    if not new_cast:
        reason = "the source returned no ballots (was %s); holding the previous figures" % format(old_cast, ",")
    else:
        reason = LAB.suspicious_drop(old, new_cast)
    if not reason:
        return None
    for name, raw in before.items():
        p = os.path.join(ROOT, "data", code, name)
        if raw is None:
            if os.path.exists(p):
                os.remove(p)
        else:
            with open(p, "wb") as f:
                f.write(raw)
    return reason


def main():
    reg = json.load(open(os.path.join(ROOT, "assets", "states.json"), encoding="utf-8"))
    codes = [s["code"] for s in reg.get("states", [])]
    health = {}
    prev_health = {}
    try:
        with open(os.path.join(ROOT, "data", "_health.json"), encoding="utf-8") as f:
            prev_health = json.load(f).get("states", {})
    except (OSError, ValueError):
        pass
    ok = changed = errors = frozen = 0
    for code in codes:
        if os.path.exists(os.path.join(ROOT, "data", code, "_frozen.json")):
            health[code] = {"status": "frozen", "checked_at": C.utc_now_iso()}
            frozen += 1
            continue
        rec = {"checked_at": C.utc_now_iso()}
        before = latest_hash(code)
        saved = read_files(code)
        try:
            mod = importlib.import_module("%s_update" % code)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                rc = mod.main()
            after = latest_hash(code)
            out = buf.getvalue()
            skipped = any(m in out for m in ("checked under", "next check not due", "next window not reached"))
            last = prev_health.get(code) or {}
            if rc in (0, None) and skipped and last.get("status") == "error":
                # a throttled run didn't check the source: keep the last real result visible
                rec.update(status="error", error=last.get("error", ""), last_error_at=last.get("last_error_at",
                                                                                              last.get("checked_at")))
                errors += 1
            elif rc in (0, None):
                rec["status"] = "ok"; ok += 1
            else:
                msgs = [l.strip() for l in out.splitlines() if re.search(r"unavailable|failed|error|!", l, re.I)]
                rec["status"] = "error"; rec["error"] = (msgs[-1][:200] if msgs else "exit %s" % rc); errors += 1
                rec["last_error_at"] = rec["checked_at"]
            held = held_back(code, saved)
            if held:
                after = before
                if rec.get("status") == "ok":
                    ok -= 1; errors += 1
                rec.update(status="error", error="held: " + held, last_error_at=rec["checked_at"])
                print("!! %s %s" % (code, held), file=sys.stderr)
            rec["changed"] = (before != after)
            if rec["changed"]:
                changed += 1
        except Exception as e:  # noqa: BLE001 - one bad connector must not sink the rest
            rec["status"] = "error"; rec["error"] = str(e)[:200]; rec["last_error_at"] = rec["checked_at"]; errors += 1
            print("!! %s failed: %s" % (code, str(e)[:140]), file=sys.stderr)
        health[code] = rec
    with open(os.path.join(ROOT, "data", "_health.json"), "w", encoding="utf-8") as f:
        json.dump({"generated_at": C.utc_now_iso(), "states": health}, f, separators=(",", ":"))
    print("state run: ok=%d changed=%d frozen=%d errors=%d" % (ok, changed, frozen, errors))
    return 0


if __name__ == "__main__":
    sys.exit(main())
