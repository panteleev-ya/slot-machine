# slot-machine: context for Claude Code

Started from a claude.ai draft (`visa_watch.py`, Nov 2026), restructured into a package.
User-facing overview, setup and constraints: README.md.

## Goal
Hourly check of the earliest open appointment date for a US B1/B2 visa at the U.S. Embassy
in Astana (AIS site `ais.usvisa-info.com/en-kz`), with Telegram notifications:
- immediately (with sound) when a sooner date opens than at the previous check;
- once a day (silent) with a summary: earliest date, change since the last summary,
  best date seen in between, number of checks, and session health.
Read-only: it never books, cancels or reschedules. Keep it that way.

## Layout
- `src/slot_machine/`: stdlib-only package (Python 3.11+). `watcher.py` holds the decision
  logic and must stay pure (state + FetchResult + now -> messages). Everything reads the
  clock through `timeutil.utcnow()`; tests replace it there.
- `tests/`: pytest. `fakes.py` is a stdlib fake AIS site + fake Telegram Bot API, also runnable
  standalone (used by `deploy/smoke-pi.sh` on the Pi). `conftest.py` isolates HOME and XDG dirs.
- `deploy/build.py`: builds `dist/slot-machine` (zipapp with its own `__main__.py`, because
  zipapp's generated entry point drops the exit code). `deploy/deploy.sh`: install on the Pi,
  cron on/off. `deploy/smoke-pi.sh`: run the installed app on the Pi against the fakes.
- `config/slot-machine.env.example`: settings template (committed; a test keeps it in sync with
  `ENV_KEYS`). New settings and their comments go here. `config/slot-machine.env` is the user's
  filled copy: git-ignored, chmod 600, holds the bot token. Don't read or print it.
  `deploy/sync_config.py` (`make config`) rebuilds it as "template + the user's values" and validates;
  `make push-config` / `make deploy` upload it to the Pi. `CRON_SCHEDULE` is read only by `deploy.sh`
  (via `sync_config.py --cron-schedule`), never by the app; it is validated to five plain crontab
  fields because it is written into the Pi's crontab through a remote shell.

- Telegram fanout: `TELEGRAM_CHAT_ID` is a comma list; `Msg.to` picks recipients (None = all;
  `expired`/`error` go to the first chat only). `state["welcomed_chats"]` tracks who got a baseline
  or welcome; state without it is treated as "only the first chat was greeted". A message is rolled
  back only if it reached nobody; a missed baseline/welcome is retried for that chat alone.

- Telegram commands: `bot.py`, run by cron every minute as `--poll-telegram` (long-polls ~55 s, then
  exits; own lock `.telegram.lock`, own file `telegram.json` with the offset, last /check time and
  chats seen outside the list). The offset is saved before a command runs, so a crash never
  repeats it. `/check` calls `run_check(force=True, via="telegram:<chat>")`, with a cooldown.
- Per-chat cutoffs: `/set_alert_before` stores `telegram.json["alert_before"][chat]` ("" = off; missing =
  ALERT_ONLY_BEFORE). The runner reads that file and passes it to `process()`, which sends a "sooner"
  alert only to chats whose cutoff the date beats. Only the poller writes `telegram.json`.
- Log lines end with `[via]`: cron, check-now, set-cookie, telegram:<chat>, manual.

## Workflow
- `make dev`, `make test`, `make lint` (ruff; the formatter owns line length). Run both before committing.
- `make deploy` then `make smoke-pi` after any change that should reach the Pi.
- Git: `main` holds the imported draft; work happens on branches.

## Rules for Claude
- **Never send requests to the real AIS site** (no curl, no live `--set-cookie`/`--discover`/
  real check). Its terms prohibit bots; the auto-mode classifier also blocks it. Anything live
  against AIS is the user's step (`make set-cookie`, `make discover`, `make cron-on`).
- Don't handle the user's `_yatri_session` cookie or Telegram token; the token lives in
  `config/slot-machine.env` (edited by the user), the cookie only in the Pi's state via `make set-cookie`.
- Tests and smoke runs talk only to `tests/fakes.py`.

## Host
- Raspberry Pi Zero 2 W `yarikos` on the home network (`yarikos.local`, 10.0.0.85), running the
  `pi-gen-yarik-os` image (Raspberry Pi OS Lite, Debian 13 trixie, arm64). Python 3.13, cron
  (active), systemd-timesyncd, tzdata, ca-certificates present. System TZ Europe/London.
- Access: `ssh -i ~/.ssh/pi -o IdentitiesOnly=yes yarikp@yarikos.local` (key auth; add
  `-o LogLevel=ERROR` to hide the leftover first-boot banner), or UART at `/dev/cu.usbserial-*`,
  115200 baud. `sudo` needs the user's password.
- Installed per README: `~/.local/bin/slot-machine`, `~/.config/slot-machine/slot-machine.env`,
  `~/.local/state/slot-machine/`, crontab line marked `# slot-machine (managed by deploy.sh)`.

## Open questions
- How long a `_yatri_session` survives hourly use (first "session ended" alert will tell).
- Whether AIS answers the Pi's requests at all: a bare unauthenticated request from the Pi got
  HTTP 403. Only the user's first `make set-cookie` will show it.
