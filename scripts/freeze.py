#!/usr/bin/env python3
"""Stop auto-updating and archive each tracked state once its 2026 general
results are certified.

Certification isn't published as a machine-readable signal, so a state is frozen
when BOTH of these are true (see config/freeze.json):

  1) the calendar has passed that state's `certified_after` date, AND
  2) its live data has been unchanged for >= `stale_days` days
     (i.e. `data/<st>/latest.json`'s `generated_at` -- which only advances when
     the numbers actually change -- is that old, meaning the count is final).

The staleness gate means the configured dates only have to be *roughly* right:
set them a touch early and the freeze still waits until the returns stop moving.
At `hard_stop` any state still live is frozen unconditionally, so the whole
system halts even if a feed keeps twitching.

On freeze, for each state:
  - snapshot data/<st>/latest.json  ->  data/<st>/final_2026_general.json  (once, immutable)
  - stamp latest.json with {"frozen": true, "frozen_at": ...} so the site shows
    a "final / archived" banner and stops implying it is live
  - write data/<st>/_frozen.json  (the update workflow skips a state's fetch when
    this marker exists, so latest.json is never overwritten after certification)

When every tracked state is frozen, write data/_frozen_all.json; the workflow's
gate step reads that and makes all later runs no-op entirely.

Idempotent -- safe to run every cycle. Reads the state list from
assets/states.json, so new states are covered automatically.

Run:  python scripts/freeze.py            (normal; used by the workflow)
      python scripts/freeze.py --status   (report only, change nothing)
      python scripts/freeze.py --force <st>[ <st>...]   (freeze now, ignore gates)
"""
import os
import sys
import json
import shutil
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATES_PATH = os.path.join(ROOT, "assets", "states.json")
FREEZE_CFG_PATH = os.path.join(ROOT, "config", "freeze.json")

DEFAULTS = {
    "stale_days": 4,
    "hard_stop": "2026-12-15",
    "default_certified_after": "2026-11-25",
    "certified_after": {},
}


def load_json(p, default=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def parse_iso(s):
    """Parse 'YYYY-MM-DDTHH:MM:SSZ' or 'YYYY-MM-DD' -> aware UTC datetime, or None."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def cast_total(latest):
    try:
        return int(latest.get("statewide", {}).get("cast", {}).get("total", 0) or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


def main():
    argv = sys.argv[1:]
    status_only = "--status" in argv
    force_states = []
    if "--force" in argv:
        force_states = [a.lower() for a in argv[argv.index("--force") + 1:] if not a.startswith("-")]

    cfg = {**DEFAULTS, **(load_json(FREEZE_CFG_PATH, {}) or {})}
    stale_days = cfg.get("stale_days", 4)
    hard_stop = parse_iso(cfg.get("hard_stop"))
    default_cert = cfg.get("default_certified_after")
    cert_map = cfg.get("certified_after", {}) or {}

    registry = load_json(STATES_PATH, {}) or {}
    states = registry.get("states", [])
    now = datetime.now(timezone.utc)

    all_frozen = True
    froze_now = []
    for entry in states:
        code = entry.get("code")
        if not code:
            continue
        data_path = os.path.join(ROOT, *entry.get("data", "data/%s/latest.json" % code).split("/"))
        state_dir = os.path.dirname(data_path)
        marker = os.path.join(state_dir, "_frozen.json")
        archive = os.path.join(state_dir, "final_2026_general.json")

        already = os.path.exists(marker)
        latest = load_json(data_path)

        cert_after = parse_iso(cert_map.get(code, default_cert))
        past_cert = bool(cert_after and now >= cert_after)
        past_hard = bool(hard_stop and now >= hard_stop)

        stale_ok = False
        days_since = None
        if latest:
            gen = parse_iso(latest.get("generated_at"))
            if gen:
                days_since = (now - gen).total_seconds() / 86400.0
                stale_ok = days_since >= stale_days

        has_data = bool(latest) and cast_total(latest) > 0
        forced = code in force_states

        # Decide whether this state should be frozen now.
        should = already or forced
        if not should and past_cert and stale_ok and has_data:
            should = True            # normal path: certified + returns stopped moving
        if not should and past_hard and latest is not None:
            should = True            # backstop: everything halts by hard_stop

        if status_only:
            state = ("FROZEN" if already else ("READY" if should else "live"))
            print("  %-3s %-7s cert_after=%s past_cert=%s stale=%s(%s d) data=%s"
                  % (code, state, cert_map.get(code, default_cert), past_cert,
                     stale_ok, ("%.1f" % days_since) if days_since is not None else "?",
                     "yes" if has_data else "no"))
            all_frozen = all_frozen and already
            continue

        if not should:
            all_frozen = False
            continue

        if not already:
            os.makedirs(state_dir, exist_ok=True)
            if latest is not None and not os.path.exists(archive):
                shutil.copyfile(data_path, archive)
            # stamp latest.json so the front end shows the archived banner
            if latest is not None:
                latest["frozen"] = True
                latest["frozen_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
                with open(data_path, "w", encoding="utf-8") as f:
                    json.dump(latest, f, separators=(",", ":"))
            with open(marker, "w", encoding="utf-8") as f:
                json.dump({
                    "state": code,
                    "frozen_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "certified_after": cert_map.get(code, default_cert),
                    "reason": ("forced" if forced else ("hard_stop" if (past_hard and not (past_cert and stale_ok and has_data)) else "certified+stable")),
                    "cast": cast_total(latest) if latest else 0,
                    "generated_at": (latest or {}).get("generated_at", ""),
                    "archive": os.path.basename(archive) if (latest is not None) else None,
                    "no_data": latest is None,
                }, f, indent=2)
            froze_now.append(code)
            print("FROZE %s (%s) cast=%s archive=%s"
                  % (code, ("forced" if forced else "certified+stable" if has_data else "hard_stop/no-data"),
                     cast_total(latest) if latest else 0,
                     os.path.basename(archive) if latest is not None else "(none)"))

    if status_only:
        print("all_frozen=%s" % all_frozen)
        return 0

    all_flag = os.path.join(ROOT, "data", "_frozen_all.json")
    if all_frozen and states:
        if not os.path.exists(all_flag):
            with open(all_flag, "w", encoding="utf-8") as f:
                json.dump({"frozen_all_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "states": [s.get("code") for s in states]}, f, indent=2)
            print("ALL STATES FROZEN -> wrote data/_frozen_all.json (workflow will now no-op)")
    if froze_now:
        print("Froze this run: %s" % ", ".join(froze_now))
    else:
        print("No new freezes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
