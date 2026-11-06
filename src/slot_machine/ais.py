"""Talking to the AIS appointment site (ais.usvisa-info.com).

One read-only request per check: the JSON list of open days for one location,
fetched with the session cookie from a browser sign-in. Also the page parsing
behind --discover.
"""

from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .config import Config
from .timeutil import DATE_RE

MAX_BODY = 2_000_000


@dataclass
class FetchResult:
    kind: str  # ok | empty | expired | throttled | error | skipped
    dates: list = field(default_factory=list)
    detail: str = ""
    cookies: dict | None = None  # refreshed cookies, only after a good answer


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # surface 3xx so a bounce to the sign-in page is visible


def http_get(url: str, headers: dict, follow_redirects: bool = False, timeout: int = 30):
    """Return (status, headers, body). Network failures raise."""
    opener = urllib.request.build_opener(*([] if follow_redirects else [_NoRedirect()]))
    try:
        with opener.open(urllib.request.Request(url, headers=headers), timeout=timeout) as resp:
            return resp.status, resp.headers, resp.read(MAX_BODY)
    except urllib.error.HTTPError as e:
        try:
            body = e.read(MAX_BODY)
        except Exception:
            body = b""
        return e.code, e.headers, body


# --------------------------------------------------------------------------- cookies


def cookie_header(cookies: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def merge_set_cookies(cookies: dict, headers) -> dict:
    """Apply Set-Cookie headers (the session cookie is re-issued as you go)."""
    out = dict(cookies)
    for line in (headers.get_all("Set-Cookie") or []) if headers is not None else []:
        first, _, attrs = line.partition(";")
        name, eq, value = first.strip().partition("=")
        if not eq or not name:
            continue
        attrs = attrs.lower()
        if value in ("", "deleted") or re.search(r"max-age=0\b", attrs) or "01 jan 1970" in attrs:
            out.pop(name, None)
        else:
            out[name] = value
    return out


def parse_cookie_input(raw: str) -> dict:
    """Accept a bare _yatri_session value or a whole 'Cookie:' header copied from DevTools."""
    s = raw.strip().strip("'\"")
    if s.lower().startswith("cookie:"):
        s = s[7:].strip()
    if s.startswith("_yatri_session=") or "; _yatri_session=" in s or ";_yatri_session=" in s:
        jar = {}
        for part in s.split(";"):
            name, eq, value = part.strip().partition("=")
            if eq and name:
                jar[name] = value
    else:
        jar = {"_yatri_session": s}
    value = jar.get("_yatri_session", "")
    if len(value) < 20 or any(c.isspace() for c in value):
        raise ValueError("that doesn't look like a _yatri_session cookie value")
    return jar


# --------------------------------------------------------------------------- the dates request


def days_referer(cfg: Config) -> str:
    # The page the browser is on when it loads the dates, exactly as Chrome sent it on
    # 2026-11-05, including the "&amp;" the site's own link puts into that URL.
    return f"{cfg.appointment_url}?confirmed_limit_message=1&amp;commit=Continue"


def clean_user_agent(value) -> str | None:
    """A browser's User-Agent as sent with a new session, or None if it isn't a plausible one
    (it goes into a request header, so printable ASCII only, and not absurdly long)."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not 10 <= len(value) <= 512 or not all(" " <= ch <= "~" for ch in value):
        return None
    return value


def ais_headers(cfg: Config, cookies: dict, json_api: bool = True) -> dict:
    """Headers of the request the Reschedule page itself makes (docs/ais-notes.md).

    Only what describes this kind of request: a same-site background fetch of JSON.
    Version-specific client hints (sec-ch-ua*) are left out because they would go
    stale against the User-Agent; _yatri_session is the only cookie the site needs.
    cfg.user_agent is the browser the session came from (runner.session_cfg).
    """
    headers = {
        "User-Agent": cfg.user_agent,
        "Cookie": cookie_header(cookies),
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    if json_api:
        headers.update(
            {
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": days_referer(cfg),
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            }
        )
    else:
        headers["Accept"] = "text/html,application/xhtml+xml"
    return headers


def classify(status: int, headers, body: bytes, cookies: dict) -> FetchResult:
    if status in (301, 302, 303, 307, 308):
        where = headers.get("Location", "") if headers is not None else ""
        if "sign_in" in where:
            return FetchResult("expired", detail=f"HTTP {status} to the sign-in page")
        return FetchResult("error", detail=f"HTTP {status} redirect to {where[:120]}")
    if status == 401:
        return FetchResult("expired", detail="HTTP 401")
    if status in (403, 429):
        return FetchResult("throttled", detail=f"HTTP {status}")
    if status != 200:
        return FetchResult("error", detail=f"HTTP {status}")
    text = body.decode("utf-8", "replace").strip()
    try:
        data = json.loads(text)
    except ValueError:
        if "user_email" in text or "sign_in" in text:
            return FetchResult("expired", detail="got the sign-in page")
        return FetchResult("error", detail=f"non-JSON reply: {text[:80]!r}")
    if isinstance(data, dict):
        msg = str(data.get("error") or data)[:120]
        kind = "expired" if "sign in" in msg.lower() else "error"
        return FetchResult(kind, detail=f"JSON reply: {msg}")
    if not isinstance(data, list):
        return FetchResult("error", detail="unexpected JSON shape")
    dates = sorted(
        {
            str(item["date"])
            for item in data
            if isinstance(item, dict) and DATE_RE.match(str(item.get("date", "")))
        }
    )
    fresh = merge_set_cookies(cookies, headers)
    if dates:
        return FetchResult("ok", dates=dates, cookies=fresh)
    if data:  # a list, but nothing that looks like {"date": "YYYY-MM-DD"}
        return FetchResult("error", detail="reply had no recognisable dates (did the site change?)")
    return FetchResult("empty", detail="no dates in reply", cookies=fresh)


def fetch_dates(cfg: Config, cookies: dict) -> FetchResult:
    url = (
        f"{cfg.ais_base}/{cfg.locale}/niv/schedule/{cfg.schedule_id}"
        f"/appointment/days/{cfg.facility_id}.json?appointments%5Bexpedite%5D=false"
    )
    try:
        status, headers, body = http_get(url, ais_headers(cfg, cookies))
    except Exception as e:  # DNS, TLS, timeouts, resets
        return FetchResult("error", detail=f"network: {type(e).__name__}: {e}"[:200])
    return classify(status, headers, body, cookies)


# --------------------------------------------------------------------------- pages (for --discover)

SELECT_RE = re.compile(r"<select[^>]*\bid=\"([^\"]*facility_id[^\"]*)\"[^>]*>(.*?)</select>", re.S | re.I)
OPTION_RE = re.compile(r"<option[^>]*\bvalue=\"(\d+)\"[^>]*>\s*([^<]+?)\s*</option>", re.S | re.I)


def parse_facility_options(page: str) -> list:
    found = []
    for select_id, inner in SELECT_RE.findall(page):
        options = [(v, html.unescape(n)) for v, n in OPTION_RE.findall(inner)]
        if options:
            found.append((select_id, options))
    return found


def is_sign_in_page(page: str) -> bool:
    return "user_email" in page
