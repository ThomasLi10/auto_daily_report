#!/usr/bin/env bash
# Generate a daily WORK report and DM it to the user on Feishu.
# Designed to run unattended from cron — uses absolute paths and sets PATH itself.
#
# Cron mode (no date arg): runs ONLY on a China workday (调休-aware via workday.py), and
# covers every day since the last successful report (state cursor: $OUT_DIR/last_covered).
# So the first workday after a weekend/holiday folds the whole break into ONE report. On a
# non-workday it instead DMs a short "rest day" placeholder pointing at the next workday.
#   run_daily_report.sh              # cron: workday-gated span ending yesterday
#   run_daily_report.sh --no-send    # same, dry-run (generate only; never DM / advance state)
# Manual mode (explicit date): always that ONE day; never touches the cron state.
#   run_daily_report.sh 2026-06-01
#   run_daily_report.sh 2026-06-01 --no-send
set -euo pipefail

# Code lives in the repo; generated reports + log live in scratch (NOT in the repo).
SKILL_DIR="$HOME/my/daily_report"
PY="/3rd/anaconda3/bin/python3"
CLAUDE="$HOME/.nvm/versions/node/v22.22.0/bin/claude"
# cron has a bare PATH; make sure node (for claude) and basic tools are reachable
export PATH="$HOME/.nvm/versions/node/v22.22.0/bin:/3rd/anaconda3/bin:/usr/local/bin:/usr/bin:/bin"
# claude CLI reaches api.anthropic.com only via the corp proxy (without it: "403 Request
# not allowed"). cron's environment has no proxy vars, so set them here (honor existing).
export https_proxy="${https_proxy:-http://172.31.0.3:12318}"
export http_proxy="${http_proxy:-http://172.31.0.3:12318}"
export HTTPS_PROXY="${HTTPS_PROXY:-$https_proxy}"
export HTTP_PROXY="${HTTP_PROXY:-$http_proxy}"

# Authenticate ONLY via the Claude Code OAuth (Max) creds in ~/.claude/.credentials.json.
# Defensively drop any inherited relay / API-key vars (e.g. a third-party ANTHROPIC_BASE_URL +
# ANTHROPIC_AUTH_TOKEN like the cursor.scihub relay in ~/.bashrc): if such a stale token leaked
# into this run it would be sent to api.anthropic.com and rejected with
# "401 Invalid authentication credentials".
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL 2>/dev/null || true

# Optional site-local config (gitignored): extra service accounts to fold in, OUT_DIR
# override, etc. Keeps machine/account-specific values out of the repo. See the .example.
[ -f "$SKILL_DIR/daily_report.local" ] && . "$SKILL_DIR/daily_report.local"
export DAILY_REPORT_EXTRA_USERS="${DAILY_REPORT_EXTRA_USERS:-}"
# Reports + log + cron cursor live in scratch under the current user (NOT in the repo).
OUT_DIR="${OUT_DIR:-/tq/scratch/$(id -un)/daily_report_log}"
LOG="$OUT_DIR/run_daily.log"

mkdir -p "$OUT_DIR"
STATE="$OUT_DIR/last_covered"  # cron cursor: last calendar date already reported

# progress goes to stderr so stdout stays just the final report path (pipe-safe)
say() { echo "→ $*" >&2; }

# Parse args order-independently: a YYYY-MM-DD token = manual single-date mode; --no-send =
# dry-run (no DM, no state advance). Anything else is a usage error (never silently misparse,
# e.g. "--no-send 2026-06-01" must not drop the date and run cron mode).
SEND=1; MANUAL_DATE=""
for a in "$@"; do
  case "$a" in
    --no-send) SEND=0 ;;
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) MANUAL_DATE="$a" ;;
    *) echo "usage: run_daily_report.sh [<YYYY-MM-DD>] [--no-send]" >&2; exit 2 ;;
  esac
done

# Decide the day span this run covers, and whether to advance the state cursor.
#   Manual (explicit date): just that day; never touches state.
#   Cron (no date): only on a CHINA WORKDAY; covers (last_covered, yesterday]. A non-workday
#   DMs a rest-day placeholder; nothing-new skips. State is left untouched on a skip/off so
#   the first workday back folds the whole weekend/holiday gap into ONE report. 调休 honored.
ADVANCE_STATE=0
if [ -n "$MANUAL_DATE" ]; then
  START="$MANUAL_DATE"; END="$MANUAL_DATE"
