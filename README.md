# daily_report

Auto-generates a concise daily **work report** from three local sources and delivers it
to Feishu:

1. **git commits** — your commits that day across all repos under `~/code/*`
2. **Claude Code sessions** — that day's session titles, prompts, and edited files
3. **Feishu calendar** — that day's meetings/interviews (via CalDAV)

Sources 1 & 2 are harvested for **the current user plus any "extra users"** — service/bot
accounts whose work should roll up into the same report. None are folded in by default; name
them via the `DAILY_REPORT_EXTRA_USERS` env var (space-separated) or `--extra-users a b`.
Their `$HOME` is `0700`, so `gather_context.py` reads them via passwordless `sudo -n -u <user>`
(git repos under `/home/<user>/code/*` and sessions under `/home/<user>/.claude/projects`).
Commits shared between clones are de-duplicated by hash; an unreachable extra user is
skipped with a note (never a crash). Disable extras with a bare `--extra-users`. This is how
work done under a service account (e.g. `report_hub`) rolls into the same report.

A small LLM pass (Claude headless) synthesizes the material into themed bullets.

## Layout

| Path | What |
|------|------|
| `~/my/daily_report/` | this repo — all code + config |
| `~/my/daily_report/holidays/` | cached China holiday/workday JSON (one file per year), auto-refreshed |
| `~/.claude/skills/daily-report` | symlink → this repo, so `/daily-report` works as a skill |
| `/tq/scratch/<user>/daily_report_log/` | generated reports + `run_daily.log` + `last_covered` cron cursor (NOT in repo) |

## Files

- `gather_context.py` — git + Claude-session material for a date **or date range**
- `gather_calendar.py` — Feishu calendar for a date/range (CalDAV). See `FEISHU_SETUP.md`
- `workday.py` — is a date a China workday? (调休-aware) + computes each cron run's report span
- `fetch_holidays.py` — refresh the cached China holiday calendar (holiday-cn JSON) into `holidays/`
- `send_feishu.py` — deliver a report to Feishu (group webhook, or 1:1 DM)
- `run_daily_report.sh` — gather → synthesize (`claude -p`) → archive → deliver (workday-gated)
- `SKILL.md` — the `/daily-report` skill definition (interactive use)
- `FEISHU_SETUP.md` — calendar (CalDAV) + delivery setup
- `.feishu_*.json` — **local secrets, gitignored** (see the `.example` files)
- `daily_report.local` — **local site config, gitignored** (extra service accounts, `OUT_DIR` override; see the `.example`)

## Usage

Interactive (in Claude Code): `/daily-report 2026-06-01`

Manual / scheduled:

```bash
~/my/daily_report/run_daily_report.sh                 # cron mode: workday-gated, span since last report
~/my/daily_report/run_daily_report.sh 2026-06-01       # one specific date (never touches cron state)
~/my/daily_report/run_daily_report.sh 2026-06-01 --no-send
~/my/daily_report/run_daily_report.sh --no-send        # cron-mode dry-run (generate only; no DM, no state)
```

## Scheduling & workday gating

No systemd/cron natively on this box, so `cron` was installed and a job added:

```
0 7 * * *  ~/my/daily_report/run_daily_report.sh >> /tq/scratch/<user>/daily_report_log/cron.log 2>&1
```

Runs every morning at 07:00. The cron run is **China-workday-aware** (incl. 调休 makeup
workdays — driven by `workday.py` + the cached calendar in `holidays/`):

- **On a workday** it reports every day since the last report (cursor file `last_covered`),
  so the **first workday after a weekend/holiday folds the whole break into ONE report**
  (e.g. the Thursday back from National Day covers `2026-09-30 → 2026-10-07`).
- **On a non-workday** it doesn't synthesize — it DMs a short *rest-day placeholder* that
  points at the next workday (e.g. `📅 Holiday: 国庆节 (2026-10-01) — … on the next workday (2026-10-08)`).
- `last_covered` advances **only after a successful send**, so a failed send or a missed
  run is retried next time — no dropped or double-sent days, and it self-heals after downtime.

The calendar self-updates: each run calls `fetch_holidays.py --quiet` (throttled to ~monthly),
which pulls next year's calendar once the State Council publishes it; otherwise everything is
offline. After a host reboot the cron daemon isn't auto-started (no systemd) — re-arm with
`sudo service cron start`.

**Cursor (`last_covered`).** Lives at `/tq/scratch/<user>/daily_report_log/last_covered` and
holds the last date already reported. The next cron run covers `(last_covered, yesterday]`.
Seed/reset it with `echo 2026-06-14 > .../last_covered`; **delete** it to re-bootstrap (next
run then covers just yesterday). A corrupt/blank cursor self-heals to "cover yesterday".
Archived span reports are named `YYYY-MM-DD_YYYY-MM-DD.md` (single days stay `YYYY-MM-DD.md`).

## Output format

English; a single markdown bullet list (no section headers, no commit count). 3–5 themed
work bullets (`- **Topic**: …`, max 8), then a final calendar bullet when there are events:
`- 📅 2 interviews; Meeting with Yutong Zou` (interviews collapsed to a count; meetings as
"Meeting with <people>" with names romanized to pinyin). See `SKILL.md` for the exact rules.

Delivered to Feishu as a blue-header interactive card (`Daily Report — <date>`) with the
markdown rendered — bold Topics, inline `code`, one bullet per line. To paste it into a Feishu
Doc with formatting intact, copy from the Feishu **desktop client** (the web client drops bold
on paste); for a 100%-faithful copy, import the archived `.md` into Feishu Drive (drag it into
云空间). See `FEISHU_SETUP.md` for delivery setup + paste tips.

## Setup on a fresh checkout

```bash
pip install requests icalendar recurring_ical_events
python3 fetch_holidays.py                               # seed holidays/ (this + next year)
cp .feishu_caldav.json.example .feishu_caldav.json     # then fill in (or use --set-caldav)
cp .feishu_webhook.json.example .feishu_webhook.json   # then fill in (or use --set-webhook)
cp daily_report.local.example daily_report.local       # optional: extra service accounts / OUT_DIR
ln -s ~/my/daily_report ~/.claude/skills/daily-report  # register the skill
```

> `holidays/*.json` is committed, so a fresh checkout already works offline; the
> `fetch_holidays.py` step just refreshes it. The calendar uses China's statutory
> holidays + 调休 makeup workdays via [holiday-cn](https://github.com/NateScarlet/holiday-cn).
