"""Clock, time zones, date/duration formatting, and timestamped logging."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

if TYPE_CHECKING:
    from .config import Config

DOW = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MON = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def utcnow() -> datetime:
    """The one clock everything reads (tests replace it)."""
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


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


def fmt_date(iso_date: str) -> str:
    """ "Wed May 24, 2028": fmt_day without the countdown."""
    d = date.fromisoformat(iso_date)
    return f"{DOW[d.weekday()]} {fmt_short(iso_date)}"


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


def fmt_until(dt: datetime, now: datetime, cfg: Config) -> str:
    """When `dt` comes, seen from `now`: "15:20 (3h 0m)", with the date if it's another
    local day: "Nov 13 02:10 (3h 0m)"."""
    clock = (
        f"{local(dt, cfg):%H:%M}" if local(dt, cfg).date() == local(now, cfg).date() else fmt_clock(dt, cfg)
    )
    # to the nearest minute: a pause started a second ago reads "3h 0m", not "2h 59m"
    return f"{clock} ({fmt_dur(round((dt - now).total_seconds() / 60) * 60)})"


def days_between(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days