else
  TODAY="$(date +%F)"
  # Read the cursor and trim whitespace. A present-but-blank cursor is corruption, not a first
  # run — warn loudly but continue (workday.py bootstraps to "cover yesterday").
  LAST="$(cat "$STATE" 2>/dev/null | tr -d '[:space:]' || true)"
  if [ -f "$STATE" ] && [ -z "$LAST" ]; then
    echo "[$(date '+%F %T')] WARN: state file $STATE is empty/whitespace; bootstrapping (covers only yesterday)" >>"$LOG"
    say "WARN: state file empty — bootstrapping"
  fi
  # Capture workday.py's exit code: a NON-ZERO rc is an ERROR (broken calendar / cursor), NOT a
  # skip — surface it loudly (and DM a FAILED alert) instead of silently stalling the cron.
  set +e
  PLAN="$("$PY" "$SKILL_DIR/workday.py" range "$TODAY" "$LAST" 2>>"$LOG")"; rc=$?
  set -e
  if [ "$rc" -ne 0 ]; then
    echo "[$(date '+%F %T')] ERROR: workday.py range failed (rc=$rc, last_covered=${LAST:-none})" >>"$LOG"
    say "ERROR: workday.py failed (rc=$rc) — see $LOG"
    if [ "$SEND" = "1" ]; then
      printf '%s' "- workday.py could not determine today's plan (rc=$rc); the workday calendar or state cursor may be broken (last_covered=${LAST:-none}).
