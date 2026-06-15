#!/usr/bin/env python3
"""Gather raw material for a daily report on a given local date.

Collects two signals and prints them as a readable context dump:
  1. Git commits authored by the user across local git repos that day.
  2. Claude Code session activity that day (AI titles, user prompts, files touched).

The caller (Claude, via the daily-report skill) synthesizes this into bullets.

Usage:
    python3 gather_context.py 2026-06-01                 # single day
    python3 gather_context.py 2026-06-01 2026-06-03      # inclusive date range
    python3 gather_context.py 2026-06-01 --repos /home/thomas/code/tq /home/thomas/code/alphahub
    python3 gather_context.py 2026-06-01 --projects-dir /home/thomas/.claude/projects
"""

import argparse
import glob
import json
import os
import subprocess
from datetime import datetime, timedelta

DEFAULT_CODE_GLOBS = ["/home/thomas/code/*"]
DEFAULT_PROJECTS_DIR = "/home/thomas/.claude/projects"
MAX_PROMPT_CHARS = 400


def local_range(start_str, end_str):
    """(start, end) tz-aware datetimes for [start 00:00, end+1day 00:00) in local time."""
    start = datetime.fromisoformat(start_str + "T00:00:00").astimezone()
    end = datetime.fromisoformat(end_str + "T00:00:00").astimezone() + timedelta(days=1)
    return start, end


def parse_ts(ts):
    """Parse a session ISO timestamp (UTC, 'Z' suffix) into a tz-aware datetime."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------
def discover_repos(repo_globs):
    repos = []
    for pattern in repo_globs:
        for path in glob.glob(pattern):
            if os.path.isdir(os.path.join(path, ".git")):
                repos.append(os.path.normpath(path))
    return sorted(set(repos))


def git_commits(repo, start_str, end_str, multiday):
    """Commits authored by this repo's user.name within [start, end] (local), all branches."""
    name = subprocess.run(
        ["git", "-C", repo, "config", "user.name"],
        capture_output=True, text=True,
    ).stdout.strip()
    args = [
        "git", "-C", repo, "log", "--all", "--no-merges",
        f"--since={start_str} 00:00:00", f"--until={end_str} 23:59:59",
        "--pretty=format:%ct\t%ad\t%h\t%s",  # %ct = commit epoch, for a correct chronological sort
        "--date=format:%m-%d %H:%M" if multiday else "--date=format:%H:%M",
    ]
    if name:
        args.insert(4, f"--author={name}")
    out = subprocess.run(args, capture_output=True, text=True).stdout.strip()
    if not out:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split("\t", 3)
        if len(parts) == 4:
            rows.append(parts)  # [epoch, time, hash, subject]
    # de-dup identical commits surfaced on multiple branches by --all
    seen, uniq = set(), []
    for r in rows:
        if r[2] in seen:  # by hash
            continue
        seen.add(r[2])
        uniq.append(r)
    # sort on real epoch (the %m-%d display string would mis-order across a year boundary)
    uniq.sort(key=lambda r: int(r[0]))
    return [(t, h, s) for _ct, t, h, s in uniq]  # (time, hash, subject)


# ---------------------------------------------------------------------------
# Claude Code sessions
# ---------------------------------------------------------------------------
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SKILL_BOILERPLATE = "Base directory for this skill:"


