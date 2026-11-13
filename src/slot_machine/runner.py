"""One scheduled check: lock, load state, maybe fetch, decide, send, save. And the pings
that stand in for checks while the visa site looks down (sitewatch)."""

from __future__ import annotations

import dataclasses
from datetime import timedelta

from . import sitewatch, telegram, timeutil
from .ais import FetchResult, fetch_dates
from .config import Config
from .store import BOT_FILE, load_json, save_json, single_instance
from .timeutil import fmt_dur, fmt_until, iso, log, parse_ts
from .watcher import WELCOMES, Msg, process, process_ping, record_expiry_told, unmark_welcomed


def starts_pause(via: str) -> bool:
    """Whether an empty answer from a check started this way pauses scheduled checks.
    A Telegram /check never starts or extends the pause: anyone listed may send it, and
    a quick look shouldn't hold off the schedule. Cron, check-now, manual, set-cookie and
    a renewal do, as before (a local --force is the owner choosing to)."""
    return not via.startswith("telegram:")


def session_cfg(cfg: Config, state: dict) -> Config:
    """cfg with the User-Agent of the browser the current session came from, if known."""
    ua = state.get("session_user_agent")
    return dataclasses.replace(cfg, user_agent=ua) if ua else cfg


def deliver(cfg: Config, state: dict, msgs: list[Msg], dry_run: bool = False) -> None:
    """Send each message to its chats; roll its state back if it reached nobody."""
    for m in msgs:
        targets = cfg.chat_ids if m.to is None else m.to
        if dry_run:
            to = ", ".join(targets) or "nobody"
            print(f"--- would send ({m.kind}{', silent' if m.silent else ''}) to {to} ---\n{m.text}\n")
            continue
        failed = [c for c in targets if not telegram.send_message(cfg, m.text, m.silent, chat_id=c)]
        if not targets:
            log("telegram not configured; message not sent")
        if len(failed) == len(targets):
            state.update(m.rollback)  # reached nobody: try again on the next run
        if m.kind == "expired":
            record_expiry_told(state, [c for c in targets if c not in failed])
        if m.kind in WELCOMES:
            for chat in failed:  # a chat that missed its welcome gets one next run
                unmark_welcomed(state, chat)


def ping_site(cfg: Config, state: dict, now, via: str) -> list[Msg]:
    """One ping of the home page while the site looks down. Logged when something changes,
    not for every ping while it stays down."""
    before = state["site"]["state"]
    url = sitewatch.home_url(cfg.ais_base, cfg.locale)
    answered, detail = sitewatch.ping(url, session_cfg(cfg, state).user_agent)
    msgs = process_ping(state, answered, detail, now, cfg)
    site = state.get("site")
    every = fmt_dur(sitewatch.PING_EVERY.total_seconds())
    if site is None:
        log(f"ping: {detail}; the visa site answers, checks resume [{via}]")
    elif site["state"] != before:
        log(f"ping: {detail}; the visa site is down, pinging every {every} until it answers [{via}]")
    elif site["state"] == "checking":
        log(f"ping {site['pings']}/{sitewatch.CONFIRM_PINGS}: {detail} [{via}]")
    return msgs


def run_ping(cfg: Config, via: str = "cron") -> int:
    """Cron runs this every minute. While the site looks down it pings it when a ping is due;
    otherwise (almost always) it returns at once, without a request or a log line."""
    state_path = cfg.state_dir / "state.json"
    with single_instance(cfg.state_dir) as got_lock:
        if not got_lock:
            return 0  # a check is running; the next minute will do
        state = load_json(state_path, {})
        now = timeutil.utcnow()
        if not sitewatch.ping_due(state.get("site"), now):
            return 0
        deliver(cfg, state, ping_site(cfg, state, now, via))
        save_json(state_path, state)
        return 0


