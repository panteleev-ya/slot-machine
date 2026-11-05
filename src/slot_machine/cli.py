"""Command line: a plain run is one scheduled check; flags are setup and inspection."""

from __future__ import annotations

import argparse
import getpass
import re
import socket
import urllib.parse
from pathlib import Path

from . import __version__, ais, bot, renew, telegram, timeutil
from .config import CONFIG_ENV_VAR, Config, default_config_path, load_config
from .messages import session_line
from .runner import run_check
from .store import load_json
from .timeutil import fmt_clock, fmt_day, local, parse_ts
from .watcher import alert_cutoff, current_earliest, pending_welcomes


def cmd_set_cookie(cfg: Config, value: str) -> int:
    raw = value or getpass.getpass("Paste the _yatri_session cookie value (input hidden): ")
    try:
        cookies = ais.parse_cookie_input(raw)
    except ValueError as e:
        raise SystemExit(f"Not saved: {e}") from None
    code, res = run_check(cfg, force=True, new_cookies=cookies, via="set-cookie")
    if res is None:
        return code
    if res.kind == "ok":
        print(f"Cookie works. Earliest date right now: {res.dates[0]}. Cron takes it from here.")
    elif res.kind == "empty":
        print("Cookie works, but no dates were returned (none open, or throttled).")
    elif res.kind == "expired":
        print("The site rejected that session. Sign in again and copy a fresh _yatri_session value.")
    elif res.kind == "skipped":
        print(f"Cookie saved. {res.detail}. Try --discover, fill the settings file, then run with --force.")
    else:
        print(f"Cookie saved, but the check failed: {res.detail}")
    return code


def cmd_discover(cfg: Config) -> int:
    cookies = load_json(cfg.state_dir / "cookies.json", {})
    if not cookies.get("_yatri_session"):
        raise SystemExit("Run --set-cookie first.")
    headers = ais.ais_headers(cfg, cookies, json_api=False)
    expired = "That session isn't valid any more; sign in again and rerun --set-cookie."
    if not cfg.schedule_id:
        _, _, body = ais.http_get(f"{cfg.ais_base}/{cfg.locale}/niv/account", headers, follow_redirects=True)
        page = body.decode("utf-8", "replace")
        if ais.is_sign_in_page(page):
            raise SystemExit(expired)
        ids = sorted(set(re.findall(r"/niv/schedule/(\d+)/", page)))
        print("Schedule ids found:", ", ".join(ids) or "none (read it from the URL after 'Continue')")
        if len(ids) != 1:
            return 0
        cfg.schedule_id = ids[0]
    _, _, body = ais.http_get(cfg.appointment_url, headers, follow_redirects=True)
    page = body.decode("utf-8", "replace")
    if ais.is_sign_in_page(page):
        raise SystemExit(expired)
    found = ais.parse_facility_options(page)
    if not found:
        print(
            "Couldn't find the location list on the reschedule page. Use DevTools -> Network instead:\n"
            "pick Astana and read the number in .../appointment/days/<ID>.json"
        )
        return 1
    print(f"AIS_SCHEDULE_ID={cfg.schedule_id}")
    for select_id, options in found:
        print(f"{select_id}:")
        for value, name in options:
            print(f"  {value:>6}  {name}")
    print("Put the Astana number into AIS_FACILITY_ID.")
    return 0


def cmd_find_chat_id(cfg: Config) -> int:
    # chats the poller saw (it consumes the bot's messages) plus whatever Telegram still holds
    chats = dict(load_json(cfg.state_dir / bot.BOT_FILE, {}).get("seen_chats", {}))
    chats.update(telegram.find_chats(cfg))
    if not chats:
        print("No messages yet. Send your bot any message in Telegram, then run this again.")
    for chat_id, name in chats.items():
        note = "  already in TELEGRAM_CHAT_ID" if chat_id in cfg.chat_ids else ""
        print(f"TELEGRAM_CHAT_ID={chat_id}    ({name}){note}")
    return 0


def cmd_test_telegram(cfg: Config) -> int:
    if not cfg.chat_ids:
        print("Failed - set TELEGRAM_CHAT_ID first.")
        return 1
    failed = 0
    for chat in cfg.chat_ids:
        ok = telegram.send_message(
            cfg, "✅ Test message from slot-machine - notifications work.", chat_id=chat
        )
        print(
            f"{chat}: {'sent' if ok else 'FAILED - check TELEGRAM_BOT_TOKEN, and that this chat messaged the bot'}"
        )
        failed += not ok
    return 1 if failed else 0


