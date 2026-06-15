#!/usr/bin/env python3
"""Fetch a day's Feishu (Lark) calendar events for the daily report, via CalDAV.

Uses the same CalDAV account you'd configure in Apple Calendar (server + username
+ a Feishu-generated CalDAV password). No open-platform app, admin rights, or OAuth.

Feishu's CalDAV server is quirky: a `calendar-query` REPORT returns matching event
hrefs but NOT their data (calendar-data -> 404), and a direct GET on an .ics -> 403.
The data only comes back via a `calendar-multiget` REPORT. So we do:
    1. PROPFIND  -> discover calendar collections
    2. calendar-query (time-range)  -> event hrefs for the day
    3. calendar-multiget (those hrefs)  -> the actual ICS
    4. expand RRULE locally and filter to the local day

One-time setup (creds read from stdin, never argv):
    python3 gather_calendar.py --set-caldav     # paste 3 lines: server / username / password

Daily use (called by the daily-report skill):
    python3 gather_calendar.py <YYYY-MM-DD>

Diagnostics:
    python3 gather_calendar.py --probe

Deps: requests, icalendar, recurring_ical_events (pip install).
"""

import argparse
import datetime as dt
import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import timedelta, timezone
from urllib.parse import urlsplit

CFG_STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".feishu_caldav.json")
NS = {"D": "DAV:", "C": "urn:ietf:params:xml:ns:caldav"}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_cfg():
    if os.environ.get("FEISHU_CALDAV_URL"):
        return {
            "url": os.environ["FEISHU_CALDAV_URL"],
            "username": os.environ.get("FEISHU_CALDAV_USER", ""),
            "password": os.environ.get("FEISHU_CALDAV_PASSWORD", ""),
        }
    if os.path.exists(CFG_STORE):
        return json.load(open(CFG_STORE))
    return None


def set_cfg_from_stdin():
    print("Paste 3 lines (server URL/host, username, password):", file=sys.stderr)
    lines = [l.strip() for l in sys.stdin.read().splitlines() if l.strip()]
    if len(lines) < 3:
        raise SystemExit("expected 3 non-empty lines: url / username / password")
    url = lines[0]
    if not url.startswith("http"):
        url = "https://" + url
    cfg = {"url": url, "username": lines[1], "password": lines[2]}
    with open(CFG_STORE, "w") as f:
        json.dump(cfg, f)
    os.chmod(CFG_STORE, 0o600)
    print("OK: stored at", CFG_STORE)


# ---------------------------------------------------------------------------
# CalDAV (raw requests)
# ---------------------------------------------------------------------------
def _client(cfg):
    import requests
    from requests.auth import HTTPBasicAuth
    parts = urlsplit(cfg["url"])
    base = f"{parts.scheme}://{parts.netloc}"
    sess = requests.Session()
    sess.auth = HTTPBasicAuth(cfg["username"], cfg["password"])
    return sess, base, cfg["username"]


def _dav(sess, method, url, body, depth="1"):
    r = sess.request(method, url, data=body.encode("utf-8"),
                     headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
                     timeout=30)
    r.raise_for_status()
    return ET.fromstring(r.content)


