# AIS site: what we know

Findings about `ais.usvisa-info.com` (Kazakhstan, `en-kz`), from community scripts and the
user's own browser and terminal. No official documentation exists. Identifiers that belong to
the account (schedule ID, email, cookie) are left out on purpose: they go in the git-ignored
`config/slot-machine.env`, the Pi's state, or nowhere.

## The endpoint (confirmed 2026-11-05)

Signed in normally, then Reschedule Appointment → Astana, with DevTools open.

- **Schedule ID** comes from `/en-kz/niv/schedule/<SID>/continue_actions` after Continue.
- **Facility:** Astana is `134`. The option is
  `<option data-collects-biometrics="false" value="134">Astana</option>`, so there is no separate
  biometrics (ASC) appointment to track.
- **Days request:** `GET /en-kz/niv/schedule/<SID>/appointment/days/134.json?appointments[expedite]=false`
  returns `200`, `content-type: application/json; charset=utf-8`.
- **Body:** a JSON list sorted by date, each item `{"date": "YYYY-MM-DD", "business_day": true}`.
  That is what `ais.classify()` and `tests/fakes.py` assume. On 2026-11-05 there were 19 dates,
  from 2028-09-01 to 2028-11-29, so the earliest was **about 22 months out**.
- **Response headers worth knowing:**
  - `server: nginx`, with no Cloudflare headers (`cf-ray`, `cf-cache-status`).
  - **`set-cookie: _yatri_session=...; path=/; HttpOnly; Secure; SameSite=None` on every JSON
    reply.** The session cookie is re-issued each time, so the newest value has to be saved.
    `ais.merge_set_cookies()` and the runner do this, and `tests/fakes.py` re-issues it the same way.
  - **`x-yatri-email` carries the account's email address**, along with `x-yatri-country: kz`,
    `x-yatri-language: en`, `x-yatri-roles: self_service`, `session-id`, `x-request-id` and `etag`.
    Never log response headers. The code logs none.

## Which request works outside the browser

**Chrome's own request** (Copy as cURL) carries, besides `Accept` and `X-Requested-With`:
an `X-CSRF-Token` (Rails' per-page token), a `Referer` whose query string lists the applicant IDs,
Google Analytics cookies (`_ga`, `_gid`, `_ga_*`) next to `_yatri_session`, the `sec-ch-ua*` client
hints, and an `If-None-Match` with the ETag of the browser's cached copy.

- **Replayed as is from the Mac: `304 Not Modified`, no body.** The `If-None-Match` made it a
  conditional request. The server accepted the session; it just had nothing new to send. A 304
  still counts as a request for throttling, so ETags would save bandwidth, not requests. The code
  never sends `If-None-Match` (a test checks this).
- **Trimmed down, from the Mac: `200` and the full JSON.** That request had only `_yatri_session`
  as its cookie, **no `X-CSRF-Token`**, and a Referer **without the applicant IDs**:

  ```
  GET /en-kz/niv/schedule/<SID>/appointment/days/134.json?appointments[expedite]=false
  Accept: application/json, text/javascript, */*; q=0.01
  Accept-Language: en-US,en;q=0.9
  Cache-Control: no-cache
  Pragma: no-cache
  Connection: keep-alive
  Cookie: _yatri_session=<cookie>
  Referer: https://ais.usvisa-info.com/en-kz/niv/schedule/<SID>/appointment?confirmed_limit_message=1&amp;commit=Continue
  Sec-Fetch-Dest: empty
  Sec-Fetch-Mode: cors
  Sec-Fetch-Site: same-origin
  User-Agent: <Chrome 152 on macOS, the browser the cookie came from>
  X-Requested-With: XMLHttpRequest
  sec-ch-ua, sec-ch-ua-mobile, sec-ch-ua-platform: <Chrome 152, macOS>
  ```

  The `&amp;` in the Referer is literal: it's in the URL of the site's own link, and Chrome sends it.
  Which of the remaining headers are actually required wasn't tested one by one.

**What the code sends** (`ais.ais_headers()`, since commit `7981b1a`): that request without the
`sec-ch-ua*` hints. Those state a Chrome version and platform, so hardcoded values would contradict
`USER_AGENT` after the next browser update. `Connection` is `close` (urllib's default); the request
is made once an hour. If the site ever rejects the code but not curl, the client hints are the first
difference to check. `USER_AGENT` in the settings should be the `navigator.userAgent` of the browser
the cookie was copied from.

## From community scripts (2022-2025, not verified here)

- **Times:** `.../appointment/times/<FACILITY>.json?date=YYYY-MM-DD&appointments[expedite]=false` returns
  an object with an `available_times` list. Not needed for "earliest day".
- **Throttling shows up as `[]`**, not as an error status. Reported: empty after about 48 requests,
  or after 3-4 hours of polling every 1-5 minutes, then about 5 hours before answers come back.
  HTTP 429 was reported on the reschedule POST, not on the days request.
- **How they fetch:** the published scripts load the JSON URL in a real Chrome (Selenium) and read
  the `<pre>` text. `_yatri_session` is the only cookie any of them handles.

Sources: [Svision gist](https://gist.github.com/Svision/04202d93fb32d14f00ac746879820722),
[orendon gist](https://gist.github.com/orendon/e18e54b65c101817cb54b0f8c8561152),
[virgs/us-visa-scheduler](https://awesome.ecosyste.ms/projects/github.com%2Fvirgs%2Fus-visa-scheduler).

## Still open

1. **From the Pi:** does the code's request work from `yarikos`, the machine that runs the checks?
   A bare request without a session from the Pi got `403` on 2026-11-05; the first `make set-cookie`
   answers this.
2. **Session lifetime:** how long `_yatri_session` stays valid when used once an hour. The first
   "session ended" alert reports it.
3. **The location `<select>`'s `id`**, which `--discover` looks for (one containing `facility_id`).
   Only matters for `--discover`; the facility ID is already known.
