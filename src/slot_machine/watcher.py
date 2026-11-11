"""The decision logic: fold one check result into the saved state and decide
which messages to send. No network, no files, no clock: everything comes in
as arguments, so every rule here is easy to test."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import messages, sitewatch
from .ais import FetchResult
from .config import Config
from .timeutil import iso, local, parse_ts

# Empty answers for this long mean "really no dates", not a temporary throttle.
EMPTY_IS_REAL_AFTER = timedelta(hours=24)


@dataclass
class Msg:
    kind: str  # baseline | welcome | sooner | expired | renewed | error | down | up | digest
    text: str
    silent: bool = False
    rollback: dict = field(default_factory=dict)  # state to restore if it reached nobody
    to: list[str] | None = None  # chat ids; None = every configured chat


# "error", "down" and "up" go to the owner (first chat) only: fixing it needs the Mac or the Pi,
# and an outage is the owner's to know about. Everything else
# goes to every chat, "session ended" included, since anyone listed can renew the session; "watch
# resumed" goes to the chats that heard it ended. Kinds that count as a chat's welcome: a chat
# they didn't reach gets a welcome later.
WELCOMES = ("baseline", "welcome")


# state key: the chats a "session ended" alert reached since the session last worked
EXPIRY_TOLD = "expiry_told_chats"


def record_expiry_told(state: dict, chats: list[str]) -> None:
    """The runner calls this with the chats an "expired" alert reached."""
    known = state.get(EXPIRY_TOLD) or []
    state[EXPIRY_TOLD] = known + [c for c in chats if c not in known]


def unmark_welcomed(state: dict, chat: str) -> None:
    """The runner calls this for a chat a baseline/welcome failed to reach."""
    state["welcomed_chats"] = [c for c in state.get("welcomed_chats") or [] if c != chat]


def welcomed_chats(state: dict, cfg: Config) -> list[str]:
    """Configured chats that already got their baseline or welcome."""
    welcomed = state.get("welcomed_chats")
    if welcomed is None:
        # state from before chat lists: the baseline went to the one chat there was, the first
        welcomed = [cfg.owner_chat] if cfg.owner_chat else []
    return [c for c in welcomed if c in cfg.chat_ids]  # removed chats: forgotten


def pending_welcomes(state: dict, cfg: Config) -> list[str]:
    if not state.get("baseline_sent"):
        return []  # the baseline will greet everyone
    welcomed = welcomed_chats(state, cfg)
    return [c for c in cfg.chat_ids if c not in welcomed]


def _welcome_new_chats(state: dict, cfg: Config, now: datetime, today) -> list[Msg]:
    pending = pending_welcomes(state, cfg)
    text = messages.text_welcome(cfg, state, current_earliest(state), now, today)
    state["welcomed_chats"] = welcomed_chats(state, cfg) + pending
    return [Msg("welcome", text, to=[chat]) for chat in pending]


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


# --------------------------------------------------------------------------- site up or down
# state["site"] is the sitewatch record while the site looks down ("checking", "down"), plus
# "told": the owner got the "down" message. While it's there, runs ping instead of checking.


def _site_answered(state: dict, cfg: Config, now: datetime) -> list[Msg]:
    """The site answered (a check or a ping): it's up. The owner hears it if they heard it was
    down. "site_up_unsent" is such a message that reached nobody: it goes with this answer."""
    site = state.pop("site", None) or state.pop("site_up_unsent", None)
    if not (site and site.get("told")):
        return []  # a blip nobody was told about
    text = messages.text_site_up(cfg, site, now)
    return [Msg("up", text, rollback={"site_up_unsent": site}, to=cfg.chat_ids[:1])]


def process_ping(state: dict, answered: bool, detail: str, now: datetime, cfg: Config) -> list[Msg]:
    """Fold one ping of the site's home page into `state` and return the messages to send."""
    site = state.get("site")
    if not site:
        return []
    if answered:
        return _site_answered(state, cfg, now)
    site = state["site"] = sitewatch.pinged(site, False, detail, now)
    if site["state"] != "down" or site.get("told"):
        return []
    site["told"] = True
    text = messages.text_site_down(cfg, site)
    return [Msg("down", text, rollback={"site": {**site, "told": False}}, to=cfg.chat_ids[:1])]


def alert_cutoff(cfg: Config, overrides: dict, chat: str) -> str:
    """The date a chat's sooner-date alerts must beat ("" = any sooner date): its own
    /set_alert_before value if it set one, else ALERT_ONLY_BEFORE from the settings."""
    own = overrides.get(chat)
    return cfg.alert_only_before if own is None else own


