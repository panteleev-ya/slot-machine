#!/usr/bin/env python3
"""
visa_watch.py - watch the earliest US visa appointment date at one AIS location
(default label: U.S. Embassy Astana) and report it to Telegram.

What it does
  * One request per run: asks the appointment site for the open dates at your
    location and keeps the earliest one. Run it hourly from cron.
  * Loud alert as soon as a date opens that is sooner than at the previous
    check (optionally only if it is before ALERT_ONLY_BEFORE).
  * One quiet summary per day at DIGEST_HOUR: earliest date, change since the
    last summary, best date seen in between, check counts, session health.
    It doubles as a heartbeat - no summary means something is broken.
  * Read-only. It never books, cancels or reschedules anything.

Why a session cookie instead of a password
  The AIS sign-in form is protected by hCaptcha, so a script can't sign in by
  itself. You sign in with your browser and give the script the session
  cookie. It keeps using (and refreshing) that session until the site ends it,
  then stops sending requests and asks you on Telegram for a fresh cookie.
  The site's terms prohibit automated tools. The script keeps its footprint
  small (one request per run, a 30-minute floor, back-off when the site goes
  quiet or throttles), but the risk to the account is yours to judge.

Setup
  1. Telegram: message @BotFather -> /newbot -> copy the token into
     visa_watch.env. Send your new bot any message, then run
         python3 visa_watch.py --find-chat-id
  2. Sign in at https://ais.usvisa-info.com/en-kz/niv/users/sign_in in a
     desktop browser and click Continue. The URL now reads
     .../niv/schedule/<AIS_SCHEDULE_ID>/continue_actions
  3. Facility id: open the Reschedule Appointment page with DevTools -> Network
     open, pick Astana in the location list, and read <AIS_FACILITY_ID> from
     the request .../appointment/days/<ID>.json  (or use --discover after 4).
  4. DevTools -> Application (Firefox: Storage) -> Cookies ->
     https://ais.usvisa-info.com, copy the value of _yatri_session, then run
         python3 visa_watch.py --set-cookie
     (input is hidden; it test-runs one check and sends the first message).
  5. cron, hourly at an odd minute:
     17 * * * * /usr/bin/python3 /path/to/visa_watch.py >> /path/to/visa_watch.log 2>&1

Other commands: --status, --test-telegram, --dry-run, --force, --discover.
Standard library only, Python 3.9+. State lives next to the script
(state.json, cookies.json - both chmod 600) unless STATE_DIR says otherwise.
"""
from __future__ import annotations

import argparse
import copy
import getpass
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    import fcntl  # POSIX: stops two runs from overlapping
except ImportError:  # pragma: no cover
    fcntl = None

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36")
# Empty answers for this long mean "really no dates", not a temporary throttle.
EMPTY_IS_REAL_AFTER = timedelta(hours=24)
MAX_BODY = 2_000_000
DOW = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MON = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


# --------------------------------------------------------------------------- config

@dataclass
class Config:
    locale: str = "en-kz"
    schedule_id: str = ""
    facility_id: str = ""
    label: str = "Astana"
    bot_token: str = ""
    chat_id: str = ""
    digest_hour: int = 9
    timezone: str = "America/Los_Angeles"
    digest_silent: bool = True
    alert_only_before: str = ""
    min_interval_minutes: float = 30
    backoff_hours: float = 3
    error_alert_after: int = 3
    user_agent: str = DEFAULT_UA
    state_dir: Path = SCRIPT_DIR
    ais_base: str = "https://ais.usvisa-info.com"
    telegram_api: str = "https://api.telegram.org"

    @property
    def appointment_url(self) -> str:
        return f"{self.ais_base}/{self.locale}/niv/schedule/{self.schedule_id}/appointment"

    @property
    def sign_in_url(self) -> str:
        return f"{self.ais_base}/{self.locale}/niv/users/sign_in"