def extract_user_text(content):
    """Pull human-typed text out of a user message's content; '' if it is noise."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        chunks = [p["text"] for p in content
                  if isinstance(p, dict) and p.get("type") == "text" and p.get("text")]
        if not chunks:
            return ""  # tool_result-only or empty
        text = "\n".join(chunks)
    else:
        return ""
    text = text.strip()
    if not text:
        return ""
    # drop harness/system noise and slash-command envelopes
    if text.startswith("<") or text.startswith("[Request interrupted"):
        return ""
    if "This session is being continued from a previous conversation" in text:
        return "[resumed session after context compaction]"
    if text.startswith(SKILL_BOILERPLATE):
        first = text.splitlines()[0]
        name = first.split("/skills/")[-1].split("/")[0] if "/skills/" in first else "?"
        return f"[invoked /{name} skill]"
    return " ".join(text.split())  # collapse whitespace


def scan_session(path, start, end):
    """Return dict of in-window activity for one session file, or None if none."""
    ai_title = None
    prompts, files = [], []
    cwd = branch = None
    has_window_activity = False

    with open(path, errors="replace") as fh:
        for line in fh:
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            typ = o.get("type")

            if typ == "ai-title":
                ai_title = o.get("aiTitle") or ai_title
                continue

            ts = parse_ts(o.get("timestamp"))
            in_window = ts is not None and start <= ts < end
            if not in_window:
                continue
            has_window_activity = True
            cwd = o.get("cwd", cwd)
            branch = o.get("gitBranch", branch)
            msg = o.get("message", {})

            if typ == "user":
                txt = extract_user_text(msg.get("content"))
                if txt:
                    if len(txt) > MAX_PROMPT_CHARS:
                        txt = txt[:MAX_PROMPT_CHARS] + " …"
                    if not prompts or prompts[-1] != txt:  # drop consecutive dups
                        prompts.append(txt)

            elif typ == "assistant":
                for part in msg.get("content", []) if isinstance(msg.get("content"), list) else []:
                    if isinstance(part, dict) and part.get("type") == "tool_use" \
                            and part.get("name") in EDIT_TOOLS:
                        fp = (part.get("input") or {}).get("file_path")
                        if fp and fp not in files:
                            files.append(fp)

    if not has_window_activity:
        return None
    return {
        "session": os.path.splitext(os.path.basename(path))[0],
        "ai_title": ai_title,
        "cwd": cwd,
        "branch": branch,
        "prompts": prompts,
        "files": files,
    }


def scan_sessions(projects_dir, start, end):
    out = []
    for path in glob.glob(os.path.join(projects_dir, "*", "*.jsonl")):
        if os.sep + "subagents" + os.sep in path:
            continue
        info = scan_session(path, start, end)
        if info and (info["prompts"] or info["files"] or info["ai_title"]):
            out.append(info)
    return out


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------
def shorten_home(p):
    if not p:
        return p
    home = os.path.expanduser("~")
    if p == home:
        return "~"
    if p.startswith(home + os.sep):
        return "~" + p[len(home):]
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("start", help="local date YYYY-MM-DD (range start)")
    ap.add_argument("end", nargs="?", help="optional inclusive range end YYYY-MM-DD")
    ap.add_argument("--repos", nargs="*", default=None,
                    help="explicit repo paths (default: auto-discover under code dirs)")
    ap.add_argument("--projects-dir", default=DEFAULT_PROJECTS_DIR)
    args = ap.parse_args()

    end_str = args.end or args.start
    multiday = args.start != end_str
    try:
        start, end = local_range(args.start, end_str)
    except ValueError:
        raise SystemExit(f"bad date(s) {args.start!r}..{end_str!r}; expected YYYY-MM-DD")

    repos = args.repos if args.repos else discover_repos(DEFAULT_CODE_GLOBS)

    label = f"{args.start} .. {end_str}" if multiday else args.start
    print(f"# Daily-report material for {label} (local time)\n")

    # --- git ---
    print("=" * 70)
    print("GIT COMMITS (your commits, all branches)")
    print("=" * 70)
    any_commit = False
    for repo in repos:
        commits = git_commits(repo, args.start, end_str, multiday)
        if not commits:
            continue
        any_commit = True
        print(f"\n## {shorten_home(repo)}")
        for t, h, subj in commits:
            print(f"  {t}  {h}  {subj}")
    if not any_commit:
        print("  (none)")

    # --- sessions ---
    sessions = scan_sessions(args.projects_dir, start, end)
    sessions.sort(key=lambda s: (s["ai_title"] or "z").lower())
    print("\n" + "=" * 70)
    print(f"CLAUDE CODE SESSIONS ({len(sessions)} with activity)")
    print("=" * 70)
    if not sessions:
        print("  (none)")
    for s in sessions:
        print(f"\n## {s['ai_title'] or '(untitled session)'}")
        loc = shorten_home(s["cwd"] or "")
        if loc:
            print(f"   cwd: {loc}" + (f"  branch: {s['branch']}" if s["branch"] else ""))
        if s["prompts"]:
            print("   user prompts:")
            for p in s["prompts"]:
                print(f"     - {p}")
        if s["files"]:
            print("   files edited:")
            for f in s["files"][:25]:
                print(f"     · {shorten_home(f)}")
            if len(s["files"]) > 25:
                print(f"     · … (+{len(s['files']) - 25} more)")


if __name__ == "__main__":
    main()
