# slot-machine

Watches the earliest open appointment date for a US tourist (B1/B2) visa at the
U.S. Embassy in Astana on the AIS booking site, and reports to Telegram:

- **Alert with sound** as soon as a date opens that is sooner than at the previous check. With
  `ALERT_ONLY_BEFORE` set (your current appointment), only dates before it count, the alert says how
  many days before it the date is, and you also hear when the earliest date moves *later* but is still
  before it (the one you were told about is gone; the next may be worth taking).
- **Quiet daily summary** at `DIGEST_HOUR`: earliest date, change since yesterday, best date
  seen in between, number of checks, session health. If it stops arriving, something broke.
- **Session ended** when the site logs the session out, then **watch resumed** once someone renewed it
  (with how long checks were paused), and **checks failing** after 3 in a row.
- **Several people** can get the alerts: `TELEGRAM_CHAT_ID` takes a comma-separated list. Everyone
  gets the date alerts, the summary and "session ended" (anyone listed can renew it), and everyone who
  got "session ended" gets "watch resumed", so nobody else signs in for nothing. Only the first chat
  (yours) gets "checks failing". Nobody can subscribe themselves. To add someone: they message the bot (`/start`),
  `make find-chat-id` shows their id, add it to `TELEGRAM_CHAT_ID`, `make push-config`. The next check
  sends them a welcome with the current earliest date.
- **`/check` in Telegram** (in a listed chat) runs a check right away and answers with the earliest
  date, like `make check-now`; at most once every `TELEGRAM_CHECK_COOLDOWN_MINUTES` (2). Other chats get
  no answer. Commands older than 10 minutes, such as ones sent while the Pi was offline, are ignored.
- **`/set_alert_before YYYY-MM-DD`** in Telegram: sooner-date alerts *in that chat* only for dates before
  that day. `/set_alert_before off` for every sooner date again, `/set_alert_before` alone shows the
  setting. Each chat has its own, so nobody changes what someone else gets; `ALERT_ONLY_BEFORE` in the
  settings is the default for chats that haven't set one. The daily summary isn't affected.

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
| Telegram commands | crontab, every minute: `slot-machine --poll-telegram` waits about a minute for commands, answers, exits |
| Renewal page | crontab, every minute: `slot-machine --serve` starts the page unless it's running (`RENEW_URL`, off when empty) |
| Log | `~/.local/state/slot-machine/watch.log`, one line per check, tagged with what started it (`[cron]`, `[check-now]`, `[telegram:<chat>]`, `[set-cookie]`); `make logs` |

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
   `USER_AGENT` to your browser's `navigator.userAgent` (the site seems to reject a session sent
   with another browser version; the Chrome extension sends its own, so this only matters for
   `make set-cookie`). Then `make push-config`.
3. **Session cookie.** DevTools → Application → Cookies → `ais.usvisa-info.com`, copy the value of
   `_yatri_session`, then `make set-cookie` and paste it (input is hidden). It runs one check and
   sends the first message.
4. **Start the schedule.** `make cron-on`. To change when it runs later: `make edit-config`, set
   `CRON_SCHEDULE` (five crontab fields, Pi time zone), `make push-config`. A running schedule updates at once.

**Renewing the session.** Sessions last about an hour from sign-in, whatever the Pi does. To make
renewing quick, the Pi runs a small page on the home network and there's a Chrome extension:

1. `make renew-token` once. It turns the page on (`RENEW_URL`, `RENEW_TOKEN`), pushes the settings,
   and copies the token to the clipboard.
2. Install the extension from `extension/` (see [extension/README.md](extension/README.md)) and paste
   the token in its options.
3. When "session ended" arrives: sign in to the visa site in Chrome, click the extension. Done.
   The Pi tries a new session before saving it, so sending one that doesn't work (not signed in
   yet, say) never replaces one that does.

To let someone else in `TELEGRAM_CHAT_ID` renew it too: `make share-extension` zips the extension
into `dist/slot-machine-extension.zip` and puts the current token on the clipboard (nothing is
rotated). Send them both; they install it the same way. They sign in with the same visa account.

