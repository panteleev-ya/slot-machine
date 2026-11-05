"""The decision logic: fold one check result into the saved state and decide
which messages to send. No network, no files, no clock: everything comes in
as arguments, so every rule here is easy to test."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import messages
from .ais import FetchResult
from .config import Config
from .timeutil import iso, local, parse_ts

# Empty answers for this long mean "really no dates", not a temporary throttle.
EMPTY_IS_REAL_AFTER = timedelta(hours=24)


@dataclass
class Msg:
    kind: str  # baseline | sooner | expired | error | digest
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


def process(state: dict, res: FetchResult, now: datetime, cfg: Config) -> list[Msg]:
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
            msgs.append(
                Msg(
                    "baseline",
                    messages.text_baseline(cfg, earliest, today),
                    rollback=snapshot(state, "baseline_sent", "last_digest_date", "last_digest_earliest"),
                )
            )
            state.update(
                baseline_sent=True, last_digest_date=today.isoformat(), last_digest_earliest=earliest
            )
        elif (prev is None or earliest < prev) and (
            not cfg.alert_only_before or earliest < cfg.alert_only_before
        ):
            # "Sooner" means sooner than at the previous good check, so a slot that
            # vanishes and comes back alerts again - each reappearance is a new chance.
            msgs.append(
                Msg("sooner", messages.text_sooner(cfg, earliest, prev, today), rollback={"last_seen": prev})
            )
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
            msgs.append(
                Msg(
                    "baseline",
                    messages.text_baseline(cfg, None, today),
                    rollback=snapshot(state, "baseline_sent", "last_digest_date", "last_digest_earliest"),
                )
            )
            state.update(baseline_sent=True, last_digest_date=today.isoformat(), last_digest_earliest=None)

    elif res.kind == "expired":
        state["session_expired_at"] = iso(now)
        msgs.append(Msg("expired", messages.text_expired(cfg, state), rollback={"session_expired_at": None}))

    elif res.kind in ("throttled", "error"):
        counts["failed"] += 1
        state["consecutive_errors"] = state.get("consecutive_errors", 0) + 1
        state["last_error"] = res.detail
        if res.kind == "throttled":
            state["backoff_until"] = iso(now + timedelta(hours=cfg.backoff_hours))
        if state["consecutive_errors"] >= cfg.error_alert_after and not state.get("error_alert_sent"):
            state["error_alert_sent"] = True
            msgs.append(Msg("error", messages.text_error(cfg, state), rollback={"error_alert_sent": False}))

    if (
        state.get("baseline_sent")
        and local(now, cfg).hour >= cfg.digest_hour
        and state.get("last_digest_date") != today.isoformat()
    ):
        rollback = snapshot(state, "last_digest_date", "last_digest_earliest", "best_since_digest", "counts")
        text = messages.text_digest(cfg, state, current_earliest(state), now, today)
        msgs.append(Msg("digest", text, silent=cfg.digest_silent, rollback=rollback))
        state.update(
            last_digest_date=today.isoformat(),
            last_digest_earliest=current_earliest(state),
            best_since_digest=None,
            counts={"ok": 0, "empty": 0, "failed": 0},
        )
    return msgs
