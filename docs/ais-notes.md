# AIS site: what we know

Findings about `ais.usvisa-info.com` (Kazakhstan, `en-kz`), from community scripts and the
user's own browser. No official documentation exists. Identifiers that belong to the account
(schedule ID, email, cookie) are left out on purpose: they go in the git-ignored
`config/slot-machine.env` or nowhere.

## Confirmed in the browser (2026-11-05)

Signed in normally, then Reschedule Appointment → Astana, with DevTools open.

- **Schedule ID** comes from `/en-kz/niv/schedule/<SID>/continue_actions` after Continue.
- **Facility:** Astana is `134`. The option is
  `<option data-collects-biometrics="false" value="134">Astana</option>`, so there is no separate
  biometrics (ASC) appointment to track.
- **Days request:** `GET /en-kz/niv/schedule/<SID>/appointment/days/134.json?appointments[expedite]=false`
  returns `200`, `content-type: application/json; charset=utf-8`, gzip.
- **Body:** a JSON list sorted by date, each item `{"date": "YYYY-MM-DD", "business_day": true}`.
  That is exactly what `ais.classify()` and `tests/fakes.py` assume. On that day there were
  19 dates, from 2028-09-01 to 2028-11-29, so the earliest was **about 22 months out**.
- **Response headers worth knowing:**
  - `server: nginx`, with no Cloudflare headers (`cf-ray`, `cf-cache-status`) on this response.
    So the 403 the Pi got for a bare request probably came from the site itself (for example
    user-agent or header checks), not from a Cloudflare challenge. Unconfirmed.
  - **`set-cookie: _yatri_session=...; path=/; HttpOnly; Secure; SameSite=None` on the JSON
    response itself.** The session cookie is re-issued on every request, so the newest value has
    to be saved each time. `ais.merge_set_cookies()` and the runner already do this.
  - **`x-yatri-email` carries the account's email address**, along with `x-yatri-country: kz`,
    `x-yatri-language: en`, `x-yatri-roles: self_service`, `session-id`, `x-request-id` and `etag`.
    Never log response headers. The code currently logs none.

## From community scripts (2022-2025, not verified here)

- **Times:** `.../appointment/times/<FACILITY>.json?date=YYYY-MM-DD&appointments[expedite]=false` returns
  an object with an `available_times` list. Not needed for "earliest day".
- **Throttling shows up as `[]`**, not as an error status. Reported: empty after about 48 requests,
  or after 3-4 hours of polling every 1-5 minutes, then about 5 hours before answers come back.
  HTTP 429 was reported on the reschedule POST, not on the days request.
- **How they fetch:** the published scripts load the JSON URL in a real Chrome (Selenium) and read
  the `<pre>` text. None shows that a plain HTTP request works. `_yatri_session` is the only
  cookie any of them handles.

Sources: [Svision gist](https://gist.github.com/Svision/04202d93fb32d14f00ac746879820722),
[orendon gist](https://gist.github.com/orendon/e18e54b65c101817cb54b0f8c8561152),
[virgs/us-visa-scheduler](https://awesome.ecosyste.ms/projects/github.com%2Fvirgs%2Fus-visa-scheduler).

## Still open (the user's experiments)

1. **Request headers the browser sends** on the days request (names only, no Cookie value):
   does it include `X-Requested-With: XMLHttpRequest`, `Accept: application/json, ...`, `Referer`?
   Also the `id` of the location `<select>` (`cli.cmd_discover` looks for one containing `facility_id`).
2. **Plain curl from the Mac:** does the copied cURL return the same 200 + JSON outside the browser?
3. **Which parts are needed:** status without `X-Requested-With`, with only `_yatri_session` as the
   cookie, and without `User-Agent`.
4. **From the Pi:** does the same request work from `yarikos`, the machine that runs the checks?
5. **Session lifetime:** does the cookie still work after about 1 h and about 3 h without using the site?

## Possible code changes once those are answered

- `tests/fakes.py`: re-issue `_yatri_session` on every days response, as the real site does, so the
  tests exercise cookie rotation on every check instead of only when a test asks for it.
- Request headers in `ais.ais_headers()`: match whatever experiment 3 shows is required, and no more.
