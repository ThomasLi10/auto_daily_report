#!/usr/bin/env python3
"""Gather raw material for a daily report on a given local date.

Collects two signals and prints them as a readable context dump:
  1. Git commits authored by the user across local git repos that day.
  2. Claude Code session activity that day (AI titles, user prompts, files touched).

The caller (Claude, via the daily-report skill) synthesizes this into bullets.

Sources: by default we harvest the current user (read directly). You can also fold
in "extra users" — service/bot accounts whose home is 0700 but which the current
user may read via passwordless `sudo -u <user>` (see DEFAULT_EXTRA_USERS). This is
how work done under a service account (e.g. report_hub) can roll into the report.
Extra-user access degrades gracefully: if sudo is unavailable the source is
skipped with a note, never a crash.

Usage:
    python3 gather_context.py 2026-06-01                 # single day
    python3 gather_context.py 2026-06-01 2026-06-03      # inclusive date range
    python3 gather_context.py 2026-06-01 --repos ~/code/myrepo ...
    python3 gather_context.py 2026-06-01 --projects-dir ~/.claude/projects
    python3 gather_context.py 2026-06-01 --extra-users svc1 svc2     # fold in extras
    python3 gather_context.py 2026-06-01 --extra-users               # no extras (current user only)
"""

import argparse
import glob
import json
import os
import subprocess
from datetime import datetime, timedelta

DEFAULT_CODE_GLOBS = [os.path.expanduser("~/code/*")]
DEFAULT_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
# Service/bot accounts to fold in BESIDES the current user. Their $HOME is 0700, so
# every read (git + session files) goes through `sudo -n -u <user>`; paths derive as
# /home/<user>/code/* and /home/<user>/.claude/projects. None by default — set
# DAILY_REPORT_EXTRA_USERS="acct1 acct2" (space-separated) to keep site-specific
# account names out of the source, or override per-run with --extra-users.
DEFAULT_EXTRA_USERS = os.environ.get("DAILY_REPORT_EXTRA_USERS", "").split()
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
# Access primitives (direct for the current user, sudo -u for extra users)
# ---------------------------------------------------------------------------
def _sudo_prefix(as_user):
    return ["sudo", "-n", "-u", as_user] if as_user else []


def sudo_available(as_user):
    """True if we can run commands as `as_user` non-interactively (None = self)."""
    if not as_user:
        return True
    try:
        r = subprocess.run(_sudo_prefix(as_user) + ["true"],
                           capture_output=True, text=True, timeout=15)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _run(argv, as_user=None):
    """Run argv (optionally via sudo -u); return stdout as text ('' on failure)."""
    try:
        r = subprocess.run(_sudo_prefix(as_user) + argv,
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        return r.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------
def discover_repos(repo_globs, as_user=None):
    """Dirs under the globs that are git repos. Globs expand as `as_user` (their FS)."""
    if as_user:
        # The current user can't even stat a 0700 home, so glob + isdir must run
        # as the target user.
        script = " ; ".join(
            f'for d in {g}; do [ -d "$d/.git" ] && echo "$d"; done'
            for g in repo_globs
        )
        out = _run(["bash", "-c", script], as_user=as_user)
        repos = [os.path.normpath(ln.strip()) for ln in out.splitlines() if ln.strip()]
        return sorted(set(repos))
    repos = []
    for pattern in repo_globs:
        for path in glob.glob(pattern):
            if os.path.isdir(os.path.join(path, ".git")):
                repos.append(os.path.normpath(path))
    return sorted(set(repos))


def git_commits(repo, start_str, end_str, multiday, as_user=None):
    """Commits authored by this repo's user.name within [start, end] (local), all branches."""
    name = _run(["git", "-C", repo, "config", "user.name"], as_user=as_user).strip()
    git_args = [
        "git", "-C", repo, "log", "--all", "--no-merges",
        f"--since={start_str} 00:00:00", f"--until={end_str} 23:59:59",
        "--pretty=format:%ct\t%ad\t%h\t%s",  # %ct = commit epoch, for a correct chronological sort
        "--date=format:%m-%d %H:%M" if multiday else "--date=format:%H:%M",
    ]
    if name:
        git_args.insert(4, f"--author={name}")  # right after "log"
    out = _run(git_args, as_user=as_user).strip()
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


def list_session_paths(projects_dir, as_user=None):
    """Top-level session jsonls under projects_dir (skips subagent transcripts)."""
    pattern = os.path.join(projects_dir, "*", "*.jsonl")
    if as_user:
        paths = [ln.strip() for ln in
                 _run(["bash", "-c", f'ls -1 {pattern} 2>/dev/null'], as_user=as_user).splitlines()
                 if ln.strip()]
    else:
        paths = glob.glob(pattern)
    return [p for p in paths if os.sep + "subagents" + os.sep not in p]


def read_session_lines(path, as_user=None):
    """Return the raw lines of a session file (via sudo cat for extra users)."""
    if as_user:
        return _run(["cat", path], as_user=as_user).splitlines()
    try:
        with open(path, errors="replace") as fh:
            return fh.read().splitlines()
    except OSError:
        return []


def scan_session(path, lines, start, end):
    """Return dict of in-window activity for one session's lines, or None if none."""
    ai_title = None
    prompts, files = [], []
    cwd = branch = None
    has_window_activity = False

    for line in lines:
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
        "as_user": None,  # filled in by caller
    }


