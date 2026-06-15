#!/usr/bin/env python3
"""Fetch China statutory holiday/workday data (incl. 调休 makeup days) into holidays/.

Data source: NateScarlet/holiday-cn — machine-readable JSON built from the State
Council's annual public-holiday announcements. Each day entry has an `isOffDay`:
    true  -> a day OFF that would otherwise be a workday (holiday)
    false -> a makeup WORKDAY on a weekend (调休 补班)

This is the only source that cleanly encodes 调休, so plain Mon–Fri logic is wrong on
its own. We cache the JSON locally so workday.py never touches the network on the
cron's critical path.

This script is the "best-effort refresh": the daily cron calls it after sending the
report, and it self-throttles — only re-downloading when the cache is older than
REFRESH_DAYS, unless --force. A failed download keeps the existing cache and (by
default in --quiet) never breaks the report run.

Usage:
    python3 fetch_holidays.py                 # this + next year, throttled
    python3 fetch_holidays.py --force         # ignore the freshness throttle
    python3 fetch_holidays.py 2025 2026 2027  # explicit years (never throttled)
    python3 fetch_holidays.py --quiet         # background use: minimal output, never exit non-zero
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date

HOLIDAYS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "holidays")
URL = "https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{year}.json"
REFRESH_DAYS = 30
TIMEOUT = 20


def cache_age_days():
    """Days since the newest cached JSON was written; None if there's no cache yet."""
    try:
        files = [os.path.join(HOLIDAYS_DIR, f) for f in os.listdir(HOLIDAYS_DIR)
                 if f.endswith(".json")]
    except FileNotFoundError:
        return None
    mtimes = [os.path.getmtime(f) for f in files]
    if not mtimes:
        return None
    return (time.time() - max(mtimes)) / 86400.0


def fetch_year(year):
    """Download one year's JSON; return the parsed dict or raise."""
    req = urllib.request.Request(URL.format(year=year),
                                 headers={"User-Agent": "daily-report/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        raw = r.read().decode("utf-8")
    data = json.loads(raw)  # raises if the body is an HTML 404 page, not JSON
    if not isinstance(data.get("days"), list):
        raise ValueError(f"{year}: unexpected schema (no 'days' list)")
    if not data["days"]:
        # holiday-cn ships an empty placeholder for years the State Council hasn't
        # announced yet. Treat that as "not published" — don't cache it, so workday.py
        # honestly falls back to weekend-only logic (+warning) until real data lands.
        raise ValueError(f"{year}: empty days (not announced yet)")
    return data


def save_year(year, data):
    os.makedirs(HOLIDAYS_DIR, exist_ok=True)
    path = os.path.join(HOLIDAYS_DIR, f"{year}.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)  # atomic; never leaves a half-written cache file
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("years", nargs="*", type=int, help="years to fetch (default: this + next year)")
    ap.add_argument("--force", action="store_true", help="ignore the freshness throttle")
    ap.add_argument("--quiet", action="store_true", help="background use: minimal output, never exit non-zero")
    args = ap.parse_args()

    def say(msg):
        if not args.quiet:
            print(msg, file=sys.stderr)

    years = args.years
    if not years:
        y = date.today().year
        years = [y, y + 1]  # next year's calendar is published late in the prior year

    # Throttle ONLY the default/background path; explicit years or --force always fetch.
    # But NEVER throttle a default year whose file is MISSING (e.g. next year's calendar at
    # the rollover): the throttle clock is global (newest mtime of any cached file), so a
    # freshly re-saved current-year file would otherwise keep a not-yet-cached year from ever
    # being pulled for up to REFRESH_DAYS.
    if not args.force and not args.years:
        missing = [y for y in years
                   if not os.path.exists(os.path.join(HOLIDAYS_DIR, f"{y}.json"))]
        if missing:
            # Fetch only the missing year(s) — cheap daily retry until the State Council
            # publishes them — without re-pulling the already-cached (throttled) years.
            say(f"missing cached year(s) {missing}; fetching only those (bypassing throttle)")
            years = missing
        else:
            age = cache_age_days()
            if age is not None and age < REFRESH_DAYS:
                say(f"cache fresh ({age:.1f}d < {REFRESH_DAYS}d), skip refresh")
                return 0

    ok, failed = [], []
    for year in years:
        try:
            data = fetch_year(year)
            save_year(year, data)
            n_off = sum(1 for d in data["days"] if d.get("isOffDay"))
            n_work = sum(1 for d in data["days"] if not d.get("isOffDay"))
            ok.append(year)
            say(f"{year}: saved ({n_off} off-days, {n_work} makeup-workdays)")
        except (urllib.error.HTTPError, urllib.error.URLError,
                ValueError, json.JSONDecodeError, OSError) as e:
            failed.append(year)
            say(f"{year}: fetch failed ({type(e).__name__}: {e}) — keeping existing cache")

    if failed and not ok and not args.quiet:
        return 1  # total failure surfaced only in interactive use
    return 0


if __name__ == "__main__":
    sys.exit(main())
