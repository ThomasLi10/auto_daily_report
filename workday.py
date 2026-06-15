#!/usr/bin/env python3
"""Is a given date a China workday? (accounts for 调休 makeup workdays + holidays)

Reads the cached holiday-cn JSON in holidays/ (kept fresh out-of-band by
fetch_holidays.py). For each date:
  - if it's in the cached data: workday  <=>  isOffDay is False
        (this covers BOTH 放假 holidays AND 调休 weekend-makeup workdays)
  - otherwise: workday  <=>  it's Mon–Fri
A date whose YEAR has no cached data falls back to weekday-only logic and warns on
stderr (that year's calendar hasn't been fetched yet).

Offline by design — this module never hits the network.

CLI (used by run_daily_report.sh):
    workday.py range <today> [<last_covered>]
        Prints ONE line on stdout describing what a run on <today> should do:
          "cover START END"        -> synthesize a report for the inclusive span
          "none"                   -> workday, but nothing new since last_covered (skip)
          "off weekend NEXT"       -> non-workday weekend; next workday is NEXT
          "off holiday NEXT NAME"  -> non-workday holiday NAME; next workday is NEXT
        last_covered defaults to <today>-2 (bootstrap: first run covers just yesterday).
    workday.py is-workday <date>
        Exit 0 if <date> is a workday, 1 if not. Explanation on stderr.
    workday.py check
        Print which years are loaded (diagnostics).
"""
import json
import os
import sys
from datetime import date, timedelta

HOLIDAYS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "holidays")


def _load():
    """Return (special: {iso: isOffDay}, names: {iso: holiday_name}, covered_years: set[int])."""
    special, hol_names, years = {}, {}, set()
    try:
        files = os.listdir(HOLIDAYS_DIR)
    except FileNotFoundError:
        return special, hol_names, years
    for fname in files:
        if not fname.endswith(".json"):
            continue
        try:
            year = int(os.path.splitext(fname)[0])
        except ValueError:
            continue
        try:
            with open(os.path.join(HOLIDAYS_DIR, fname)) as f:
                days = json.load(f)["days"]
        except (OSError, ValueError, KeyError):
            continue  # skip a corrupt/partial file rather than crash the cron
        for d in days:
            if "date" in d and "isOffDay" in d:
                special[d["date"]] = bool(d["isOffDay"])
                if d.get("name"):
                    hol_names[d["date"]] = d["name"]
        years.add(year)
    return special, hol_names, years


_SPECIAL, _NAMES, _YEARS = _load()


def is_workday(d):
    """True if d is a China workday (holiday-cn override if known, else Mon–Fri)."""
    off = _SPECIAL.get(d.isoformat())
    if off is not None:
        return not off
    return d.weekday() < 5


def is_covered(d):
    """True if d's year has cached holiday data (so is_workday is authoritative)."""
    return d.year in _YEARS


def report_range(today, last_covered):
    """Inclusive [start, end] a report run on `today` should cover, or None to skip."""
    if not is_workday(today):
        return None
    start = last_covered + timedelta(days=1)
    end = today - timedelta(days=1)  # yesterday
    if start > end:
        return None
    return start, end


def next_workday(d):
    """The first workday strictly after d (within a year), or None."""
    n = d + timedelta(days=1)
    for _ in range(366):
        if is_workday(n):
            return n
        n += timedelta(days=1)
    return None


def _parse(s):
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise SystemExit(f"bad date {s!r}; expected YYYY-MM-DD")


def _warn_if_uncovered(d):
    if not is_covered(d):
        print(f"WARN: no holiday data for {d.year}; using weekday-only logic "
              f"(run fetch_holidays.py)", file=sys.stderr)


def main(argv):
    if not argv:
        raise SystemExit("usage: workday.py {range|is-workday|check} ...")
    cmd = argv[0]

    if cmd == "check":
        print(f"holidays dir : {HOLIDAYS_DIR}")
        print(f"years loaded : {sorted(_YEARS) or '(none)'}")
        print(f"special days : {len(_SPECIAL)}")
        return 0

    if cmd == "is-workday":
        if len(argv) < 2:
            raise SystemExit("usage: workday.py is-workday <date>")
        d = _parse(argv[1])
        wd = is_workday(d)
        why = ("holiday-cn" if d.isoformat() in _SPECIAL
               else ("weekday" if is_covered(d) else f"weekday/NO-DATA-{d.year}"))
        print(f"{d} {'WORKDAY' if wd else 'off'} [{why}]", file=sys.stderr)
        _warn_if_uncovered(d)
        return 0 if wd else 1

    if cmd == "range":
        if len(argv) < 2:
            raise SystemExit("usage: workday.py range <today> [<last_covered>]")
        today = _parse(argv[1])
        # last_covered is LENIENT (today stays strict): empty -> bootstrap silently;
        # unparseable -> warn + bootstrap. So a corrupt cursor self-heals to "cover
        # yesterday" (and gets rewritten clean on the next successful send) instead of
        # raising SystemExit and wedging the cron forever.
        arg = argv[2] if len(argv) > 2 else ""
        if arg:
            try:
                last = date.fromisoformat(arg)
            except ValueError:
                print(f"WARN: bad last_covered {arg!r}; using bootstrap default (today-2)",
                      file=sys.stderr)
                last = today - timedelta(days=2)
        else:
            last = today - timedelta(days=2)
        _warn_if_uncovered(today)
        if is_workday(today):
            rng = report_range(today, last)
            if rng is None:
                print("none")  # workday, nothing new since last_covered
            else:
                print(f"cover {rng[0]} {rng[1]}")
        else:
            nxt = next_workday(today)
            nxt_s = nxt.isoformat() if nxt else "?"
            iso = today.isoformat()
            if _SPECIAL.get(iso):  # an off-day the State Council named -> a holiday
                print(f"off holiday {nxt_s} {_NAMES.get(iso, '假期')}")
            else:                  # a plain weekend
                print(f"off weekend {nxt_s}")
        return 0

    raise SystemExit(f"unknown command {cmd!r}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