def run_check(
    cfg: Config,
    force: bool = False,
    dry_run: bool = False,
    new_cookies: dict | None = None,
    via: str = "manual",
    new_user_agent: str | None = None,
):
    """Return (exit code, FetchResult or None if the run was skipped before deciding).

    `via` says what started the run (cron, check-now, telegram:<chat>, ...); it ends
    every log line so watch.log shows who triggered each check. `new_user_agent` is the
    browser the new cookies came from (the extension and the renewal page send it); None
    means the USER_AGENT setting (make set-cookie). Checks with that session use it, since
    the site rejects a session sent from a different browser version.

    New cookies are tried first and saved only if the site accepts them (or, when no
    working session would be lost, if the check fails for another reason). A rejected
    one changes nothing and returns (1, the rejected result) without any message."""
    state_path, cookie_path = cfg.state_dir / "state.json", cfg.state_dir / "cookies.json"
    with single_instance(cfg.state_dir) as got_lock:
        if not got_lock:
            log(f"another run is in progress; skipping [{via}]")
            return 0, None
        state = load_json(state_path, {})
        now = timeutil.utcnow()
        cookies = load_json(cookie_path, {})
        res = None
        if new_cookies is not None:
            # Try the new session before saving it: one that doesn't work must never replace
            # one that does (an anonymous cookie, sent before signing in, is rejected).
            if cfg.schedule_id and cfg.facility_id:
                res = fetch_dates(
                    dataclasses.replace(cfg, user_agent=new_user_agent or cfg.user_agent), new_cookies
                )
            current_works = bool(cookies.get("_yatri_session")) and not state.get("session_expired_at")
            if res and (res.kind == "expired" or (res.kind not in ("ok", "empty") and current_works)):
                kept = "kept the current one" if current_works else "the session stays ended"
                log(f"new session not saved ({f'{res.kind} {res.detail}'.strip()}); {kept} [{via}]")
                return 1, res
            # session_expired_at is only set once the "session ended" alert reached a chat
            # (it's rolled back otherwise), so this is a renewal the bot asked for. Keep the
            # first time it asked, in case this session fails later and another one follows.
            if state.get("session_expired_at") and not state.get("renew_requested_at"):
                state["renew_requested_at"] = state["session_expired_at"]
            state.update(
                session_user_agent=new_user_agent,
                session_set_at=iso(now),
                session_expired_at=None,
                backoff_until=None,
                consecutive_errors=0,
                error_alert_sent=False,
            )
            cookies = new_cookies
            save_json(cookie_path, cookies)

        last = parse_ts(state.get("last_check_at"))
        if res is None and not force and last and now - last < timedelta(minutes=cfg.min_interval_minutes):
            log(
                f"last check was {fmt_dur((now - last).total_seconds())} ago (< MIN_INTERVAL_MINUTES); skipping [{via}]"
            )
            return 0, None

        site_msgs = []
        if res is None and state.get("site") and not force:
            # the site looked down: pings stand in for checks until one gets an answer
            if sitewatch.ping_due(state["site"], now):
                site_msgs = ping_site(cfg, state, now, via)
            if state.get("site"):
                res = FetchResult("skipped", detail=f"visa site {state['site']['state']}; pinging instead")

        backoff = parse_ts(state.get("backoff_until"))
        if res is not None:
            pass  # the new session's trial was this run's check
        elif not (cfg.schedule_id and cfg.facility_id):
            res = FetchResult("skipped", detail="AIS_SCHEDULE_ID / AIS_FACILITY_ID not set")
        elif not cookies.get("_yatri_session"):
            res = FetchResult("skipped", detail="no session cookie yet (run --set-cookie)")
        elif state.get("session_expired_at"):
            res = FetchResult("skipped", detail="session expired (run --set-cookie)")
        elif backoff and now < backoff and not force:
            res = FetchResult("skipped", detail=f"backing off until {fmt_until(backoff, now, cfg)}")
        else:
            res = fetch_dates(session_cfg(cfg, state), cookies)

        if res.cookies is not None and res.cookies != cookies:
            save_json(cookie_path, res.cookies)
        alert_before = load_json(cfg.state_dir / BOT_FILE, {}).get("alert_before", {})
        msgs = process(state, res, now, cfg, alert_before, pause_on_empty=starts_pause(via))
        deliver(cfg, state, site_msgs + msgs, dry_run)
        if not dry_run:
            save_json(state_path, state)
        line = f"check: {res.kind} {res.dates[0] if res.dates else ''} {res.detail}".rstrip()
        if res.kind == "empty":
            backoff = parse_ts(state.get("backoff_until"))
            if starts_pause(via):
                line += f"; scheduled checks pause until {fmt_until(backoff, now, cfg)}"
            elif backoff and backoff > now:
                line += f"; the pause until {fmt_until(backoff, now, cfg)} stays as it was"
            else:
                line += "; no pause from a Telegram /check"
        log(f"{line} [{via}]")
        return (0 if res.kind in ("ok", "empty", "skipped") else 1), res