ENV_KEYS = {
    "AIS_LOCALE": ("locale", str),
    "AIS_SCHEDULE_ID": ("schedule_id", str),
    "AIS_FACILITY_ID": ("facility_id", str),
    "LOCATION_LABEL": ("label", str),
    "TELEGRAM_BOT_TOKEN": ("bot_token", str),
    "TELEGRAM_CHAT_ID": ("chat_id", str),
    "DIGEST_HOUR": ("digest_hour", int),
    "TIMEZONE": ("timezone", str),
    "DIGEST_SILENT": ("digest_silent", "bool"),
    "ALERT_ONLY_BEFORE": ("alert_only_before", str),
    "MIN_INTERVAL_MINUTES": ("min_interval_minutes", float),
    "BACKOFF_HOURS": ("backoff_hours", float),
    "ERROR_ALERT_AFTER": ("error_alert_after", int),
    "USER_AGENT": ("user_agent", str),
    "STATE_DIR": ("state_dir", Path),
    "AIS_BASE_URL": ("ais_base", str),          # for testing against a fake server
    "TELEGRAM_API_BASE": ("telegram_api", str),  # for testing against a fake server
}


def read_env_file(path: Path) -> dict:
    values = {}
    if not path.exists():
        return values
    if os.name == "posix" and path.stat().st_mode & 0o077:
        log(f"warning: {path} is readable by other users; run: chmod 600 {path}")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        values[key.strip()] = val
    return values


def load_config(path: Path) -> Config:
    values = read_env_file(path)
    values.update({k: v for k, v in os.environ.items() if k in ENV_KEYS})
    cfg = Config()
    for key, (attr, kind) in ENV_KEYS.items():
        val = values.get(key, "").strip()
        if not val:
            continue
        if kind == "bool":
            setattr(cfg, attr, val.lower() in ("1", "true", "yes", "on"))
            continue
        try:
            setattr(cfg, attr, kind(val))
        except ValueError:
            raise SystemExit(f"Config error: {key}={val!r} is not valid")
    cfg.ais_base = cfg.ais_base.rstrip("/")
    cfg.telegram_api = cfg.telegram_api.rstrip("/")
    if not 0 <= cfg.digest_hour <= 23:
        raise SystemExit("Config error: DIGEST_HOUR must be 0-23")
    if cfg.alert_only_before and not DATE_RE.match(cfg.alert_only_before):
        raise SystemExit("Config error: ALERT_ONLY_BEFORE must look like 2027-04-30")
    for name, value in (("AIS_SCHEDULE_ID", cfg.schedule_id), ("AIS_FACILITY_ID", cfg.facility_id)):
        if value and not value.isdigit():
            raise SystemExit(f"Config error: {name} should be a number")
    return cfg


# --------------------------------------------------------------------------- time & text helpers

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_ts(value) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


_tz_warned = False


def local(dt: datetime, cfg: Config) -> datetime:
    global _tz_warned
    if ZoneInfo is not None:
        try:
            return dt.astimezone(ZoneInfo(cfg.timezone))
        except Exception:
            pass
    if not _tz_warned:
        log(f"warning: time zone {cfg.timezone!r} unavailable (install tzdata); using system time")
        _tz_warned = True
    return dt.astimezone()


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if abs(n) == 1 else 's'}"


def fmt_day(iso_date: str, today: date) -> str:
    d = date.fromisoformat(iso_date)
    n = (d - today).days
    away = "today" if n == 0 else f"in {plural(n, 'day')}"
    return f"{DOW[d.weekday()]} {MON[d.month - 1]} {d.day}, {d.year} ({away})"


def fmt_short(iso_date: str) -> str:
    d = date.fromisoformat(iso_date)
    return f"{MON[d.month - 1]} {d.day}, {d.year}"


def fmt_dur(seconds: float) -> str:
    if seconds < 60:
        return "less than a minute"
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def fmt_clock(dt: datetime, cfg: Config) -> str:
    t = local(dt, cfg)
    return f"{MON[t.month - 1]} {t.day} {t:%H:%M}"


