"""Rule-table automations for writable inverter settings -- same shape as
Solar Assistant's "Rule table" automation type
(https://solar-assistant.io/help/automation/table), scoped to what this
project actually needs: a time-of-day range plus a weekday/weekend/holiday
condition. Battery-SOC conditions (Solar Assistant's Example 2) are not
supported -- add them here if a real need for one shows up.

Each automation is an ORDERED list of rules; the first rule whose
conditions match "now" wins -- read top-to-bottom, like Solar Assistant's
own tables. Put more specific rules (holiday) above more general ones
(any), so a holiday that happens to fall on a weekday still gets the
holiday behavior rather than the weekday one. Solar Assistant's own docs
recommend covering the full day with no gaps across a rule set, so there
is never a moment where a setting is left on "whatever was last applied"
by accident -- the same recommendation applies here.

Runs entirely server-side, in-process (see Runner._automation_loop in
poller.py) -- it calls InverterDevice.write_holding() directly, the exact
same validated write path (writable: true, min/max from the register map,
read-back verification) manual commands from solar_settings.html use. It
does NOT go through the arm-lock: arm-lock exists to stop an accidental
click from a second browser tab, and a scheduled automation is a reviewed,
pre-configured rule, not an accidental one. The register-map validation is
the safety net that actually matters here, and it applies no matter who
-- or what -- is asking for the write.
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime


def _parse_hhmm(s: str) -> dtime:
    hh, mm = s.split(":")
    return dtime(int(hh), int(mm))


def _day_matches(kind: str, today: date, holidays: set[date]) -> bool:
    if kind == "any":
        return True
    if kind == "holiday":
        return today in holidays
    is_weekend = today.weekday() >= 5   # Monday=0 .. Sunday=6
    if kind == "weekend":
        return is_weekend
    if kind == "weekday":
        return not is_weekend
    return False


def evaluate(rules: list[dict], now: datetime, holidays: set[date]):
    """Returns the value of the first matching rule, or None if none match
    (nothing to write -- the setting is left alone). `now` must already be
    in the SITE's own timezone, not the container's -- these rules are
    written against a human's idea of "the weekend", which only makes
    sense in local time. See app/poller.py's clock-drift comment for the
    same bug class this would otherwise repeat.

    Overnight ranges (end < start) are deliberately not supported -- Solar
    Assistant's own docs warn this "creates cyclic logic that is difficult
    to understand and maintain" and recommends splitting into same-day
    rules instead (e.g. 22:00-23:59 and 00:00-05:00 as two rules). This
    matches that guidance rather than trying to be cleverer than it.
    """
    today = now.date()
    t = now.time()
    for rule in rules:
        start = _parse_hhmm(rule.get("time_start") or "00:00")
        end = _parse_hhmm(rule.get("time_end") or "23:59")
        if not (start <= t <= end):
            continue
        if not _day_matches(rule.get("days", "any"), today, holidays):
            continue
        return rule.get("value")
    return None


def parse_holidays(dates: list[str]) -> set[date]:
    """Site-level holiday list -> a set of date objects, for fast lookup.
    Bad entries are dropped rather than raising -- a typo in one date
    should not take down every automation depending on this set."""
    out = set()
    for s in dates or []:
        try:
            out.add(date.fromisoformat(s))
        except (ValueError, TypeError):
            continue
    return out
