"""Is the visa site up? One small state machine, shared by the Pi's date checks and the
sign-in loop (src/signin imports this module), so both treat an outage the same way.

    up        (no record) "actual" requests run on their own schedule: the Pi's date checks,
              the sign-in loop's renewals.
    checking  an actual request failed the way it does when the whole site is down (no
              connection, HTTP 502/503/504). No more actual requests; instead a ping right
              away and then every PING_EVERY. The first answer: back to up, it was a blip.
              CONFIRM_PINGS pings without one: down.
    down      still no actual requests; a ping every PING_EVERY until the first answer: up.

A ping is one GET of the site's home page, without cookies, not following redirects. Any
answer except a gateway error counts: a sign-in page, a redirect, even a 403 means the site
is there. The record is a plain dict, so the Pi keeps it in state.json and the sign-in loop
in memory. Standard library only, and no clock of its own: callers pass `now`.
"""

from __future__ import annotations

import http.client
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from .timeutil import iso, parse_ts

PING_EVERY = timedelta(minutes=3)  # the same for the Pi and the sign-in loop
CONFIRM_PINGS = 3  # at +0, +3 and +6 minutes: a blip that short doesn't count as down
PING_TIMEOUT = 20  # seconds
# The front server answering for an application that isn't there: the whole site is down,
# not just one request. HTTP 500 is the application answering, so it isn't one of them.
DOWN_STATUSES = (502, 503, 504)


def home_url(base: str, locale: str) -> str:
    """The page a ping asks for: https://ais.usvisa-info.com/en-kz/niv."""
    return f"{base.rstrip('/')}/{locale}/niv"


def failed(site: dict | None, detail: str, now: datetime) -> dict:
    """An actual request failed the way it does when the site is down: start checking.
    (Already checking or down: only the error is noted.)"""
    if site:
        return {**site, "last_error": detail}
    return {"state": "checking", "since": iso(now), "pings": 0, "next_ping": iso(now), "last_error": detail}


def ping_due(site: dict | None, now: datetime) -> bool:
    next_ping = parse_ts((site or {}).get("next_ping"))
    return bool(site) and (next_ping is None or now >= next_ping)


def pinged(site: dict, answered: bool, detail: str, now: datetime) -> dict | None:
    """Fold one ping into the record. None means the site is up."""
    if answered:
        return None
    site = {**site, "pings": site.get("pings", 0) + 1, "last_error": detail}
    site["next_ping"] = iso(now + PING_EVERY)
    if site.get("state") == "checking" and site["pings"] >= CONFIRM_PINGS:
        site.update(state="down", down_at=iso(now))
    return site


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # a redirect is an answer


def ping(url: str, user_agent: str, timeout: float = PING_TIMEOUT) -> tuple[bool, str]:
    """GET the home page once. Returns (the site answered, what happened)."""
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(urllib.request.Request(url, headers=headers), timeout=timeout) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
        e.close()
    except (OSError, http.client.HTTPException) as e:  # DNS, TLS, timeouts, resets
        return False, f"network: {type(e).__name__}: {e}"[:200]
    return status not in DOWN_STATUSES, f"HTTP {status}"
