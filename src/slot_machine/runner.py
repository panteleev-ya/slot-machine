"""One scheduled check: lock, load state, maybe fetch, decide, send, save."""

from __future__ import annotations

from datetime import timedelta

from . import telegram, timeutil
from .ais import FetchResult, fetch_dates
from .config import Config
from .store import BOT_FILE, load_json, save_json, single_instance
from .timeutil import fmt_clock, fmt_dur, iso, log, parse_ts
from .watcher import WELCOMES, process, record_expiry_told, unmark_welcomed


def run_check(
    cfg: Config,
    force: bool = False,
    dry_run: bool = False,
    new_cookies: dict | None = None,
    via: str = "manual",
):
    """Return (exit code, FetchResult or None if the run was skipped before deciding).

    `via` says what started the run (cron, check-now, telegram:<chat>, ...); it ends
    every log line so watch.log shows who triggered each check."""
    state_path, cookie_path = cfg.state_dir / "state.json", cfg.state_dir / "cookies.json"
    with single_instance(cfg.state_dir) as got_lock:
        if not got_lock:
            log(f"another run is in progress; skipping [{via}]")
            return 0, None
        state = load_json(state_path, {})
        now = timeutil.utcnow()
        if new_cookies is not None:
            save_json(cookie_path, new_cookies)
            # session_expired_at is only set once the "session ended" alert reached a chat
            # (it's rolled back otherwise), so this is a renewal the bot asked for. Keep the
            # first time it asked, in case a renewal attempt fails and another one follows.
            if state.get("session_expired_at") and not state.get("renew_requested_at"):
                state["renew_requested_at"] = state["session_expired_at"]
            state.update(
                session_set_at=iso(now),
                session_expired_at=None,
                backoff_until=None,
                consecutive_errors=0,
                error_alert_sent=False,
            )
        cookies = load_json(cookie_path, {})

        last = parse_ts(state.get("last_check_at"))
        if not force and last and now - last < timedelta(minutes=cfg.min_interval_minutes):
            log(
                f"last check was {fmt_dur((now - last).total_seconds())} ago (< MIN_INTERVAL_MINUTES); skipping [{via}]"
            )
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
        alert_before = load_json(cfg.state_dir / BOT_FILE, {}).get("alert_before", {})
        for m in process(state, res, now, cfg, alert_before):
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
        if not dry_run:
            save_json(state_path, state)
        log(f"check: {res.kind} {res.dates[0] if res.dates else ''} {res.detail}".rstrip() + f" [{via}]")
        return (0 if res.kind in ("ok", "empty", "skipped") else 1), res