def cmd_status(cfg: Config, config_path: Path) -> int:
    state = load_json(cfg.state_dir / "state.json", {})
    cookies = load_json(cfg.state_dir / "cookies.json", {})
    now = timeutil.utcnow()
    cur = current_earliest(state)
    last = parse_ts(state.get("last_check_at"))
    print(f"Location:   {cfg.label} (facility {cfg.facility_id or '?'}, schedule {cfg.schedule_id or '?'})")
    print(f"Earliest:   {fmt_day(cur, local(now, cfg).date()) if cur else 'none / unknown'}")
    print(f"Last check: {fmt_clock(last, cfg) if last else 'never'}")
    cookie = "cookie saved" if cookies.get("_yatri_session") else "no cookie"
    print(f"Session:    {cookie}; {session_line(cfg, state, now)}")
    pending = pending_welcomes(state, cfg)
    own = load_json(cfg.state_dir / bot.BOT_FILE, {}).get("alert_before", {})

    def describe(i: int, c: str) -> str:
        cutoff = alert_cutoff(cfg, own, c)
        return f"{c}{' (owner)' if i == 0 else ''}{f' [before {cutoff}]' if cutoff else ''}"

    chats = ", ".join(describe(i, c) for i, c in enumerate(cfg.chat_ids)) or "none"
    print(f"Chats:      {chats}" + (f"; welcome on the next check: {', '.join(pending)}" if pending else ""))
    if state.get("consecutive_errors"):
        print(f"Errors:     {state['consecutive_errors']} in a row, last: {state.get('last_error')}")
    if cfg.renew_url:
        print(f"Renew page: {cfg.renew_url} ({'listening' if _listening(cfg) else 'not running'})")
    print(f"Settings:   {config_path}{'' if config_path.exists() else ' (missing)'}")
    print(f"State:      {cfg.state_dir}")
    return 0


def _listening(cfg: Config) -> bool:
    port = urllib.parse.urlsplit(cfg.renew_url).port
    try:
        with socket.create_connection(("localhost", port), timeout=1):
            return True
    except OSError:
        return False


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="slot-machine",
        description="Watch the earliest US visa appointment date and report to Telegram.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"settings file (default: ${CONFIG_ENV_VAR} or ~/.config/slot-machine/slot-machine.env)",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--set-cookie",
        nargs="?",
        const="",
        metavar="VALUE",
        help="save a fresh _yatri_session cookie (prompts if VALUE is omitted) and test it",
    )
    mode.add_argument(
        "--discover", action="store_true", help="list schedule ids and location ids for your account"
    )
    mode.add_argument(
        "--find-chat-id", action="store_true", help="show the chat id of whoever messaged your bot"
    )
    mode.add_argument("--test-telegram", action="store_true", help="send a test message")
    mode.add_argument("--status", action="store_true", help="print the current state")
    mode.add_argument(
        "--serve", action="store_true", help="run the renewal page (RENEW_URL) until killed; cron starts it"
    )
    mode.add_argument(
        "--poll-telegram",
        nargs="?",
        const=55.0,
        type=float,
        metavar="SECONDS",
        help="answer Telegram commands (/check) for about SECONDS (default 55), then exit; cron runs it every minute",
    )
    p.add_argument("--force", action="store_true", help="ignore MIN_INTERVAL_MINUTES and any back-off")
    p.add_argument("--dry-run", action="store_true", help="check, but print messages instead of sending them")
    p.add_argument(
        "--via", default="manual", metavar="NAME", help="what started this run, for the log (cron, ...)"
    )
    args = p.parse_args(argv)
    config_path = args.config or default_config_path()
    cfg = load_config(config_path)

    if args.set_cookie is not None:
        return cmd_set_cookie(cfg, args.set_cookie)
    if args.discover:
        return cmd_discover(cfg)
    if args.find_chat_id:
        return cmd_find_chat_id(cfg)
    if args.test_telegram:
        return cmd_test_telegram(cfg)
    if args.status:
        return cmd_status(cfg, config_path)
    if args.poll_telegram is not None:
        return bot.poll(cfg, args.poll_telegram)
    if args.serve:
        return renew.serve(cfg)
    return run_check(cfg, force=args.force, dry_run=args.dry_run, via=args.via)[0]