def days_between(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def esc(value) -> str:
    return html.escape(str(value), quote=False)


# --------------------------------------------------------------------------- talking to AIS

@dataclass
class FetchResult:
    kind: str                       # ok | empty | expired | throttled | error | skipped
    dates: list = field(default_factory=list)
    detail: str = ""
    cookies: dict | None = None     # refreshed cookies, only after a good answer


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


def ais_headers(cfg: Config, cookies: dict, json_api: bool = True) -> dict:
    headers = {"User-Agent": cfg.user_agent, "Cookie": cookie_header(cookies)}
    if json_api:
        headers.update({
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": cfg.appointment_url,
        })
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
    dates = sorted({str(item["date"]) for item in data
                    if isinstance(item, dict) and DATE_RE.match(str(item.get("date", "")))})
    fresh = merge_set_cookies(cookies, headers)
    if dates:
        return FetchResult("ok", dates=dates, cookies=fresh)
    if data:  # a list, but nothing that looks like {"date": "YYYY-MM-DD"}
        return FetchResult("error", detail="reply had no recognisable dates (did the site change?)")
    return FetchResult("empty", detail="no dates in reply", cookies=fresh)


def fetch_dates(cfg: Config, cookies: dict) -> FetchResult:
    url = (f"{cfg.ais_base}/{cfg.locale}/niv/schedule/{cfg.schedule_id}"
           f"/appointment/days/{cfg.facility_id}.json?appointments%5Bexpedite%5D=false")
    try:
        status, headers, body = http_get(url, ais_headers(cfg, cookies))
    except Exception as e:  # DNS, TLS, timeouts, resets
        return FetchResult("error", detail=f"network: {type(e).__name__}: {e}"[:200])
    return classify(status, headers, body, cookies)


# --------------------------------------------------------------------------- telegram

def send_telegram(cfg: Config, text: str, silent: bool = False) -> bool:
    if not (cfg.bot_token and cfg.chat_id):
        log("telegram not configured; message not sent")
        return False
    data = urllib.parse.urlencode({
        "chat_id": cfg.chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_notification": "true" if silent else "false",
        "link_preview_options": json.dumps({"is_disabled": True}),
    }).encode()
    url = f"{cfg.telegram_api}/bot{cfg.bot_token}/sendMessage"
    for attempt in range(2):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
                if json.loads(r.read().decode()).get("ok"):
                    return True
        except urllib.error.HTTPError as e:  # never log the URL: it contains the token
            log(f"telegram HTTP {e.code}: {e.read()[:200]!r}")
            if 400 <= e.code < 500 and e.code != 429:
                return False
        except Exception as e:
            log(f"telegram failed: {type(e).__name__}")
        if attempt == 0:
            time.sleep(3)
    return False


# --------------------------------------------------------------------------- state & decisions

@dataclass
class Msg:
    kind: str
    text: str
    silent: bool = False
    rollback: dict = field(default_factory=dict)  # state to restore if sending fails


def snapshot(state: dict, *keys) -> dict:
    return {k: copy.deepcopy(state.get(k)) for k in keys}


def current_earliest(state: dict) -> str | None:
    """Latest known earliest date, or None if the newest good answer had no dates."""
    if not state.get("last_seen"):
        return None
    seen_at, empty_at = parse_ts(state.get("last_seen_at")), parse_ts(state.get("last_empty_at"))
    if empty_at and (seen_at is None or empty_at > seen_at):
        return None
    return state["last_seen"]


def session_line(cfg: Config, state: dict, now: datetime) -> str:
    expired = parse_ts(state.get("session_expired_at"))
    if expired:
        return f"EXPIRED since {fmt_clock(expired, cfg)} - sign in and run --set-cookie"
    set_at = parse_ts(state.get("session_set_at"))
    line = f"active for {fmt_dur((now - set_at).total_seconds())}" if set_at else "active"
    backoff = parse_ts(state.get("backoff_until"))
    if backoff and backoff > now:
        line += f"; pausing until {fmt_clock(backoff, cfg)} (site went quiet or throttled)"
    return line


def text_baseline(cfg: Config, earliest: str | None, today: date) -> str:
    first = (f"Earliest date now: {fmt_day(earliest, today)}" if earliest else
             "No dates are offered right now (or the site is throttling); you'll hear when that changes.")
    rule = "You'll get an alert when a sooner date opens"
    if cfg.alert_only_before:
        rule += f" before {fmt_short(cfg.alert_only_before)}"
    rule += f", plus a quiet summary daily around {cfg.digest_hour:02d}:00."
    return "\n".join([f"<b>✅ Visa slot watch started - {esc(cfg.label)}</b>", first, rule])


def text_sooner(cfg: Config, earliest: str, prev: str | None, today: date) -> str:
    if prev:
        title = "Sooner visa date opened"
        change = f"Was {fmt_short(prev)} at the previous check: {plural(days_between(earliest, prev), 'day')} sooner."
    else:
        title = "Visa dates available again"
        change = "Earlier checks found no dates at all."
    return "\n".join([
        f"<b>🟢 {title} - {esc(cfg.label)}</b>",
        f"Earliest: {fmt_day(earliest, today)}",
        change,
        f"Slots go fast: {esc(cfg.appointment_url)}",
    ])


def text_expired(cfg: Config, state: dict) -> str:
    set_at, ok_at = parse_ts(state.get("session_set_at")), parse_ts(state.get("session_last_ok_at"))
    lines = ["<b>🔑 Visa slot watch paused - the AIS session ended</b>"]
    if set_at and ok_at and ok_at >= set_at:
        lines.append(f"That session lasted {fmt_dur((ok_at - set_at).total_seconds())}.")
    lines += [
        f"To resume: sign in at {esc(cfg.sign_in_url)}, copy the _yatri_session cookie, then run",
        "<code>python3 visa_watch.py --set-cookie</code>",
        "No requests are sent until then.",
    ]
    return "\n".join(lines)


def text_error(cfg: Config, state: dict) -> str:
    return "\n".join([
        f"<b>⚠️ Visa slot watch: {state.get('consecutive_errors', 0)} checks in a row failed</b>",
        f"Last error: {esc(state.get('last_error', 'unknown'))}",
        "It keeps trying; the daily summary shows when it recovers.",
    ])


def text_digest(cfg: Config, state: dict, now: datetime, today: date) -> str:
    lines = [f"<b>📋 Daily visa slot summary - {esc(cfg.label)}</b>"]
    cur = current_earliest(state)
    last_good = parse_ts(state.get("session_last_ok_at"))
    if cur:
        stale = (state.get("session_expired_at") or last_good is None
                 or now - last_good > timedelta(hours=2))
        lead = (f"Last known earliest (as of {fmt_clock(parse_ts(state['last_seen_at']), cfg)})"
                if stale else "Earliest now")
        lines.append(f"{lead}: {fmt_day(cur, today)}")
    elif state.get("last_empty_at"):
        lines.append(f"No dates offered at the last good check ({fmt_clock(parse_ts(state['last_empty_at']), cfg)}).")
    else:
        lines.append("No successful check yet.")

    prev = state.get("last_digest_earliest")
    if cur and prev:
        d = days_between(prev, cur)
        if d == 0:
            change = "unchanged"
        elif d > 0:
            change = f"{plural(d, 'day')} later (was {fmt_short(prev)})"
        else:
            change = f"{plural(-d, 'day')} sooner (was {fmt_short(prev)})"
        lines.append(f"Since the last summary: {change}")
    elif cur:
        lines.append("Since the last summary: dates became available")
    elif prev:
        lines.append(f"Since the last summary: no dates now (was {fmt_short(prev)})")

    best = state.get("best_since_digest")  # only worth a line if it beat both now and the last summary
    if best and (cur is None or best["date"] < cur) and (prev is None or best["date"] < prev):
        lines.append(f"Best seen in between: {fmt_short(best['date'])} at "
                     f"{fmt_clock(parse_ts(best['at']), cfg)} (gone since)")
    c = state.get("counts") or {}
    lines.append(f"Checks: {c.get('ok', 0)} ok · {c.get('empty', 0)} empty · {c.get('failed', 0)} failed")
    lines.append(f"Session: {session_line(cfg, state, now)}")
    return "\n".join(lines)


def process(state: dict, res: FetchResult, now: datetime, cfg: Config) -> list:
    """Fold one check result into `state` and return the messages to send."""
    msgs = []
    today = local(now, cfg).date()
    counts = state.setdefault("counts", {"ok": 0, "empty": 0, "failed": 0})

    if res.kind != "skipped":
        state["last_check_at"] = iso(now)
    if res.kind in ("ok", "empty"):
        state.update(consecutive_errors=0, error_alert_sent=False, session_last_ok_at=iso(now))

    if res.kind == "ok":
        earliest = res.dates[0]
        counts["ok"] += 1
        state.update(backoff_until=None, empty_since=None)
        prev = state.get("last_seen")
        if not state.get("baseline_sent"):
            msgs.append(Msg("baseline", text_baseline(cfg, earliest, today),
                            rollback=snapshot(state, "baseline_sent", "last_digest_date", "last_digest_earliest")))
            state.update(baseline_sent=True, last_digest_date=today.isoformat(), last_digest_earliest=earliest)
        elif (prev is None or earliest < prev) and (not cfg.alert_only_before or earliest < cfg.alert_only_before):
            # "Sooner" means sooner than at the previous good check, so a slot that
            # vanishes and comes back alerts again - each reappearance is a new chance.
            msgs.append(Msg("sooner", text_sooner(cfg, earliest, prev, today), rollback={"last_seen": prev}))
        best = state.get("best_since_digest")
        if not best or earliest < best["date"]:
            state["best_since_digest"] = {"date": earliest, "at": iso(now)}
        state.update(last_seen=earliest, last_seen_at=iso(now))

    elif res.kind == "empty":
        # AIS answers [] both when nothing is open and when it is quietly throttling
        # you, so back off and don't treat a short empty spell as "dates gone".
        counts["empty"] += 1
        state.update(last_empty_at=iso(now), backoff_until=iso(now + timedelta(hours=cfg.backoff_hours)))
        since = parse_ts(state.get("empty_since"))
        if since is None:
            state["empty_since"] = iso(now)
        elif now - since >= EMPTY_IS_REAL_AFTER:
            state["last_seen"] = None  # a day of nothing: any reopening should alert
        if not state.get("baseline_sent"):
            msgs.append(Msg("baseline", text_baseline(cfg, None, today),
                            rollback=snapshot(state, "baseline_sent", "last_digest_date", "last_digest_earliest")))
            state.update(baseline_sent=True, last_digest_date=today.isoformat(), last_digest_earliest=None)

    elif res.kind == "expired":
        state["session_expired_at"] = iso(now)
        msgs.append(Msg("expired", text_expired(cfg, state), rollback={"session_expired_at": None}))

    elif res.kind in ("throttled", "error"):
        counts["failed"] += 1
        state["consecutive_errors"] = state.get("consecutive_errors", 0) + 1
        state["last_error"] = res.detail
        if res.kind == "throttled":
            state["backoff_until"] = iso(now + timedelta(hours=cfg.backoff_hours))
        if state["consecutive_errors"] >= cfg.error_alert_after and not state.get("error_alert_sent"):
            state["error_alert_sent"] = True
            msgs.append(Msg("error", text_error(cfg, state), rollback={"error_alert_sent": False}))

    if (state.get("baseline_sent") and local(now, cfg).hour >= cfg.digest_hour
            and state.get("last_digest_date") != today.isoformat()):
        rollback = snapshot(state, "last_digest_date", "last_digest_earliest", "best_since_digest", "counts")
        msgs.append(Msg("digest", text_digest(cfg, state, now, today), silent=cfg.digest_silent, rollback=rollback))
        state.update(last_digest_date=today.isoformat(), last_digest_earliest=current_earliest(state),
                     best_since_digest=None, counts={"ok": 0, "empty": 0, "failed": 0})
    return msgs


# --------------------------------------------------------------------------- files

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except ValueError:
        log(f"warning: {path.name} is unreadable; starting fresh")
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


@contextmanager
def single_instance(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    if fcntl is None:
        yield True
        return
    with open(state_dir / ".visa_watch.lock", "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True


# --------------------------------------------------------------------------- commands

def run_check(cfg: Config, force: bool = False, dry_run: bool = False, new_cookies: dict | None = None):
    state_path, cookie_path = cfg.state_dir / "state.json", cfg.state_dir / "cookies.json"
    with single_instance(cfg.state_dir) as got_lock:
        if not got_lock:
            log("another run is in progress; skipping")
            return 0, None
        state = load_json(state_path, {})
        now = utcnow()
        if new_cookies is not None:
            save_json(cookie_path, new_cookies)
            state.update(session_set_at=iso(now), session_expired_at=None, backoff_until=None,
                         consecutive_errors=0, error_alert_sent=False)
        cookies = load_json(cookie_path, {})

        last = parse_ts(state.get("last_check_at"))
        if not force and last and now - last < timedelta(minutes=cfg.min_interval_minutes):
            log(f"last check was {fmt_dur((now - last).total_seconds())} ago (< MIN_INTERVAL_MINUTES); skipping")
            return 0, None

        backoff = parse_ts(state.get("backoff_until"))
        if not (cfg.schedule_id and cfg.facility_id):
            res = FetchResult("skipped", detail="AIS_SCHEDULE_ID / AIS_FACILITY_ID not set")
        elif not cookies.get("_yatri_session"):
            res = FetchResult("skipped", detail="no session cookie yet (run --set-cookie)")
        elif state.get("session_expired_at"):
            res = FetchResult("skipped", detail="session expired (run --set-cookie)")
        elif backoff and now < backoff and not force:
            res = FetchResult("skipped", detail=f"backing off until {fmt_clock(backoff, cfg)}")
        else:
            res = fetch_dates(cfg, cookies)

        if res.cookies is not None and res.cookies != cookies:
            save_json(cookie_path, res.cookies)
        for m in process(state, res, now, cfg):
            if dry_run:
                print(f"--- would send ({m.kind}{', silent' if m.silent else ''}) ---\n{m.text}\n")
            elif not send_telegram(cfg, m.text, m.silent):
                state.update(m.rollback)  # try again on the next run
        if not dry_run:
            save_json(state_path, state)
        log(f"check: {res.kind} {res.dates[0] if res.dates else ''} {res.detail}".rstrip())
        return (0 if res.kind in ("ok", "empty", "skipped") else 1), res


def cmd_set_cookie(cfg: Config, value: str) -> int:
    raw = value or getpass.getpass("Paste the _yatri_session cookie value (input hidden): ")
    try:
        cookies = parse_cookie_input(raw)
    except ValueError as e:
        raise SystemExit(f"Not saved: {e}")
    code, res = run_check(cfg, force=True, new_cookies=cookies)
    if res is None:
        return code
    if res.kind == "ok":
        print(f"Cookie works. Earliest date right now: {res.dates[0]}. Cron takes it from here.")
    elif res.kind == "empty":
        print("Cookie works, but no dates were returned (none open, or throttled).")
    elif res.kind == "expired":
        print("The site rejected that session. Sign in again and copy a fresh _yatri_session value.")
    elif res.kind == "skipped":
        print(f"Cookie saved. {res.detail}. Try --discover, fill visa_watch.env, then run with --force.")
    else:
        print(f"Cookie saved, but the check failed: {res.detail}")
    return code


SELECT_RE = re.compile(r"<select[^>]*\bid=\"([^\"]*facility_id[^\"]*)\"[^>]*>(.*?)</select>", re.S | re.I)
OPTION_RE = re.compile(r"<option[^>]*\bvalue=\"(\d+)\"[^>]*>\s*([^<]+?)\s*</option>", re.S | re.I)


def parse_facility_options(page: str) -> list:
    found = []
    for select_id, inner in SELECT_RE.findall(page):
        options = [(v, html.unescape(n)) for v, n in OPTION_RE.findall(inner)]
        if options:
            found.append((select_id, options))
    return found


def cmd_discover(cfg: Config) -> int:
    cookies = load_json(cfg.state_dir / "cookies.json", {})
    if not cookies.get("_yatri_session"):
        raise SystemExit("Run --set-cookie first.")
    headers = ais_headers(cfg, cookies, json_api=False)
    if not cfg.schedule_id:
        _, _, body = http_get(f"{cfg.ais_base}/{cfg.locale}/niv/account", headers, follow_redirects=True)
        page = body.decode("utf-8", "replace")
        if "user_email" in page:
            raise SystemExit("That session isn't valid any more; sign in again and rerun --set-cookie.")
        ids = sorted(set(re.findall(r"/niv/schedule/(\d+)/", page)))
        print("Schedule ids found:", ", ".join(ids) or "none (read it from the URL after 'Continue')")
        if len(ids) != 1:
            return 0
        cfg.schedule_id = ids[0]
    _, _, body = http_get(cfg.appointment_url, headers, follow_redirects=True)
    page = body.decode("utf-8", "replace")
    if "user_email" in page:
        raise SystemExit("That session isn't valid any more; sign in again and rerun --set-cookie.")
    found = parse_facility_options(page)
    if not found:
        print("Couldn't find the location list on the reschedule page. Use DevTools -> Network instead:\n"
              "pick Astana and read the number in .../appointment/days/<ID>.json")
        return 1
    print(f"AIS_SCHEDULE_ID={cfg.schedule_id}")
    for select_id, options in found:
        print(f"{select_id}:")
        for value, name in options:
            print(f"  {value:>6}  {name}")
    print("Put the Astana number into AIS_FACILITY_ID.")
    return 0


def cmd_find_chat_id(cfg: Config) -> int:
    if not cfg.bot_token:
        raise SystemExit("Put TELEGRAM_BOT_TOKEN in visa_watch.env first.")
    try:
        with urllib.request.urlopen(f"{cfg.telegram_api}/bot{cfg.bot_token}/getUpdates", timeout=20) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Telegram said HTTP {e.code} - check the token.")
    except Exception as e:
        raise SystemExit(f"Telegram request failed: {type(e).__name__}")
    chats = {}
    for update in data.get("result", []):
        chat = (update.get("message") or update.get("channel_post") or {}).get("chat") or {}
        if "id" in chat:
            chats[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name") or ""
    if not chats:
        print("No messages yet. Send your bot any message in Telegram, then run this again.")
    for chat_id, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={chat_id}    ({name})")
    return 0


def cmd_status(cfg: Config) -> int:
    state = load_json(cfg.state_dir / "state.json", {})
    cookies = load_json(cfg.state_dir / "cookies.json", {})
    now = utcnow()
    cur = current_earliest(state)
    last = parse_ts(state.get("last_check_at"))
    print(f"Location:   {cfg.label} (facility {cfg.facility_id or '?'}, schedule {cfg.schedule_id or '?'})")
    print(f"Earliest:   {fmt_day(cur, local(now, cfg).date()) if cur else 'none / unknown'}")
    print(f"Last check: {fmt_clock(last, cfg) if last else 'never'}")
    print(f"Session:    {'cookie saved' if cookies.get('_yatri_session') else 'no cookie'}; {session_line(cfg, state, now)}")
    if state.get("consecutive_errors"):
        print(f"Errors:     {state['consecutive_errors']} in a row, last: {state.get('last_error')}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Watch the earliest US visa appointment date and report to Telegram.")
    p.add_argument("--config", type=Path,
                   default=Path(os.environ.get("VISA_WATCH_CONFIG", SCRIPT_DIR / "visa_watch.env")),
                   help="settings file (default: visa_watch.env next to this script)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--set-cookie", nargs="?", const="", metavar="VALUE",
                      help="save a fresh _yatri_session cookie (prompts if VALUE is omitted) and test it")
    mode.add_argument("--discover", action="store_true", help="list schedule ids and location ids for your account")
    mode.add_argument("--find-chat-id", action="store_true", help="show the chat id of whoever messaged your bot")
    mode.add_argument("--test-telegram", action="store_true", help="send a test message")
    mode.add_argument("--status", action="store_true", help="print the current state")
    p.add_argument("--force", action="store_true", help="ignore MIN_INTERVAL_MINUTES and any back-off")
    p.add_argument("--dry-run", action="store_true", help="check, but print messages instead of sending them")
    args = p.parse_args(argv)
    cfg = load_config(args.config)

    if args.set_cookie is not None:
        return cmd_set_cookie(cfg, args.set_cookie)
    if args.discover:
        return cmd_discover(cfg)
    if args.find_chat_id:
        return cmd_find_chat_id(cfg)
    if args.test_telegram:
        ok = send_telegram(cfg, "✅ Test message from visa_watch - notifications work.")
        print("Sent." if ok else "Failed - check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        return 0 if ok else 1
    if args.status:
        return cmd_status(cfg)
    return run_check(cfg, force=args.force, dry_run=args.dry_run)[0]


if __name__ == "__main__":
    sys.exit(main())
