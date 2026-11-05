"""Telegram commands from the chats in TELEGRAM_CHAT_ID: /check runs a check now, /help.

Cron starts `slot-machine --poll-telegram` every minute. It long-polls Telegram for
about a minute, answers commands as they arrive, and exits, so there is no daemon to
manage and a crash costs at most one minute. Chats not in TELEGRAM_CHAT_ID never get
an answer; their ids are kept (telegram.json) so --find-chat-id can still show them,
since the poller consumes the bot's messages.
"""

from __future__ import annotations

import html
import time
from datetime import date, timedelta

from . import telegram, timeutil
from .ais import FetchResult
from .config import Config
from .messages import esc, session_line
from .renew_link import renew_link
from .runner import run_check
from .store import BOT_FILE, load_json, save_json, single_instance  # BOT_FILE: offset, last /check,

# chats seen outside the list, and each chat's own alert cutoff (alert_before)
from .timeutil import DATE_RE, fmt_day, fmt_dur, fmt_short, iso, local, log, parse_ts
from .watcher import alert_cutoff, current_earliest

LOCK_NAME = ".telegram.lock"  # one poller at a time; separate from the check lock
LONG_POLL = 50  # seconds per getUpdates request
STALE_AFTER = timedelta(minutes=10)  # older commands (queued while offline) are ignored

HELP = (
    "<b>Visa slot watch</b>\n"
    "/check - check the earliest date right now\n"
    "/set_alert_before YYYY-MM-DD - alert me only about dates before this one\n"
    "/set_alert_before off - alert me about every sooner date\n"
    "/set_alert_before - show my setting\n"
    "/renew - (owner) link to renew the visa-site session\n"
    "Alerts about sooner dates and the daily summary arrive on their own."
)


def poll(cfg: Config, seconds: float = 55) -> int:
    """Answer commands for about `seconds`, then return. 0 seconds = one quick look."""
    if not (cfg.bot_token and cfg.chat_ids):
        log("telegram not configured; not polling")
        return 0
    with single_instance(cfg.state_dir, LOCK_NAME) as got_lock:
        if not got_lock:
            return 0  # the previous minute's poller is still answering
        path = cfg.state_dir / BOT_FILE
        bot = load_json(path, {})
        deadline = time.monotonic() + seconds
        while True:
            left = deadline - time.monotonic()
            updates = telegram.get_updates(cfg, bot.get("offset"), int(max(0, min(LONG_POLL, left - 5))))
            if updates is None and deadline - time.monotonic() > 10:
                time.sleep(5)  # Telegram unreachable: don't spin
            for update in updates or []:
                # move past the update before acting on it: a crash mid-check must not run it twice
                bot["offset"] = update["update_id"] + 1
                save_json(path, bot)
                handle(cfg, bot, update)
                save_json(path, bot)
            if deadline - time.monotonic() <= 5:
                return 0


def handle(cfg: Config, bot: dict, update: dict) -> None:
    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    text = (msg.get("text") or "").strip()
    if not chat_id:
        return
    if chat_id not in cfg.chat_ids:
        bot.setdefault("seen_chats", {})[chat_id] = telegram.chat_name(chat)
        log(f"telegram: message from {chat_id}, not in TELEGRAM_CHAT_ID; ignored")
        return
    if not text.startswith("/"):
        return  # chatter, not a command
    sent_at = msg.get("date")
    if sent_at and timeutil.utcnow().timestamp() - sent_at > STALE_AFTER.total_seconds():
        log(f"telegram: {text.split()[0]} from {chat_id} is too old to act on; ignored")
        return
    command, *args = text.split()
    command = command.split("@")[0].lower()  # "/check@SomeBot" -> "/check"
    if command == "/check":
        reply = check(cfg, bot, chat_id)
    elif command == "/set_alert_before":
        reply = set_alert_before(cfg, bot, chat_id, args)
    elif command == "/renew":
        reply = renew_reply(cfg, chat_id)
    else:
        reply = HELP  # /start, /help, anything unknown
    telegram.send_message(cfg, reply, chat_id=chat_id)


