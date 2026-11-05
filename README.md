# slot-machine

Watches the earliest open appointment date for a US tourist (B1/B2) visa at the
U.S. Embassy in Astana on the AIS booking site, and reports to Telegram:

- **Alert with sound** as soon as a date opens that is sooner than at the previous check
  (optionally only before `ALERT_ONLY_BEFORE`).
- **Quiet daily summary** at `DIGEST_HOUR`: earliest date, change since yesterday, best date
  seen in between, number of checks, session health. If it stops arriving, something broke.
- **Session ended** when the site logs the session out, and **checks failing** after 3 in a row.
- **Several people** can get the alerts: `TELEGRAM_CHAT_ID` takes a comma-separated list. Everyone
  gets the date alerts and the summary; only the first chat (yours) gets "session ended" and
  "checks failing". Nobody can subscribe themselves. To add someone: they message the bot (`/start`),
  `make find-chat-id` shows their id, add it to `TELEGRAM_CHAT_ID`, `make push-config`. The next check
  sends them a welcome with the current earliest date.

It only reads. It never books, cancels or reschedules.

## How it runs

One Python file with no dependencies (a zipapp of `src/slot_machine`), run hourly by the
`yarikp` user's crontab on the Raspberry Pi `yarikos`. No sudo needed.

| What | On the Pi |
|---|---|
| Program | `~/.local/bin/slot-machine` |
| Settings | `~/.config/slot-machine/slot-machine.env` (chmod 600) |
| State, session cookie, log | `~/.local/state/slot-machine/` (chmod 600) |
| Schedule | crontab, `CRON_SCHEDULE` in the settings (default `17 * * * *`, hourly at :17); `make cron-on` / `make cron-off` switch it |

The site's sign-in form has a captcha, so the app can't log in. You sign in in your
browser and hand it the `_yatri_session` cookie; it uses that session until the site ends it.

## Setup (all from the Mac)

`make deploy` has already installed the app on the Pi.

Settings live in two files in `config/`. The **template** `slot-machine.env.example` is committed: it
lists every setting with its comment. **Your copy** `slot-machine.env` holds your values (token, IDs);
it is git-ignored and chmod 600. `make edit-config` creates or refreshes your copy from the template
(keeping your values, adding any new settings) and opens it in `$EDITOR`. `make push-config` validates
it and uploads it to the Pi; `make deploy` does that too. Add new settings to the template, not to
your copy: refreshing keeps only values.

1. **Telegram.** Message @BotFather, send `/newbot`, copy the token. `make edit-config`, set
   `TELEGRAM_BOT_TOKEN`, then `make push-config`. Send your bot any message, run `make find-chat-id`,
   put the number in `TELEGRAM_CHAT_ID`, `make push-config` again, then `make test-telegram`.
2. **Your appointment.** Sign in at <https://ais.usvisa-info.com/en-kz/niv/users/sign_in> and click
   Continue: the URL reads `.../niv/schedule/<AIS_SCHEDULE_ID>/continue_actions`. For
   `AIS_FACILITY_ID`, open the Reschedule page with DevTools → Network open, pick Astana, and read
   the number in `.../appointment/days/<ID>.json` (or run `make discover` after step 3).
   While in the settings, check `TIMEZONE`, `DIGEST_HOUR`, `ALERT_ONLY_BEFORE`, and set
   `USER_AGENT` to your browser's `navigator.userAgent`. Then `make push-config`.
3. **Session cookie.** DevTools → Application → Cookies → `ais.usvisa-info.com`, copy the value of
   `_yatri_session`, then `make set-cookie` and paste it (input is hidden). It runs one check and
   sends the first message.
4. **Start the schedule.** `make cron-on`. To change when it runs later: `make edit-config`, set
   `CRON_SCHEDULE` (five crontab fields, Pi time zone), `make push-config`. A running schedule updates at once.

Day to day: `make status`, `make logs`. `make check-now` runs one check immediately, outside cron (it
skips the interval and any throttle pause, so use it sparingly). After a "session ended" alert: sign in
again, `make set-cookie`.

## Development

```bash
make dev        # .venv with pytest + ruff (Mac only)
make test       # unit tests + full runs against fake AIS/Telegram servers + the built zipapp
make lint
make deploy     # build dist/slot-machine and install it on the Pi (cron untouched)
make smoke-pi   # run the installed app on the Pi against the fake servers, in /tmp
```

```
src/slot_machine/
  cli.py        commands (--set-cookie, --discover, --status, ...)
  runner.py     one scheduled check: lock, load, fetch, decide, send, save
  watcher.py    the decision logic - pure, no I/O
  messages.py   message text
  ais.py        the visa-site client
  telegram.py   Bot API
  config.py     settings file + environment, XDG paths
  store.py      private atomic JSON files, single-run lock
  timeutil.py   clock and formatting
tests/          fakes.py (fake site + bot API), one test file per module, test_zipapp.py
deploy/         build.py (zipapp), deploy.sh, smoke-pi.sh, sync_config.py (template -> your copy)
config/         slot-machine.env.example (committed), slot-machine.env (yours, git-ignored)
```

Tests and the smoke run only ever talk to the fake servers.

## Things to know

- **The site's terms prohibit bots and scripts**, and say using them can delay your application.
  This stays read-only and slow (one request an hour, 30-minute floor, 3-hour pause after an empty
  answer or HTTP 403/429), but the risk to the account is real and yours.
- **Throttling:** users report empty answers after about 48 requests, lifting after about 5 hours.
- **Session length is unknown.** The first "session ended" alert says how long it lasted. If it's
  under an hour, a shorter cron interval might keep it alive, at the cost of more requests.
- **Hourly is too slow to catch individual cancellations**; it shows the trend and new batches.
- **The cookie is a login** to an account holding passport and DS-160 data. It lives only on the
  Pi, chmod 600; Telegram messages contain only dates.
- **The dates endpoint is undocumented.** If the site changes, you'll get the "checks failed" alert.
