"""The text of every Telegram message (HTML parse mode)."""

from __future__ import annotations

import html
from datetime import date, datetime, timedelta

from .config import Config
from .renew_link import renew_link
from .sitewatch import PING_EVERY
from .timeutil import days_between, fmt_clock, fmt_date, fmt_day, fmt_dur, fmt_short, parse_ts, plural


def esc(value) -> str:
    return html.escape(str(value), quote=False)


def session_line(cfg: Config, state: dict, now: datetime) -> str:
    expired = parse_ts(state.get("session_expired_at"))
    if expired:
        return f"EXPIRED since {fmt_clock(expired, cfg)} - sign in again to renew it"
    set_at = parse_ts(state.get("session_set_at"))
    line = f"active for {fmt_dur((now - set_at).total_seconds())}" if set_at else "active"
    backoff = parse_ts(state.get("backoff_until"))
    if backoff and backoff > now:
        line += f"; pausing until {fmt_clock(backoff, cfg)} (site went quiet or throttled)"
    return line


def _rules(cfg: Config) -> str:
    rule = "You'll get an alert when a sooner date opens"
    if cfg.alert_only_before:
        rule += f" before {fmt_short(cfg.alert_only_before)}"
    return rule + f", plus a quiet summary daily around {cfg.digest_hour:02d}:00."


def text_baseline(cfg: Config, earliest: str | None, today: date) -> str:
    first = (
        f"Earliest date now: {fmt_day(earliest, today)}"
        if earliest
        else "No dates are offered right now (or the site is throttling); you'll hear when that changes."
    )
    return "\n".join([f"<b>✅ Visa slot watch started - {esc(cfg.label)}</b>", first, _rules(cfg)])


def text_welcome(cfg: Config, state: dict, cur: str | None, now: datetime, today: date) -> str:
    """For a chat added to TELEGRAM_CHAT_ID after the watch started."""
    seen_at = parse_ts(state.get("last_seen_at"))
    if cur and seen_at:
        status = f"Earliest date at the last check ({fmt_clock(seen_at, cfg)}): {fmt_day(cur, today)}"
    elif state.get("last_empty_at"):
        status = "No dates were offered at the last check."
    else:
        status = "No successful check yet."
    return "\n".join(
        [
            f"<b>👋 You're subscribed to the visa slot watch - {esc(cfg.label)}</b>",
            "It watches the earliest open US visa appointment date.",
            status,
            _rules(cfg),
        ]
    )


def text_sooner(cfg: Config, earliest: str, prev: str | None, today: date, cutoff: str = "") -> str:
    """`cutoff` is the chat's alert-before date, its current appointment: the date compared
    against. Without one, the comparison is with the previous check. With one, the earliest
    date may also have got later (the one before it is gone) while still beating the cutoff."""
    later = bool(prev) and earliest > prev
    if later:
        title = "🟡 Earliest visa date moved later, still before your appointment"
    else:
        title = "🟢 Sooner visa date opened" if prev else "🟢 Visa dates available again"
    lines = [f"<b>{title} - {esc(cfg.label)}</b>", f"Earliest: {fmt_day(earliest, today)}"]
    if cutoff:
        gain = plural(days_between(earliest, cutoff), "day")
        lines.append(f"{gain} before your current appointment, {fmt_date(cutoff)}.")
        if later:
            lines.append(f"{fmt_short(prev)} is no longer open.")
    elif prev:
        lines.append(
            f"Was {fmt_short(prev)} at the previous check: {plural(days_between(earliest, prev), 'day')} sooner."
        )
    else:
        lines.append("Earlier checks found no dates at all.")
    lines.append(f"Slots go fast: {esc(cfg.appointment_url)}")
    return "\n".join(lines)


def text_expired(cfg: Config, state: dict) -> str:
    set_at, ok_at = parse_ts(state.get("session_set_at")), parse_ts(state.get("session_last_ok_at"))
    lines = ["<b>🔑 Visa slot watch paused - the AIS session ended</b>"]
    if set_at and ok_at and ok_at >= set_at:
        lines.append(f"That session lasted {fmt_dur((ok_at - set_at).total_seconds())}.")
    link = renew_link(cfg, state)
    if link:
        lines += [
            f"To resume: sign in at {esc(cfg.sign_in_url)}, then click the slot-machine extension in Chrome,",
            f'or paste the cookie on the <a href="{html.escape(link, quote=True)}">renewal page</a> (home Wi-Fi).',
        ]
    else:
        lines += [
            f"To resume: sign in at {esc(cfg.sign_in_url)}, copy the _yatri_session cookie, then run",
            "<code>make set-cookie</code>",
        ]
    lines.append("No requests are sent until then.")
    return "\n".join(lines)


def text_renewed(
    cfg: Config, earliest: str | None, since: datetime | None, now: datetime, today: date
) -> str:
    """To the chats that heard the session ended, once a renewed session works."""
    paused = f" Checks were paused for {fmt_dur((now - since).total_seconds())}." if since else ""
    first = (
        f"Earliest date now: {fmt_day(earliest, today)}"
        if earliest
        else "No dates came back at the first check (none open, or the site is throttling)."
    )
    return "\n".join(
        [
            f"<b>🔓 Visa slot watch resumed - {esc(cfg.label)}</b>",
            f"The new session works; checks continue on schedule.{paused}",
            first,
        ]
    )


def text_error(cfg: Config, state: dict) -> str:
    return "\n".join(
        [
            f"<b>⚠️ Visa slot watch: {state.get('consecutive_errors', 0)} checks in a row failed</b>",
            f"Last error: {esc(state.get('last_error', 'unknown'))}",
            "It keeps trying; the daily summary shows when it recovers.",
        ]
    )


def _every() -> str:
    return fmt_dur(PING_EVERY.total_seconds())


def text_site_down(cfg: Config, site: dict) -> str:
    """To the owner, once the pings after a failed check got no answer either (sitewatch)."""
    since, down_at = parse_ts(site["since"]), parse_ts(site.get("down_at")) or parse_ts(site["since"])
    pings = plural(site.get("pings", 0), "ping")
    return "\n".join(
        [
            f"<b>🔴 Visa site down - {esc(cfg.label)}</b>",
            f"No answer since {fmt_clock(since, cfg)}: a check failed, then {pings} of the home page "
            f"over {fmt_dur((down_at - since).total_seconds())} got none.",
            f"Last error: {esc(site.get('last_error', 'unknown'))}",
            f"Date checks are paused. The Pi pings the home page every {_every()} and tells you when it "
            "answers; the sign-in loop does the same and doesn't sign in meanwhile.",
        ]
    )


def text_site_up(cfg: Config, site: dict, now: datetime) -> str:
    """To the owner, when the site answers again after a "site down" message."""
    since = parse_ts(site["since"])
    return "\n".join(
        [
            f"<b>✅ Visa site back up - {esc(cfg.label)}</b>",
            f"It didn't answer for {fmt_dur((now - since).total_seconds())} (since {fmt_clock(since, cfg)}).",
            "Date checks resume on schedule.",
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
    site = state.get("site")
    if site:
        how = "down" if site.get("state") == "down" else "not answering"
        lines.append(f"Visa site: {how} since {fmt_clock(parse_ts(site['since']), cfg)}")
    return "\n".join(lines)