def check(cfg: Config, bot: dict, chat_id: str) -> str:
    now = timeutil.utcnow()
    last = parse_ts(bot.get("last_check_at"))
    cooldown = timedelta(minutes=cfg.check_cooldown_minutes)
    if last and now - last < cooldown:
        wait = fmt_dur((cooldown - (now - last)).total_seconds())
        log(f"telegram: /check from {chat_id} refused, cooldown")
        return f"A check ran {fmt_dur((now - last).total_seconds())} ago. Try again in {wait}."
    bot["last_check_at"] = iso(now)
    _, res = run_check(cfg, force=True, via=f"telegram:{chat_id}")
    return check_reply(cfg, res, now)


def check_reply(cfg: Config, res: FetchResult | None, now) -> str:
    if res is None:
        return "Another check is running right now. Its result arrives as the usual alerts."
    if res.kind == "ok":
        return (
            f"✅ Checked just now. Earliest date: {fmt_day(res.dates[0], local(now, cfg).date())}\n"
            "If it's sooner than before, everyone also gets the usual alert."
        )
    if res.kind == "empty":
        return (
            "No dates came back (none open, or the site is throttling). "
            f"Scheduled checks pause for {fmt_dur(cfg.backoff_hours * 3600)}."
        )
    if res.kind == "expired" or "session expired" in res.detail or "no session cookie" in res.detail:
        return "The visa-site session has ended, so nothing was checked. The owner needs to sign in again."
    if res.kind == "skipped":
        return f"Nothing was checked: {esc(res.detail)}."
    return f"⚠️ The check failed: {esc(res.detail)}"


def set_alert_before(cfg: Config, bot: dict, chat_id: str, args: list[str]) -> str:
    """This chat's own cutoff for sooner-date alerts. Stored per chat, so nobody changes
    what someone else gets; the daily summary is not affected."""
    own = bot.setdefault("alert_before", {})
    today = local(timeutil.utcnow(), cfg).date()
    if not args:
        current = alert_cutoff(cfg, own, chat_id)
        where = "" if chat_id in own else " (the default from the settings)"
        now = (
            f"You get alerts only for dates before {fmt_day(current, today)}{where}."
            if current
            else f"You get an alert for every sooner date{where}."
        )
        return f"{now}\nChange it: /set_alert_before YYYY-MM-DD, or /set_alert_before off"
    value = args[0].lower()
    if value in ("off", "none", "clear", "all"):
        own[chat_id] = ""
        log(f"telegram: {chat_id} set alert-before off")
        return "OK. You'll get an alert for every sooner date."
    try:
        if not DATE_RE.match(value):
            raise ValueError
        cutoff = date.fromisoformat(value)
    except ValueError:
        return f"That's not a date I can use: {esc(args[0])}. Use YYYY-MM-DD, like 2027-06-30."
    if cutoff <= today:
        return f"{fmt_short(value)} has already passed. Pick a future date, or /set_alert_before off."
    own[chat_id] = value
    log(f"telegram: {chat_id} set alert-before {value}")
    reply = f"OK. You'll get sooner-date alerts only for dates before {fmt_day(value, today)}."
    earliest = current_earliest(load_json(cfg.state_dir / "state.json", {}))
    if earliest:
        relation = "already before" if earliest < value else "after"
        reply += f"\nThe earliest date at the last check, {fmt_short(earliest)}, is {relation} that."
    return reply + "\nThe daily summary still comes."


def renew_reply(cfg: Config, chat_id: str) -> str:
    """The current renewal link, for the owner (it's their visa account)."""
    if chat_id != cfg.owner_chat:
        return "Only the owner can renew the visa-site session."
    if not cfg.renew_url:
        return "The renewal page isn't set up. Run `make renew-token` on the Mac."
    state = load_json(cfg.state_dir / "state.json", {})
    link = renew_link(cfg, state)
    if not link:
        return f"The session is {session_line(cfg, state, timeutil.utcnow())}. Nothing to renew."
    return (
        f"Sign in at {esc(cfg.sign_in_url)}, then click the slot-machine extension in Chrome, or use the "
        f'<a href="{html.escape(link, quote=True)}">renewal page</a> (home Wi-Fi).'
    )