def discover_calendars(sess, base, username):
    body = ('<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            '<d:prop><d:resourcetype/><d:displayname/></d:prop></d:propfind>')
    root = _dav(sess, "PROPFIND", f"{base}/{username}/", body, depth="1")
    cals = []
    for resp in root.findall("D:response", NS):
        href = resp.findtext("D:href", default="", namespaces=NS)
        rtype = resp.find(".//D:resourcetype", NS)
        is_cal = rtype is not None and rtype.find("C:calendar", NS) is not None
        if is_cal and href:
            name = resp.findtext(".//D:displayname", default="", namespaces=NS)
            cals.append((href, name))
    return cals


def query_event_hrefs(sess, base, cal_href, start_utc, end_utc):
    body = (
        '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        '<d:prop><d:getetag/></d:prop>'
        '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
        f'<c:time-range start="{start_utc}" end="{end_utc}"/>'
        '</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>'
    )
    root = _dav(sess, "REPORT", base + cal_href, body, depth="1")
    return [h for h in (resp.findtext("D:href", default="", namespaces=NS)
                        for resp in root.findall("D:response", NS)) if h.endswith(".ics")]


def multiget_ics(sess, base, cal_href, hrefs):
    out = []
    for i in range(0, len(hrefs), 50):  # chunk
        chunk = hrefs[i:i + 50]
        hx = "".join(f"<d:href>{h}</d:href>" for h in chunk)
        body = ('<c:calendar-multiget xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                f'<d:prop><d:getetag/><c:calendar-data/></d:prop>{hx}</c:calendar-multiget>')
        root = _dav(sess, "REPORT", base + cal_href, body, depth="1")
        for resp in root.findall("D:response", NS):
            data = resp.find(".//C:calendar-data", NS)
            if data is not None and data.text:
                out.append(data.text)
    return out


# ---------------------------------------------------------------------------
# Parse / format
# ---------------------------------------------------------------------------
def local_range(start_str, end_str):
    """(start, end) tz-aware datetimes for [start 00:00, end+1day 00:00) in local time."""
    start = dt.datetime.fromisoformat(start_str + "T00:00:00").astimezone()
    end = dt.datetime.fromisoformat(end_str + "T00:00:00").astimezone() + timedelta(days=1)
    return start, end


def to_utc_str(d):
    return d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


SYSTEM_ORGANIZERS = {"面试日历", "interview"}  # synthetic organizers, not real people


def expand_events(ics_blobs, day_start, day_end, self_names=frozenset(), multiday=False):
    import icalendar
    import recurring_ical_events
    rows, seen = [], set()
    for blob in ics_blobs:
        try:
            cal = icalendar.Calendar.from_ical(blob)
        except Exception:
            continue
        try:
            occ = recurring_ical_events.of(cal).between(day_start, day_end)
        except Exception:
            occ = [c for c in cal.walk("VEVENT")]
        for comp in occ:
            if str(comp.get("STATUS", "")).upper() == "CANCELLED":
                continue
            try:
                sk, dd, text = _entry(comp, day_start, day_end, self_names, multiday)
            except Exception:
                continue
            if dd in seen:
                continue
            seen.add(dd)
            rows.append((sk, text))
    rows.sort(key=lambda x: x[0])
    return rows


def _cn(addr):
    try:
        return str(addr.params.get("CN", "")).strip()
    except Exception:
        return ""


def _romanize(name):
    """Chinese name -> 'Given Surname' pinyin (e.g. 邹煜曈 -> Yutong Zou).
    Non-Chinese names are returned unchanged. Falls back to the original on any error."""
    if not any("一" <= c <= "鿿" for c in name):
        return name  # already latin / has an English name
    try:
        from pypinyin import lazy_pinyin
        parts = lazy_pinyin(name)
        if not parts:
            return name
        if len(parts) == 1:
            return parts[0].capitalize()
        surname = parts[0].capitalize()              # assume 1-char surname (covers most)
        given = "".join(parts[1:]).capitalize()
        return f"{given} {surname}"
    except Exception:
        return name


def _people(comp, self_names):
    """Attendee/organizer display names (romanized), excluding self and synthetic organizers."""
    names = []
    att = comp.get("ATTENDEE")
    items = att if isinstance(att, list) else ([att] if att is not None else [])
    for a in items:
        cn = _cn(a)
        if cn and cn not in self_names and cn not in SYSTEM_ORGANIZERS:
            names.append(cn)
    org = _cn(comp.get("ORGANIZER")) if comp.get("ORGANIZER") is not None else ""
    if org and org not in self_names and org not in SYSTEM_ORGANIZERS and org not in names:
        names.append(org)
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(_romanize(n))
    return out


def _entry(comp, day_start, day_end, self_names, multiday=False):
    summary = str(comp.get("SUMMARY", "(无标题)"))
    start = comp["DTSTART"].dt
    end = comp["DTEND"].dt if "DTEND" in comp else start
    if isinstance(start, dt.datetime):
        ls = start.astimezone()
        le = end.astimezone() if isinstance(end, dt.datetime) else ls
        s = max(ls, day_start)
        e = min(le, day_end)
        pfx = f"{ls.strftime('%m-%d')} " if multiday else ""
        when = f"{pfx}{s.strftime('%H:%M')}-{e.strftime('%H:%M')}"
        sk, dd = ls.timestamp(), (summary, ls.timestamp())
    else:
        # all-day event: DTSTART is a date. In range mode, key by day so identical
        # all-day events on different days neither collide (dedup) nor mis-sort.
        pfx = f"{start.strftime('%m-%d')} " if multiday else ""
        when = f"{pfx}全天"
        if multiday:
            day0 = dt.datetime(start.year, start.month, start.day).astimezone()
            sk, dd = day0.timestamp() - 1, (summary, "allday", start.isoformat())
        else:
            sk, dd = day_start.timestamp() - 1, (summary, "allday")
    loc = str(comp.get("LOCATION", "")).strip()
    people = _people(comp, self_names)
    extra = ""
    if loc:
        extra += f"  @{loc}"
    if people:
        extra += f"  ｜with: {', '.join(people)}"
    return sk, dd, f"{when}  {summary}{extra}"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def fetch(start_str, end_str):
    cfg = load_cfg()
    if not cfg:
        print("NO_CONFIG: 还没配置飞书 CalDAV 账号。先跑 --set-caldav。", file=sys.stderr)
        return 2
    try:
        import requests  # noqa: F401
        import icalendar  # noqa: F401
        import recurring_ical_events  # noqa: F401
    except ImportError:
        print("MISSING_DEPS: pip install requests icalendar recurring_ical_events", file=sys.stderr)
        return 3
    multiday = start_str != end_str
    label = f"{start_str}..{end_str}" if multiday else start_str
    day_start, day_end = local_range(start_str, end_str)
    s_utc, e_utc = to_utc_str(day_start), to_utc_str(day_end)
    try:
        sess, base, user = _client(cfg)
        cals = discover_calendars(sess, base, user)
        self_names = frozenset(name for _h, name in cals if name)
        blobs = []
        for cal_href, _name in cals:
            hrefs = query_event_hrefs(sess, base, cal_href, s_utc, e_utc)
            if hrefs:
                blobs.extend(multiget_ics(sess, base, cal_href, hrefs))
    except Exception as e:
        print(f"CALDAV_FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 4

    rows = expand_events(blobs, day_start, day_end, self_names, multiday)
    if not rows:
        print(f"# Feishu calendar {label}: (无日程)")
        return 0
    print(f"# Feishu calendar {label} ({len(rows)} 条)")
    for _, text in rows:
        print("  - " + text)
    return 0


def probe():
    cfg = load_cfg()
    if not cfg:
        print("NO_CONFIG: 先跑 --set-caldav", file=sys.stderr)
        return 2
    sess, base, user = _client(cfg)
    cals = discover_calendars(sess, base, user)
    print(f"connected ok; {len(cals)} calendar(s):")
    for href, name in cals:
        print(f"  - {name or href}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", help="local date YYYY-MM-DD (range start)")
    ap.add_argument("end", nargs="?", help="optional inclusive range end YYYY-MM-DD")
    ap.add_argument("--set-caldav", action="store_true", help="store CalDAV creds (3 lines from stdin)")
    ap.add_argument("--probe", action="store_true", help="connect and list calendars")
    args = ap.parse_args()

    if args.set_caldav:
        set_cfg_from_stdin()
        return
    if args.probe:
        sys.exit(probe())
    if not args.date:
        ap.error("need a date, or --set-caldav / --probe")
    sys.exit(fetch(args.date, args.end or args.date))


if __name__ == "__main__":
    main()
