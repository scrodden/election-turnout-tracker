#!/usr/bin/env python3
"""Read Georgia's SoS Election Data Hub from Sam's PC and push the result.

Why: since 10/18 the hub's CloudFront refuses GitHub's runners (403 "The request
could not be satisfied") but not a home connection, so the 10-minute workflow
can only show the UF Election Lab's daily figures for Georgia. This script runs
from a Windows scheduled task (every ~20 minutes while Sam is logged on, through
Nov 10) in a dedicated clone that nothing else edits:

  1. sync the clone to origin/main
  2. run scripts/ga_update.py (reads the hub; writes data/ga/*)
  3. commit only data/ga/* and push (retrying if the workflow pushed meanwhile)

The workflow keeps these hub figures while they're under an hour old
(ga_update.recent_hub_read); if this stops (PC off, asleep, hub down), it falls
back to the Lab and flags Georgia on the status page.

Run by hand:  python scripts/local_ga_reader.py [--clone <dir>]
Log:          <clone>/../ga-reader.log (last ~500 lines)
"""
import datetime as dt
import os
import subprocess
import sys

REPO_URL = "https://github.com/scrodden/election-turnout-tracker.git"
DEFAULT_CLONE = os.path.join(os.path.expanduser("~"), ".claude", "projects", "Politics", "ga-reader", "election-turnout-tracker")
STOP_AFTER = dt.date(2026, 11, 10)
GA_FILES = ["data/ga/latest.json", "data/ga/history.jsonl", "data/ga/race_share.json", "data/ga/districts.json"]


def log(msg):
    line = "%s  %s" % (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:   # pythonw has no console; a cp1252 console can't print every character
        print(line.encode("ascii", "replace").decode("ascii"))
    except Exception:  # noqa: BLE001
        pass
    path = os.path.join(os.path.dirname(CLONE), "ga-reader.log")
    try:
        old = open(path, encoding="utf-8").read().splitlines()[-499:] if os.path.exists(path) else []
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(old + [line]) + "\n")
    except OSError:
        pass


def git(*args, check=True):
    r = subprocess.run(["git", "-C", CLONE] + list(args), capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode:
        raise RuntimeError("git %s: %s" % (" ".join(args), (r.stderr or r.stdout).strip()[:300]))
    return r


def main():
    if dt.date.today() > STOP_AFTER:
        log("past %s -- nothing to do" % STOP_AFTER)
        return 0
    if not os.path.isdir(os.path.join(CLONE, ".git")):
        os.makedirs(os.path.dirname(CLONE), exist_ok=True)
        r = subprocess.run(["git", "clone", "--quiet", REPO_URL, CLONE], capture_output=True, text=True)
        if r.returncode:
            log("clone failed: %s" % r.stderr.strip()[:300])
            return 1
    try:
        git("fetch", "--quiet", "origin", "main")
        git("reset", "--quiet", "--hard", "origin/main")   # dedicated clone: always start from the published state
        # --force: read on every run (the task's 20-minute timer would otherwise land just inside
        # ga_update's 20-minute throttle and skip every other run); each read refreshes
        # source.fetched_at, which keeps the workflow from switching to the Lab fallback
        hist = os.path.join(CLONE, "data", "ga", "history.jsonl")
        hist0 = os.path.getsize(hist) if os.path.exists(hist) else 0
        run = subprocess.run([sys.executable, os.path.join(CLONE, "scripts", "ga_update.py"), "--force"], cwd=CLONE,
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
        out = (run.stdout + run.stderr).strip().splitlines()
        summary = next((l for l in reversed(out) if l.startswith(("CHANGED", "NOCHANGE", "ga:"))), out[-1] if out else "")
        import json
        src = (json.load(open(os.path.join(CLONE, "data", "ga", "latest.json"), encoding="utf-8")).get("source") or {})
        if run.returncode or not str(src.get("primary", "")).startswith("GA SoS"):
            log("hub read failed (rc %s): %s" % (run.returncode, " | ".join(out[-3:])[:400]))
            return 1
        try:   # each method's totals on the new history line, as run_all_states does for the workflow
            sys.path.insert(0, os.path.join(CLONE, "scripts"))
            import common
            common.enrich_history(os.path.join(CLONE, "data", "ga"), hist0)
        except Exception as e:  # noqa: BLE001
            log("history not enriched: %s" % str(e)[:200])
        git("add", "--", *[f for f in GA_FILES if os.path.exists(os.path.join(CLONE, f))])
        if not git("diff", "--cached", "--quiet", check=False).returncode:
            log("no change: %s" % summary[:200])
            return 0
        git("-c", "user.name=ga-local-reader", "-c", "user.email=actions@users.noreply.github.com",
            "commit", "--quiet", "-m", "Georgia hub (local reader) %s UTC" % dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M"))
        for attempt in range(4):
            if not git("push", "--quiet", "origin", "HEAD:main", check=False).returncode:
                log("pushed: %s" % summary[:200])
                return 0
            # the workflow pushed in between: replay this commit on top (our data/ga wins)
            git("fetch", "--quiet", "origin", "main")
            r = git("rebase", "-X", "theirs", "origin/main", check=False)
            if r.returncode:
                git("rebase", "--abort", check=False)
                log("rebase failed: %s" % (r.stderr or r.stdout).strip()[:200])
                return 1
        log("push failed after retries")
        return 1
    except Exception as e:  # noqa: BLE001
        log("error: %s" % str(e)[:300])
        return 1


if __name__ == "__main__":
    CLONE = DEFAULT_CLONE
    if "--clone" in sys.argv:
        CLONE = sys.argv[sys.argv.index("--clone") + 1]
    sys.exit(main())