The alert also links to the page (valid only until that session is renewed), where you can paste a
cookie by hand; `/renew` in Telegram sends the current link to any listed chat. The page is plain http and
for the home network only: never forward its port. It only accepts a session, never shows one.

## Signing in from Chrome

The Pi never logs in. A session lasts about an hour from sign-in, so a browser has to sign in
again and hand the cookie over. `src/signin` does that and nothing else: it signs in, follows
Continue, and POSTs `_yatri_session` plus that Chrome's User-Agent to the renewal page, the same
request the extension sends. It does not book, cancel, or reschedule. It is not part of the
zipapp on the Pi.

Two files, both git-ignored and chmod 600:

| File | What |
|---|---|
| `config/ais-login.env` | `AIS_EMAIL` and `AIS_PASSWORD`. Start from `config/ais-login.env.example`. |
| `config/slot-machine.env` | `RENEW_URL` and `RENEW_TOKEN`, the same values the extension uses. |

On the Mac, in the Chrome you already use. Turn on View → Developer → Allow JavaScript from Apple Events first.

```bash
PYTHONPATH=src python3 -m signin --init          # creates config/ais-login.env if it is missing
PYTHONPATH=src python3 -m signin                  # every 12 minutes
PYTHONPATH=src python3 -m signin --every 50 --jitter 3
```

`make signin` is the 12-minute loop. The first cycle uses the Chrome session already open. Later
cycles sign out and sign in, so the Pi receives a new session. Ctrl+C leaves that Chrome window up.
The first run asks macOS to control Chrome and to read the "Chrome Safe Storage" keychain item
(the cookie is HttpOnly). Allow both.

The same package runs on Linux against real Google Chrome on a virtual screen, which is the
stand-in for a mini-PC with no monitor. It is not Chrome's headless mode.

```bash
src/signin/docker/run.sh --check                  # prove Chrome looks like a normal window
src/signin/docker/run.sh --every 50 --jitter 3    # renew until Ctrl+C
src/signin/docker/run.sh --once                   # one cycle, then exit
```

`--every` is the gap in minutes. `--jitter` adds a random amount between minus and plus that many
minutes, and it has to be smaller than `--every`. A failed cycle waits 2 minutes. The browser
profile is kept between Docker runs. If a cycle fails, the window is saved to
`src/signin/shots/last.png`.

Fifty minutes, plus or minus three, is the overnight pace: a session dies about an hour after
sign-in. Twelve minutes is only for a short test.

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
  bot.py        Telegram commands (/check, /set_alert_before, /renew)
  renew.py      the renewal page (HTTP server); renew_link.py the alert's one-session link
  telegram.py   Bot API
  config.py     settings file + environment, XDG paths
  store.py      private atomic JSON files, single-run lock
  timeutil.py   clock and formatting
tests/          fakes.py (fake site + bot API), one test file per module, test_zipapp.py
deploy/         build.py (zipapp), deploy.sh, smoke-pi.sh, sync_config.py (template -> your copy),
                renew_token.py (make renew-token)
extension/      the Chrome extension that sends a fresh session to the renewal page
src/signin/     Chrome sign-in (python -m signin); docker/run.sh runs it on Linux
config/         slot-machine.env.example and ais-login.env.example (committed);
                slot-machine.env and ais-login.env (yours, git-ignored)
```

Tests and the smoke run only ever talk to the fake servers.

## Things to know

- **The site's terms prohibit bots and scripts**, and say using them can delay your application.
  This stays read-only and slow (one request an hour, 30-minute floor, 3-hour pause after an empty
  answer or HTTP 403/429), but the risk to the account is real and yours.
- **Throttling:** users report empty answers after about 48 requests, lifting after about 5 hours.
- **Sessions last about an hour from sign-in**, whatever the Pi does with them. Renew from Chrome
  (`python -m signin` or `src/signin/docker/run.sh`) before that hour is up. The hourly check on
  the Pi does not keep the session alive.
- **Hourly is too slow to catch individual cancellations**; it shows the trend and new batches.
- **The cookie is a login** to an account holding passport and DS-160 data. It lives only on the
  Pi, chmod 600; Telegram messages contain only dates.
- **The dates endpoint is undocumented.** If the site changes, you'll get the "checks failed" alert.
