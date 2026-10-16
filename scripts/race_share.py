"""Ballots by date and race, for the turnout page's "share of ballots over time"
chart (data/<st>/race_share.json). Georgia builds its own series from the
SoS hub (ga_update.race_by_day); this is the shared form for connectors that
read voter-level files (North Carolina).

    days = DayCounter(election_day)
    for each counted ballot: days.add(return_date, race_label)
    ser = days.series(stop=min(election_day, today))
    write(path, "nc", {"2026": ser, "2022": old}, markers, source, as_of)
"""
import json
import os
from datetime import timedelta

import common as C

UNKNOWN = ("Unknown", "Not designated", "Other/Unknown")   # sorted last


class DayCounter:
    def __init__(self, election_day):
        self.eday = election_day
        self.days, self.extra = {}, {}

    def add(self, day, race, n=1):
        """day: date or None (no / unreadable date)."""
        if day is None:
            self.extra[race] = self.extra.get(race, 0) + n
            return
        b = self.days.setdefault(day, {})
        b[race] = b.get(race, 0) + n

    def series(self, stop):
        """-> {"election_day", "dates", "counts": {race: [per day]}, "total"} or None.
        Ballots with no date, or dated after `stop` (late-arriving mail, stray
        future dates) go into the last day; dates more than 120 days before
        Election Day (mistyped years) into the first, so the total matches the
        file's ballot count."""
        days = {}
        extra = dict(self.extra)
        early = {}
        for d, b in self.days.items():
            tgt = extra if d > stop else early if (self.eday and d < self.eday - timedelta(days=120)) else days.setdefault(d, {})
            for r, v in b.items():
                tgt[r] = tgt.get(r, 0) + v
        if not days:
            return None
        first, last = min(days), max(days)
        for src, d in ((early, first), (extra, last)):
            for r, v in src.items():
                days[d][r] = days[d].get(r, 0) + v
        dates = sorted(days)
        races = sorted({r for b in days.values() for r in b}, key=lambda r: (r in UNKNOWN, r))
        return {"election_day": self.eday.isoformat() if self.eday else None, "dates": [d.isoformat() for d in dates],
                "counts": {r: [days[d].get(r, 0) for d in dates] for r in races},
                "total": sum(sum(b.values()) for b in days.values())}


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write(path, state, series, markers, source, as_of, compare_default=None, compare_note=None):
    """Rewrite only when the content changes. -> True if written."""
    body = {"state": state, "series": series, "markers": markers or [], "source": source}
    if compare_default:
        body["compare_default"] = compare_default
    if compare_note:
        body["compare_note"] = compare_note
    h = C.data_hash(body)
    if h == load(path).get("data_hash"):
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(dict(body, as_of=as_of, data_hash=h), f, separators=(",", ":"))
    print("%s: race_share.json updated (%s)" % (state, ", ".join("%s: %d ballots" % (k, v["total"]) for k, v in sorted(series.items()))))
    return True
