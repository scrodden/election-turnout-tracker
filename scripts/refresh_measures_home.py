#!/usr/bin/env python3
"""Rebuild config/measures.json from this PC and push it.

Ballotpedia sends GitHub's Actions runners an empty page (every CI run since
2026-10-14: 0 bytes back), but a home connection gets the full "2026 ballot
measures" article. So the CI step can't rebuild the list; this script does it
from Sam's PC once a day (Windows Task Scheduler task "Election tracker -
ballot measures refresh", which expires on Election Day; build_ballot_2026.py
stops rebuilding from 2026-11-03 10:00 UTC anyway).

It works in its own small clone (shallow, sparse: scripts/ + config/) so it
never touches a checkout someone is working in, commits only
config/measures.json, and won't commit a list that drops a state the current
list has. Log: <work dir>/refresh.log.

  python scripts/refresh_measures_home.py [--force] [--dry-run]
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

REPO = "https://github.com/scrodden/election-turnout-tracker.git"
WORK = os.environ.get("BALLOT_REFRESH_DIR", r"C:\Users\sam\.claude\projects\Politics\ballot-refresh")
CLONE = os.path.join(WORK, "repo")
LOG = os.path.join(WORK, "refresh.log")
MEAS = "config/measures.json"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # no console flashes when run by Task Scheduler


def log(msg):
    line = "%s %s" % (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"), msg)
    if sys.stdout:   # None under pythonw.exe
        print(line)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run(cmd, cwd=None, check=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=NO_WINDOW)
    if check and r.returncode:
        raise RuntimeError("%s failed: %s" % (" ".join(cmd[:3]), (r.stderr or r.stdout).strip()))
    return r


def git(*args, check=True):
    return run(["git", "-C", CLONE] + list(args), check=check)


def sync():
    """Bring the work clone to origin/main (cloning it the first time)."""
    if not os.path.isdir(os.path.join(CLONE, ".git")):
        run(["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse", REPO, CLONE])
        git("sparse-checkout", "set", "scripts", "config")
    git("fetch", "-q", "--depth", "1", "origin", "main")
    git("reset", "-q", "--hard", "FETCH_HEAD")


def states(raw):
    return {m["state"] for m in json.loads(raw).get("measures", [])}


def main():
    os.makedirs(WORK, exist_ok=True)
    force, dry = "--force" in sys.argv, "--dry-run" in sys.argv
    sync()
    target = os.path.join(CLONE, *MEAS.split("/"))
    before = open(target, "rb").read()
    py = os.path.join(os.path.dirname(sys.executable), "python.exe") if os.name == "nt" else sys.executable
    r = run([py, os.path.join(CLONE, "scripts", "build_ballot_2026.py"), "--measures"] + (["--force"] if force else []),
            cwd=CLONE, check=False)
    log("build: " + (r.stdout + r.stderr).strip().replace("\n", " | "))
    new = open(target, "rb").read()
    if r.returncode or new == before:
        return r.returncode
    lost = sorted(states(before) - states(new))
    if lost:
        log("NOT committed: the new list drops states the current one has: %s" % ", ".join(lost))
        return 1
    if dry:
        log("dry run: not committed")
        return 0
    msg = ("Ballot measures refresh (home PC) %s\n\nBallotpedia sends GitHub's runners an empty page, so the list is "
           "rebuilt from a home connection by scripts/refresh_measures_home.py." % datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    for _ in range(4):
        git("add", MEAS)
        git("commit", "-q", "-m", msg)
        if git("push", "-q", "origin", "HEAD:main", check=False).returncode == 0:
            log("pushed: %d measures in %d states" % (len(json.loads(new)["measures"]), len(states(new))))
            return 0
        sync()   # CI pushed in between: redo the commit on the new tip (CI never changes this file)
        with open(target, "wb") as f:
            f.write(new)
    log("push failed 4 times; the next run will try again")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 - log it so a scheduled run's failure is visible
        log("ERROR: %s" % e)
        sys.exit(1)