def process(
    state: dict, res: FetchResult, now: datetime, cfg: Config, alert_before: dict | None = None
) -> list[Msg]:
    """Fold one check result into `state` and return the messages to send.
    `alert_before` maps chat id -> its own cutoff ("" = none), set with /set_alert_before."""
    alert_before = alert_before or {}
    msgs = []
    today = local(now, cfg).date()
    counts = state.setdefault("counts", {"ok": 0, "empty": 0, "failed": 0})

    if res.kind != "skipped":
        state["last_check_at"] = iso(now)
    if res.kind in ("ok", "empty"):
        state.update(consecutive_errors=0, error_alert_sent=False, session_last_ok_at=iso(now))
    if res.kind not in ("down", "skipped"):
        msgs += _site_answered(state, cfg, now)  # dates, a sign-in page, an error: it's there

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
                    rollback=snapshot(
                        state, "baseline_sent", "last_digest_date", "last_digest_earliest", "welcomed_chats"
                    ),
                )
            )
            state.update(
                baseline_sent=True, last_digest_date=today.isoformat(), last_digest_earliest=earliest
            )
            state["welcomed_chats"] = list(cfg.chat_ids)  # the baseline is everyone's welcome
        elif earliest != prev:
            # A chat with a cutoff (its current appointment) hears about every change of the
            # earliest date while it's before that cutoff, later ones included: if the date
            # it was told about got booked, the next one may be just as worth taking. A chat
            # without one hears only about dates sooner than at the previous good check. Either
            # way a slot that vanishes and comes back alerts again: each reappearance is a new
            # chance. Chats with the same cutoff share one message (the text names it).
            by_cutoff: dict[str, list[str]] = {}
            for c in cfg.chat_ids:
                cutoff = alert_cutoff(cfg, alert_before, c)
                wanted = earliest < cutoff if cutoff else (prev is None or earliest < prev)
                if wanted:
                    by_cutoff.setdefault(cutoff, []).append(c)
            for cutoff, to in by_cutoff.items():
                text = messages.text_sooner(cfg, earliest, prev, today, cutoff)
                msgs.append(Msg("sooner", text, rollback={"last_seen": prev}, to=to))
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
                    rollback=snapshot(
                        state, "baseline_sent", "last_digest_date", "last_digest_earliest", "welcomed_chats"
                    ),
                )
            )
            state.update(baseline_sent=True, last_digest_date=today.isoformat(), last_digest_earliest=None)
            state["welcomed_chats"] = list(cfg.chat_ids)

    elif res.kind == "expired" and state.get("renew_requested_at"):
        # a renewal attempt the site rejected: the chats already know the session ended, and
        # whoever sent it sees the answer, so stay quiet. Keep the original end time, so the
        # renewal link in that alert still works.
        state["session_expired_at"] = state["renew_requested_at"]

    elif res.kind == "expired":
        state["session_expired_at"] = iso(now)
        msgs.append(
            Msg(
                "expired",
                messages.text_expired(cfg, state),
                rollback={"session_expired_at": None},
            )
        )

    elif res.kind == "down":
        # pings decide whether it's down (process_ping); "checks failing" doesn't count these
        counts["failed"] += 1
        unsent = state.pop("site_up_unsent", None)
        site = sitewatch.failed(state.get("site"), res.detail, now)
        if unsent and not state.get("site"):
            site.update(since=unsent["since"], told=True)  # to the owner it's the same outage
        state["site"] = site

    elif res.kind in ("throttled", "error"):
        counts["failed"] += 1
        state["consecutive_errors"] = state.get("consecutive_errors", 0) + 1
        state["last_error"] = res.detail
        if res.kind == "throttled":
            state["backoff_until"] = iso(now + timedelta(hours=cfg.backoff_hours))
        if state["consecutive_errors"] >= cfg.error_alert_after and not state.get("error_alert_sent"):
            state["error_alert_sent"] = True
            msgs.append(
                Msg(
                    "error",
                    messages.text_error(cfg, state),
                    rollback={"error_alert_sent": False},
                    to=cfg.chat_ids[:1],
                )
            )

    if res.kind in ("ok", "empty") and state.get("renew_requested_at"):
        # the chats were asked to renew and the new session works: tell them it's done, so
        # nobody else signs in for nothing
        rollback = snapshot(state, "renew_requested_at", EXPIRY_TOLD)
        since = state.pop("renew_requested_at")
        told = state.pop(EXPIRY_TOLD, None)
        if told is None:
            told = cfg.chat_ids[:1]  # state from when only the owner heard about expiries
        to = [c for c in cfg.chat_ids if c in told]
        if to:
            earliest = res.dates[0] if res.kind == "ok" else None
            text = messages.text_renewed(cfg, earliest, parse_ts(since), now, today)
            msgs.append(Msg("renewed", text, rollback=rollback, to=to))

    if state.get("baseline_sent"):
        msgs += _welcome_new_chats(state, cfg, now, today)

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