- Check \`$LOG\` and the \`holidays/\` cache." \
        | "$PY" "$SKILL_DIR/send_feishu.py" --title "⚠️ Daily Report FAILED — $TODAY" >>"$LOG" 2>&1 || true
    fi
    exit 1
  fi
  # plan: "cover S E" | "none" | "off weekend NEXT" | "off holiday NEXT NAME"
  read -r KIND F1 F2 F3 <<<"$PLAN" || true
  case "$KIND" in
    cover)
      START="$F1"; END="$F2"; ADVANCE_STATE=1 ;;
    off)
      # Non-workday: DM a short placeholder (no synthesis), never advance state. F2 = next workday.
      if [ "$F1" = "holiday" ]; then
        BODY="# Daily Report — $TODAY (rest day)
- 📅 Holiday: ${F3:-假期} ($TODAY) — no work to report; will be summarized on the next workday ($F2)."
      else
        BODY="# Daily Report — $TODAY (rest day)
- 📅 Weekend ($TODAY) — no work to report; will be folded into the next workday's report ($F2)."
      fi
      echo "[$(date '+%F %T')] off-day [$F1] $TODAY → placeholder (next workday $F2, send=$SEND)" >>"$LOG"
      if [ "$SEND" = "1" ]; then
        if printf '%s\n' "$BODY" | "$PY" "$SKILL_DIR/send_feishu.py" --title "Daily Report — $TODAY (rest day)" >>"$LOG" 2>&1; then
          say "off-day placeholder sent ✓"
        else
          say "WARN: placeholder send failed (see $LOG)"
        fi
      else
        say "off-day [$F1]; placeholder NOT sent (--no-send)"
      fi
      "$PY" "$SKILL_DIR/fetch_holidays.py" --quiet >>"$LOG" 2>&1 || true
      exit 0 ;;
    *)
      # "none": workday, but nothing new since last_covered -> skip, DM nothing.
      # (A real error is rc!=0, already handled above; this branch is the benign case.)
      echo "[$(date '+%F %T')] skip: $TODAY nothing new since last report (last_covered=${LAST:-none}, plan='${PLAN}')" >>"$LOG"
      say "$TODAY 无新增 → 跳过"
      exit 0 ;;
  esac
fi

# Human label + archive filename: single day vs multi-day span.
if [ "$START" = "$END" ]; then
  LABEL="$START"; OUTFILE="$OUT_DIR/$START.md"
else
  LABEL="$START → $END"; OUTFILE="$OUT_DIR/${START}_${END}.md"
fi

echo "[$(date '+%F %T')] generating daily report for $LABEL (send=$SEND)" >>"$LOG"

# Is claude's output a real report (vs. an auth/system error it dumped to stdout)?
# The prompt mandates a "# Daily Report — <DATE>" first line; a themed "- **" bullet is a
# fallback. Claude's auth/system error dumps (401, "Not logged in", usage limit, overloaded)
# never produce that title line, so a valid title is a DECISIVE positive signal — accept on it
# BEFORE scanning for error phrases. Scanning first was a bug: a legit report whose own content
# mentions an error phrase (e.g. 2026-07-02's first bullet "…halts on backend/API errors", from
# the fix(tq_ai) commit) landed inside the head-2 window and got false-rejected 3× → FAILED alert.
# The error scan now only guards the title-LESS fallback path (where an error dump could masquerade).
is_valid_report() {
  local r="$1"
  [ -n "$r" ] || return 1
  # Real report: mandated title on line 1 → accept outright (don't let the error scan see the body).
  printf '%s\n' "$r" | head -1 | grep -q '^# Daily Report' && return 0
  # No title (fallback): reject known error dumps, then require a themed "- **" bullet.
  if printf '%s\n' "$r" | head -2 | grep -qiE 'invalid authentication|failed to authenticate|api error|usage limit|please run .*login|overloaded|credit balance'; then
    return 1
  fi
  printf '%s\n' "$r" | grep -q '^- \*\*' && return 0
  return 1
}

# 1. gather raw material (git + Claude sessions + Feishu calendar) over the span
say "$LABEL: gathering git + Claude sessions + Feishu calendar…"
MATERIAL="$("$PY" "$SKILL_DIR/gather_context.py" "$START" "$END"; echo; "$PY" "$SKILL_DIR/gather_calendar.py" "$START" "$END" 2>>"$LOG" || true)"

# 2. synthesize with Claude headless (self-contained prompt; no tools needed)
read -r -d '' RULES <<'EOF' || true
You are writing a concise daily WORK report from the gathered material below.
Output ONLY the final report in GitHub markdown — no preamble, no commentary.

Format (follow exactly):
- Language: ENGLISH (keep code symbols verbatim).
- DATE may be a single day OR a span like "2026-09-30 → 2026-10-07" (a workday report
  that folds in the preceding weekend/holiday). Use it verbatim in the title, and
  summarize the WHOLE span as ONE report (group by theme across the days, do not split
  the report per-day).
- The WHOLE report is ONE markdown bullet list. First line: "# Daily Report — <DATE>",
  then every line is a bullet starting with "- ". NO section headers (no "**Work**",
  no "**📅 Schedule**").
- Work bullets: group by THEME, not by commit/session. Use git commits as the backbone of
  "what shipped" and the session prompts for "why / what was debugged". Merge multiple
  commits+sessions on one theme into a single bullet. Each: "- **Topic**: …".
  Normally 3–5 bullets, at most 8.
- COVERAGE OVER DETAIL: when there's a lot of work, prioritize BREADTH — make sure every
  major area is represented; do NOT let one big theme crowd out the others, and never drop
  a real work theme just to hit 3–5 (expand toward 8 when there genuinely are more areas).
  For a content-heavy area, summarize COARSELY: capture the gist, MERGE similar/adjacent
  points into one phrase (just mention them), and don't enumerate every individual change.
- BE CONCISE: short phrases, not long multi-clause sentences. One main bullet ≈ 1–2 lines.
- DON'T cite specific file paths / scratch dirs / throwaway script names or numbers
  (e.g. "scripts/scratch/.../foo.py", "scripts 15–23"); only mention them if truly key.
  Meaningful module / node / field names (opt_replay, trading_status, r_last) are fine.
- When a bullet covers SEVERAL things (an elimination list, multiple change points), use
  indented sub-bullets ("  - …") under the main "- **Topic**:" line. Single-item bullets
  stay on one line (no sub-bullets).
- Drop non-work chatter (personal questions, concept explainers).
- If the material has a "Feishu calendar" section WITH events, add ONE final bullet that
  summarizes the day's calendar, prefixed with "📅 ":
    * Interviews (title contains 面试/interview): COLLAPSE into a count, e.g. "2 interviews"
      ("1 interview" if one). No candidate names, no times.
    * Meetings: "Meeting with <people>" — ENGLISH names (use the English name if it's in
      the title, e.g. "Yutong Weekly"→Yutong; otherwise romanize to pinyin, e.g.
      邹煜曈→Yutong Zou; if no attendee, use the meeting name). The gather output lists
      attendees after "｜with:" (your own name already excluded).
    * Join items with "; ", e.g. "- 📅 2 interviews; Meeting with Yutong Zou".
  If there is no calendar section or it says "(无日程)", OMIT this bullet entirely.
- Do NOT add a "(N commits)" line or any trailing summary.
- No blank lines between bullets (tight list).

Match the BREVITY and shape of this example exactly (terse phrases, not sentences):
# Daily Report — 2026-05-29
- **prod vs iter1 PnL gap (~28pp)**: isolated the cause by elimination
  - ruled out: participation limit, slippage, cost structure, weight overlap, alpha capture, execution VWAP
  - prime suspect: γ scale (0.01 vs 100) → different TE utilization; TE audit prepared, not yet run
- **PnL leg decomposition**: split into night/day/trading — day dominant, night negative recently; checked `r_last` (open-to-close vs open-to-now)
- **Limit-up/down & participation**: optimizer blocks only fully-locked (status 1), lets pinned-open (status 3) through; added 10% participation cap (10–11am vol) to iter1 sim
- **opt_replay arena node**: replays iter1 weights through prod sim (bypasses opt_comb); configs for cn_equity/estu_x/debench
- **Handoff docs**: elimination chain + next steps
- 📅 All-hands with Zhihui Chu & Naive AI; 1-on-1 with Chang Zhang
EOF

PROMPT="$RULES

DATE = $LABEL

===== GATHERED MATERIAL =====
$MATERIAL"

say "synthesizing with claude (up to 3 tries, ~30s each, no output until done)…"
# Retry, and ONLY accept output that looks like a real report. The first headless call after
# an overnight-expired OAuth token will refresh it and then succeed; retries cover transient
# hiccups. Critically, a non-empty error line (e.g. the 401 auth error) is NOT a valid report,
# so it can never again be archived and DM'd to the user.
REPORT=""
LASTOUT=""
for attempt in 1 2 3; do
  set +e
  LASTOUT="$(printf '%s' "$PROMPT" | "$CLAUDE" -p --model sonnet 2>>"$LOG")"
  rc=$?
  set -e
  if [ "$rc" -eq 0 ] && is_valid_report "$LASTOUT"; then
    REPORT="$LASTOUT"
    break
  fi
  echo "[$(date '+%F %T')] claude attempt $attempt failed (rc=$rc): $(printf '%s' "$LASTOUT" | head -1)" >>"$LOG"
  say "claude attempt $attempt failed (rc=$rc) — retrying…"
  sleep 5
done

if [ -z "$REPORT" ]; then
  ERRLINE="$(printf '%s' "$LASTOUT" | head -1)"
  echo "[$(date '+%F %T')] ERROR: no valid report from claude after 3 tries (last: ${ERRLINE:-<empty>})" >>"$LOG"
  say "ERROR: claude produced no valid report (check proxy/auth) — see $LOG"
  # Notify the user that generation FAILED, instead of silently DMing the raw error as "today's report".
  if [ "$SEND" = "1" ]; then
    printf '%s' "- Claude synthesis returned no valid report after 3 attempts.
- Last output: \`${ERRLINE:-<empty>}\`
- Usually a Claude OAuth issue: run \`claude\` once interactively to refresh login, then check \`$LOG\`." \
      | "$PY" "$SKILL_DIR/send_feishu.py" --title "⚠️ Daily Report FAILED — $LABEL" >>"$LOG" 2>&1 || true
  fi
  exit 1
fi

# 3. archive
echo "$REPORT" >"$OUTFILE"
echo "[$(date '+%F %T')] saved $OUTFILE ($(wc -l <"$OUTFILE") lines)" >>"$LOG"
say "saved $OUTFILE"

# 4. deliver to Feishu. Advance the state cursor ONLY after a successful send (and only
#    in cron mode) — a send failure leaves state put so the next run retries the same span
#    rather than silently dropping it.
if [ "$SEND" = "1" ]; then
  if printf '%s' "$REPORT" | "$PY" "$SKILL_DIR/send_feishu.py" --title "Daily Report — $LABEL" >>"$LOG" 2>&1; then
    echo "[$(date '+%F %T')] sent to Feishu" >>"$LOG"; say "sent to Feishu ✓"
    if [ "$ADVANCE_STATE" = "1" ]; then
      # Atomic write (tmp + mv) so a torn write can't leave a corrupt cursor; guarded so a
      # write failure after a successful send doesn't abort under set -e (which would skip the
      # cursor advance and re-send this span next run).
      if printf '%s\n' "$END" >"$STATE.tmp" && mv -f "$STATE.tmp" "$STATE"; then
        echo "[$(date '+%F %T')] advanced last_covered -> $END" >>"$LOG"; say "state → $END"
      else
        echo "[$(date '+%F %T')] CRITICAL: report sent but state write failed; next run may re-send $LABEL" >>"$LOG"
        say "CRITICAL: state write failed after send (see $LOG)"
      fi
    fi
  else
    echo "[$(date '+%F %T')] WARN: Feishu send failed; state NOT advanced (will retry span)" >>"$LOG"
    say "WARN: Feishu send failed (see $LOG)"
  fi
else
  say "not sent (--no-send); state unchanged"
fi

# 5. best-effort: keep the China-holiday cache fresh (self-throttled; failure ignored,
#    so a network blip here never affects the report).
"$PY" "$SKILL_DIR/fetch_holidays.py" --quiet >>"$LOG" 2>&1 || true

echo "$OUTFILE"
