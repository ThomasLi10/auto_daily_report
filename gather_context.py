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

We keep only INTERACTIVE Claude-Max sessions and drop AUTOMATED pipeline ones, so a
high-volume automation can't flood the report. Automated jobs are recognized by their
cwd: they run in ephemeral per-job scratch checkouts (e.g. tq_ai library/alphas mining
under /tmp/*_ro_*). Match them with DAILY_REPORT_EXCLUDE_CWD_GLOBS / --exclude-cwd; this
applies to EVERY source (the same automation runs under the primary user too). Those
jobs' git commits still count. Empty globs = no session filtering.

Usage:
    python3 gather_context.py 2026-06-01                 # single day
    python3 gather_context.py 2026-06-01 2026-06-03      # inclusive date range
    python3 gather_context.py 2026-06-01 --repos ~/code/myrepo ...
    python3 gather_context.py 2026-06-01 --projects-dir ~/.claude/projects
    python3 gather_context.py 2026-06-01 --extra-users svc1 svc2     # fold in extras
    python3 gather_context.py 2026-06-01 --extra-users               # no extras (current user only)
    python3 gather_context.py 2026-06-01 --exclude-cwd '/tmp/*_ro_*' # drop automated pipeline sessions
    python3 gather_context.py 2026-06-01 --exclude-cwd               # keep all sessions (no filtering)
"""

import argparse
import fnmatch
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
# Session cwd globs to DROP as automated pipeline jobs (see module docstring for why). None
# by default — set DAILY_REPORT_EXCLUDE_CWD_GLOBS="/tmp/tqlib_ro_* /tmp/aha_ro_*" (space-
# separated) to keep site-specific paths out of the source, or override per-run with
# --exclude-cwd. Match ONLY the ephemeral per-job scratch dirs, NOT the code dirs where you
# DEVELOP the pipeline (e.g. .../tq_ai/agent/alphas) — that's real interactive work to keep.
DEFAULT_EXCLUDE_CWD_GLOBS = os.environ.get("DAILY_REPORT_EXCLUDE_CWD_GLOBS", "").split()
# ssh host aliases whose ~/.claude/projects sessions fold in too (e.g. a Windows box running
# msys2 bash). Sessions only — no git. Needs non-interactive key auth (BatchMode). None by
# default — set DAILY_REPORT_SSH_HOSTS="host1 host2" or override per-run with --ssh-hosts.
DEFAULT_SSH_HOSTS = os.environ.get("DAILY_REPORT_SSH_HOSTS", "").split()
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
    ai_title = custom_title = None
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
        if typ == "custom-title":  # Claude Desktop sessions name themselves this way
            t = o.get("customTitle")
            if t and t != "New session":  # Desktop's placeholder until renamed
                custom_title = t
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
        "ai_title": custom_title or ai_title,
        "cwd": cwd,
        "branch": branch,
        "prompts": prompts,
        "files": files,
        "source": None,  # filled in by caller
    }


def local_session_files(projects_dir, as_user=None):
    """Yield (path, lines) for every top-level session under a local projects dir."""
    for path in list_session_paths(projects_dir, as_user):
        yield path, read_session_lines(path, as_user)


REMOTE_FILE_MARK = "@@DAILY_REPORT_FILE "
REMOTE_OK_MARK = "@@DAILY_REPORT_OK"
REMOTE_MTIME_SLACK_SECS = 60  # clock skew between hosts + coarse mtime granularity


def remote_session_files(host, since):
    """Fetch session files from an ssh host in ONE connection. Returns list of (path, lines),
    or None if the host is unreachable. Only files modified at/after `since` are sent — a
    session with activity in the window must have been written then, and this keeps
    multi-MB old transcripts off the wire. The remote side needs only a POSIX shell + GNU
    find/cat (msys2 bash on Windows qualifies); its non-interactive PATH may lack /usr/bin,
    hence the export. Session jsonl lines start with '{', so the marker can't collide."""
    script = (
        "export PATH=/usr/bin:/bin:$PATH; "
        f"echo {REMOTE_OK_MARK}; "
        'find "$HOME/.claude/projects" -mindepth 2 -maxdepth 2 -name "*.jsonl" '
        f"-newermt @{int(since.timestamp()) - REMOTE_MTIME_SLACK_SECS} 2>/dev/null | "
        f'while IFS= read -r f; do printf "\\n{REMOTE_FILE_MARK}%s\\n" "$f"; cat "$f"; done'
    )
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = r.stdout.splitlines()
    if REMOTE_OK_MARK not in lines[:5]:
        return None
    files, path, buf = [], None, []
    for ln in lines:
        if ln.startswith(REMOTE_FILE_MARK):
            if path:
                files.append((path, buf))
            path, buf = ln[len(REMOTE_FILE_MARK):], []
        elif path:
            buf.append(ln)
    if path:
        files.append((path, buf))
    return files


def scan_sessions(session_files, start, end, label=None, exclude_cwd_globs=()):
    """Scan (path, lines) pairs. Sessions whose cwd matches an exclude glob are skipped —
    these are automated pipeline jobs that run in ephemeral per-job scratch checkouts (e.g.
    the tq_ai library / alphas mining under /tmp/*_ro_*), NOT interactive Claude-Max work.
    Their git commits still count (git is harvested separately). Returns (sessions,
    n_dropped)."""
    out, n_dropped = [], 0
    for path, lines in session_files:
        info = scan_session(path, lines, start, end)
        if info and (info["prompts"] or info["files"] or info["ai_title"]):
            cwd = info.get("cwd") or ""
            if exclude_cwd_globs and any(fnmatch.fnmatch(cwd, g) for g in exclude_cwd_globs):
                n_dropped += 1
                continue
            info["source"] = label
            out.append(info)
    return out, n_dropped


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
def build_sources(args):
    """Ordered list of {label, as_user, ssh_host, repos, projects_dir} to harvest. Primary
    first. `label` tags output lines (None = primary, no tag)."""
    primary = {
        "label": None,
        "as_user": None,
        "ssh_host": None,
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
            "label": user,
            "as_user": user,
            "ssh_host": None,
            "repos": discover_repos([f"{home}/code/*"], as_user=user) if ok else [],
            "projects_dir": f"{home}/.claude/projects",
            "available": ok,
        })

    # ssh hosts contribute Claude sessions only (no git); reachability is known after fetch.
    hosts = DEFAULT_SSH_HOSTS if args.ssh_hosts is None else args.ssh_hosts
    for host in hosts:
        sources.append({
            "label": host,
            "as_user": None,
            "ssh_host": host,
            "repos": [],
            "projects_dir": None,
            "available": True,
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
    ap.add_argument("--exclude-cwd", nargs="*", default=None,
                    help="glob(s) for session cwds to DROP as automated pipeline jobs "
                         f"(default: {' '.join(DEFAULT_EXCLUDE_CWD_GLOBS) or 'none'}; "
                         "pass with no globs to disable)")
    ap.add_argument("--ssh-hosts", nargs="*", default=None,
                    help="ssh host aliases whose Claude sessions fold in "
                         f"(default: {' '.join(DEFAULT_SSH_HOSTS) or 'none'}; pass with no "
                         "hosts to disable)")
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
            tag = f"  [{src['label']}]" if src["label"] else ""
            print(f"\n## {shorten_home(repo)}{tag}")
            for t, h, subj in commits:
                print(f"  {t}  {h}  {subj}")
    if not any_commit:
        print("  (none)")

    # --- sessions ---
    # Drop automated pipeline sessions (those whose cwd is an ephemeral per-job scratch
    # checkout, e.g. tq_ai library/alphas under /tmp/*_ro_*) so they don't flood the
    # report — keep only interactive Claude-Max work. Applies to EVERY source (the same
    # automation runs under the primary user too). Their git commits still count. The
    # globs come from DAILY_REPORT_EXCLUDE_CWD_GLOBS / --exclude-cwd; empty = no filtering.
    exclude_globs = DEFAULT_EXCLUDE_CWD_GLOBS if args.exclude_cwd is None else args.exclude_cwd
    sessions = []
    auto_dropped = []
    unreachable = []
    for src in sources:
        if src["ssh_host"]:
            files = remote_session_files(src["ssh_host"], start)
            if files is None:
                unreachable.append(src["ssh_host"])
                continue
        else:
            files = local_session_files(src["projects_dir"], src["as_user"])
        found, ndrop = scan_sessions(files, start, end, src["label"], exclude_globs)
        sessions.extend(found)
        if ndrop:
            auto_dropped.append((src["label"] or "self", ndrop))
    sessions.sort(key=lambda s: (s["ai_title"] or "z").lower())
    print("\n" + "=" * 70)
    print(f"CLAUDE CODE SESSIONS ({len(sessions)} with activity)")
    print("=" * 70)
    for host in unreachable:
        print(f"# NOTE: ssh host '{host}' unreachable (ssh -o BatchMode=yes failed) — "
              "its sessions skipped.")
    for user, n in auto_dropped:
        print(f"# NOTE: skipped {n} automated-pipeline session(s) for '{user}' "
              f"(cwd in excluded scratch dirs); their git commits still count.")
    if not sessions:
        print("  (none)")
    for s in sessions:
        tag = f"  [{s['source']}]" if s["source"] else ""
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
