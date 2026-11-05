"""The text of every Telegram message (HTML parse mode)."""

from __future__ import annotations

import html
from datetime import date, datetime, timedelta

from .config import Config
from .timeutil import days_between, fmt_clock, fmt_day, fmt_dur, fmt_short, parse_ts, plural


def esc(value) -> str:
    return html.escape(str(value), quote=False)


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
    first = (
        f"Earliest date now: {fmt_day(earliest, today)}"
        if earliest
        else "No dates are offered right now (or the site is throttling); you'll hear when that changes."
    )
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
    return "\n".join(
        [
            f"<b>🟢 {title} - {esc(cfg.label)}</b>",
            f"Earliest: {fmt_day(earliest, today)}",
            change,
            f"Slots go fast: {esc(cfg.appointment_url)}",
        ]
    )


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
    return "\n".join(
        [
            f"<b>⚠️ Visa slot watch: {state.get('consecutive_errors', 0)} checks in a row failed</b>",
            f"Last error: {esc(state.get('last_error', 'unknown'))}",
            "It keeps trying; the daily summary shows when it recovers.",
        ]
    )


def text_digest(cfg: Config, state: dict, cur: str | None, now: datetime, today: date) -> str:
    """`cur` is the current earliest date (None if the newest good answer had none)."""
    lines = [f"<b>📋 Daily visa slot summary - {esc(cfg.label)}</b>"]
    last_good = parse_ts(state.get("session_last_ok_at"))
    if cur:
        stale = state.get("session_expired_at") or last_good is None or now - last_good > timedelta(hours=2)
        lead = (
            f"Last known earliest (as of {fmt_clock(parse_ts(state['last_seen_at']), cfg)})"
            if stale
            else "Earliest now"
        )
        lines.append(f"{lead}: {fmt_day(cur, today)}")
    elif state.get("last_empty_at"):
        lines.append(
            f"No dates offered at the last good check ({fmt_clock(parse_ts(state['last_empty_at']), cfg)})."
        )
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
        lines.append(
            f"Best seen in between: {fmt_short(best['date'])} at {fmt_clock(parse_ts(best['at']), cfg)} (gone since)"
        )
    c = state.get("counts") or {}
    lines.append(f"Checks: {c.get('ok', 0)} ok · {c.get('empty', 0)} empty · {c.get('failed', 0)} failed")
    lines.append(f"Session: {session_line(cfg, state, now)}")
    return "\n".join(lines)