def scan_sessions(projects_dir, start, end, as_user=None):
    out = []
    for path in list_session_paths(projects_dir, as_user):
        info = scan_session(path, read_session_lines(path, as_user), start, end)
        if info and (info["prompts"] or info["files"] or info["ai_title"]):
            info["as_user"] = as_user
            out.append(info)
    return out


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
def build_sources(args):
    """Ordered list of {as_user, repos, projects_dir} to harvest. Primary first."""
    primary = {
        "as_user": None,
        "repos": args.repos if args.repos is not None
        else discover_repos(DEFAULT_CODE_GLOBS),
        "projects_dir": args.projects_dir,
        "available": True,
    }
    sources = [primary]

    extra = DEFAULT_EXTRA_USERS if args.extra_users is None else args.extra_users
    for user in extra:
        home = f"/home/{user}"
        ok = sudo_available(user)
        sources.append({
            "as_user": user,
            "repos": discover_repos([f"{home}/code/*"], as_user=user) if ok else [],
            "projects_dir": f"{home}/.claude/projects",
            "available": ok,
        })
    return sources


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
                    help="explicit repo paths for the primary user "
                         "(default: auto-discover under code dirs)")
    ap.add_argument("--projects-dir", default=DEFAULT_PROJECTS_DIR)
    ap.add_argument("--extra-users", nargs="*", default=None,
                    help="service/bot accounts to fold in via sudo -u "
                         f"(default: {' '.join(DEFAULT_EXTRA_USERS) or 'none'}; pass "
                         "with no names to disable)")
    args = ap.parse_args()

    end_str = args.end or args.start
    multiday = args.start != end_str
    try:
        start, end = local_range(args.start, end_str)
    except ValueError:
        raise SystemExit(f"bad date(s) {args.start!r}..{end_str!r}; expected YYYY-MM-DD")

    sources = build_sources(args)

    label = f"{args.start} .. {end_str}" if multiday else args.start
    print(f"# Daily-report material for {label} (local time)\n")

    # note any extra source we wanted but can't reach
    for src in sources:
        if src["as_user"] and not src["available"]:
            print(f"# NOTE: source '{src['as_user']}' unavailable "
                  f"(sudo -n -u {src['as_user']} failed) — skipped.\n")

    # --- git (global hash de-dup: shared clones surface the same commit) ---
    print("=" * 70)
    print("GIT COMMITS (your commits, all branches)")
    print("=" * 70)
    any_commit = False
    seen_hashes = set()
    for src in sources:
        for repo in src["repos"]:
            commits = git_commits(repo, args.start, end_str, multiday, src["as_user"])
            commits = [c for c in commits if c[1] not in seen_hashes]  # c = (time, hash, subj)
            if not commits:
                continue
            any_commit = True
            seen_hashes.update(c[1] for c in commits)
            tag = f"  [{src['as_user']}]" if src["as_user"] else ""
            print(f"\n## {shorten_home(repo)}{tag}")
            for t, h, subj in commits:
                print(f"  {t}  {h}  {subj}")
    if not any_commit:
        print("  (none)")

    # --- sessions ---
    sessions = []
    for src in sources:
        sessions.extend(scan_sessions(src["projects_dir"], start, end, src["as_user"]))
    sessions.sort(key=lambda s: (s["ai_title"] or "z").lower())
    print("\n" + "=" * 70)
    print(f"CLAUDE CODE SESSIONS ({len(sessions)} with activity)")
    print("=" * 70)
    if not sessions:
        print("  (none)")
    for s in sessions:
        tag = f"  [{s['as_user']}]" if s["as_user"] else ""
        print(f"\n## {s['ai_title'] or '(untitled session)'}{tag}")
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
