# visa-watch: context for Claude Code

Carried over from a claude.ai conversation (Nov 2026).

## Goal
Run a scheduled job (hourly) that checks the earliest open appointment date for a US
non-immigrant (B1/B2 tourist) visa at the U.S. Embassy in Astana, Kazakhstan, and
sends Telegram notifications:
- immediately (with sound) when a sooner date opens than at the previous check;
- once a day (silent) with a summary: earliest date, change since the last summary,
  best date seen in between, number of checks, and session health.

## What exists
- `visa_watch.py`: Python 3.9+, standard library only. It's read-only and never books anything.
  - Requests `GET https://ais.usvisa-info.com/en-kz/niv/schedule/<SCHEDULE_ID>/appointment/days/<FACILITY_ID>.json?appointments[expedite]=false`
    using the `_yatri_session` cookie copied from a browser after a manual sign-in.
    The sign-in form has an hCaptcha, so the script doesn't log in and stores no password.
  - Commands: `--set-cookie`, `--discover`, `--find-chat-id`, `--test-telegram`,
    `--status`, `--dry-run`, `--force`.
  - Saves `state.json` and `cookies.json` (chmod 600) next to the script.
  - Guard rails: at least 30 minutes between checks, a 3-hour pause after an empty
    answer or HTTP 403/429, alerts when the session expires and after 3 failures in a row.
- `visa_watch.env.example`: settings template. Copy it to `visa_watch.env` and `chmod 600`.
- Tested only offline: unit tests plus an end-to-end run against a fake AIS/Telegram
  server. It has never run against the real site.

## Constraints to keep in mind
- The AIS Kazakhstan site's terms prohibit bots and scripts. Keep it read-only and infrequent.
- Users report AIS throttling (empty lists) after about 48 requests, lasting about 5 hours.
- Unknown: how long a `_yatri_session` stays valid when hit hourly.
- Host: Raspberry Pi Zero 2 W `yarikos` on the home network (`yarikos.local`, 10.0.0.85),
  running the `pi-gen-yarik-os` image (Raspberry Pi OS Lite, Debian 13 trixie, arm64).
  Python 3.13, ca-certificates, tzdata, cron (active) and systemd-timesyncd are already there.
  Access: `ssh -i ~/.ssh/pi yarikp@yarikos.local` (key auth), or the UART console at
  `/dev/cu.usbserial-*`, 115200 baud. `sudo` needs a password. System TZ is Europe/London.

## Next steps
1. Create the Telegram bot, fill `visa_watch.env`, run `--find-chat-id`.
2. Sign in, find the schedule and facility IDs, run `--set-cookie`.
3. Install the cron entry: `17 * * * * /usr/bin/python3 /path/visa_watch.py >> /path/visa_watch.log 2>&1`
4. Watch the first session-expiry alert and tune the interval.
