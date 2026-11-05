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

## The browser's request (Copy as cURL, 2026-11-05)

Header names Chrome sends on the days request (values omitted; several are secrets):

- `Accept: application/json, text/javascript, */*; q=0.01` and `X-Requested-With: XMLHttpRequest`,
  the same as `ais.ais_headers()`.
- `Referer`: the appointment page URL **with a query string** listing the applicant IDs plus
  `confirmed_limit_message=1&commit=Continue`. The code sends the bare appointment URL.
- **`X-CSRF-Token`**: Rails' per-page token from the appointment page's `<meta name="csrf-token">`.
  The code doesn't send it. Rails normally checks it only on POST, so a GET probably works without
  it. Unconfirmed (experiment 3).
- Cookies: `_yatri_session` plus Google Analytics cookies (`_ga`, `_gid`, `_ga_*`), which the
  server shouldn't need.
- `User-Agent` (Chrome 152 on macOS), `Accept-Language`, `Sec-Fetch-Dest/Mode/Site`,
  `sec-ch-ua*` client hints.
- `If-None-Match` with the ETag of the browser's last copy.

**Replaying it with curl from the Mac returned `304 Not Modified` with no body.** The copied
`If-None-Match` made it a conditional request, and the dates hadn't changed since the browser's
copy. So the server accepted the session and the request outside the browser; only the body was
skipped. Rerun without `If-None-Match` to get `200` and the JSON. The code never sends
`If-None-Match`, so it won't see 304 as long as it doesn't. Using ETags would save bandwidth, not
requests: a 304 still counts against the throttle.

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

1. ~~Request headers~~: answered above. Still open: the `id` of the location `<select>`
   (`cli.cmd_discover` looks for one containing `facility_id`).
2. **Plain curl from the Mac:** answered in part. It got `304`, so the session works outside the
   browser. Still to see: `200` plus the JSON body without `If-None-Match`.
3. **Which parts are needed:** status without `X-CSRF-Token`, without `X-Requested-With`, with only
   `_yatri_session` as the cookie, without `Sec-*`/`sec-ch-ua*`, with the bare Referer, and without
   `User-Agent`.
4. **From the Pi:** does the same request work from `yarikos`, the machine that runs the checks?
5. **Session lifetime:** does the cookie still work after about 1 h and about 3 h without using the site?

## Possible code changes once those are answered

- `tests/fakes.py`: re-issue `_yatri_session` on every days response, as the real site does, so the
  tests exercise cookie rotation on every check instead of only when a test asks for it.
- Request headers in `ais.ais_headers()`: match whatever experiment 3 shows is required, and no more.
