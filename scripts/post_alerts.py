#!/usr/bin/env python3
"""Post election alerts to a Discord/Slack webhook: races called, states going
live, and national turnout milestones. DORMANT unless the ALERT_WEBHOOK env var
(a repo secret) is set. First run with a webhook seeds the sent-state without
spamming (posts a single "alerts enabled" note); later runs post only new events.
Tracks what's been sent in data/_alerts_state.json. Stdlib only.

Run:  ALERT_WEBHOOK=https://... python scripts/post_alerts.py
"""
import os
import sys
import json
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATE_PATH = os.path.join(ROOT, "data", "_alerts_state.json")
SITE = os.environ.get("SITE_URL", "https://scrodden.github.io/election-turnout-tracker/")
MILESTONES = [100000, 250000, 500000, 1000000, 2500000, 5000000, 10000000, 20000000, 40000000, 60000000]


def load(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def current_events():
    """Return {event_id: human_text} for everything currently true."""
    ev = {}
    st = load(os.path.join(ROOT, "data", "status.json"), {}) or {}
    tot = 0
    for s in st.get("states", []):
        tot += s.get("cast", 0) or 0
        if s.get("has_data"):
            ev["live:" + s["code"]] = "\U0001f7e2 %s turnout is now reporting." % s["name"]
    for m in MILESTONES:
        if tot >= m:
            ev["ms:%d" % m] = "\U0001f5f3️ National early vote passed %s ballots cast." % ("{:,}".format(m))
    names = {"senate": "U.S. Senate", "governor": "Governor", "house": "U.S. House"}
    for office in ("senate", "governor", "house"):
        doc = load(os.path.join(ROOT, "data", "results", office + ".json"), {}) or {}
        for r in doc.get("races", []):
            if r.get("called"):
                who = {"D": "Democratic", "R": "Republican"}.get(r["called"], r["called"])
                ev["call:" + r["id"]] = "✅ CALLED: %s %s%s → %s." % (
                    r.get("state_name", r.get("state", "")), r.get("office", office),
                    (" District " + r["district"]) if r.get("district") else "", who)
    return ev


def post(webhook, text):
    payload = {"content": text} if "discord" in webhook else {"text": text}
    req = urllib.request.Request(webhook, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=30).read()


def main():
    webhook = os.environ.get("ALERT_WEBHOOK", "").strip()
    if not webhook:
        print("ALERT_WEBHOOK not set — alerts dormant (skipping).")
        return 0
    ev = current_events()
    state = load(STATE_PATH, None)
    if state is None:  # first run: seed without spamming
        try:
            post(webhook, "\U0001f4e3 2026 election alerts enabled. You'll get race calls, states going live, and turnout milestones. " + SITE)
        except Exception as e:  # noqa: BLE001
            print("seed post failed: %s" % str(e)[:80], file=sys.stderr); return 0
        json.dump({"sent": sorted(ev.keys())}, open(STATE_PATH, "w", encoding="utf-8"), indent=0)
        print("seeded %d existing events (no spam)." % len(ev))
        return 0
    sent = set(state.get("sent", []))
    new = [ev[k] for k in ev if k not in sent]
    if not new:
        print("no new alerts.")
        return 0
    # order: calls first, then milestones, then live
    new.sort(key=lambda t: (0 if t.startswith("✅") else 1 if "milestone" in t or "passed" in t else 2))
    chunk = new[:15]
    body = "\n".join(chunk) + (("\n…and %d more." % (len(new) - len(chunk))) if len(new) > len(chunk) else "") + "\n" + SITE
    try:
        post(webhook, body)
    except Exception as e:  # noqa: BLE001
        print("post failed: %s" % str(e)[:80], file=sys.stderr); return 0
    json.dump({"sent": sorted(set(list(sent) + list(ev.keys())))}, open(STATE_PATH, "w", encoding="utf-8"), indent=0)
    print("posted %d new alert(s)." % len(new))
    return 0


if __name__ == "__main__":
    sys.exit(main())
