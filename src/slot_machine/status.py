"""The status report: `slot-machine --status` (make status) and /status in Telegram.

Both show the same rows. In Telegram a chat sees its own alert cutoff but not the other
chats' ids or cutoffs, and not the server's file paths.
"""

from __future__ import annotations

import re
import socket
import urllib.parse
from pathlib import Path

from . import timeutil
from .config import Config
from .messages import esc, session_line
from .store import BOT_FILE, load_json
from .timeutil import fmt_clock, fmt_day, fmt_short, local, parse_ts
from .watcher import alert_cutoff, current_earliest, pending_welcomes


def browser(ua: str) -> str:
    """ "Chrome 156 on macOS" from a User-Agent, or the start of it."""
    found = (re.search(rf"({b})/(\d+)", ua) for b in ("Edg", "OPR", "Chrome", "Firefox", "Version"))
    name = next((m for m in found if m), None)  # Edge and Opera also say "Chrome/"
    os_name = next((o for o in ("Mac OS X", "Windows", "Android", "iPhone", "Linux") if o in ua), None)
    if not name:
        return ua[:60]
    label = {"Edg": "Edge", "OPR": "Opera", "Version": "Safari"}.get(name.group(1), name.group(1))
    os_label = {"Mac OS X": "macOS", "iPhone": "iOS"}.get(os_name, os_name)
    return f"{label} {name.group(2)}" + (f" on {os_label}" if os_label else "")


def listening(cfg: Config) -> bool:
    """Whether the renewal page answers on this machine."""
    port = urllib.parse.urlsplit(cfg.renew_url).port
    try:
        with socket.create_connection(("localhost", port), timeout=1):
            return True
    except OSError:
        return False


def rows(cfg: Config, config_path: Path | None = None, chat: str | None = None) -> list[tuple[str, str]]:
    """(label, value) pairs, plain text. `chat`: the Telegram chat asking, which changes the
    Chats row to that chat's own view and drops Settings/State."""
    state = load_json(cfg.state_dir / "state.json", {})
    cookies = load_json(cfg.state_dir / "cookies.json", {})
    own = load_json(cfg.state_dir / BOT_FILE, {}).get("alert_before", {})
    now = timeutil.utcnow()
    cur = current_earliest(state)
    last = parse_ts(state.get("last_check_at"))
    ua = state.get("session_user_agent")
    out = [
        ("Location", f"{cfg.label} (facility {cfg.facility_id or '?'}, schedule {cfg.schedule_id or '?'})"),
        ("Earliest", fmt_day(cur, local(now, cfg).date()) if cur else "none / unknown"),
        ("Last check", fmt_clock(last, cfg) if last else "never"),
        (
            "Session",
            f"{'cookie saved' if cookies.get('_yatri_session') else 'no cookie'}; {session_line(cfg, state, now)}",
        ),
        ("Browser", f"{browser(ua)} (sent with the session)" if ua else "USER_AGENT setting"),
    ]
    if chat is None:
        pending = pending_welcomes(state, cfg)

        def describe(i: int, c: str) -> str:
            cutoff = alert_cutoff(cfg, own, c)
            return f"{c}{' (owner)' if i == 0 else ''}{f' [before {cutoff}]' if cutoff else ''}"

        chats = ", ".join(describe(i, c) for i, c in enumerate(cfg.chat_ids)) or "none"
        out.append(
            ("Chats", chats + (f"; welcome on the next check: {', '.join(pending)}" if pending else ""))
        )
    else:
        cutoff = alert_cutoff(cfg, own, chat)
        mine = f"dates before {fmt_short(cutoff)}" if cutoff else "every sooner date"
        out.append(("Alerts", f"{mine} (/set_alert_before); {len(cfg.chat_ids)} chat(s) listed"))
    if state.get("consecutive_errors"):
        out.append(("Errors", f"{state['consecutive_errors']} in a row, last: {state.get('last_error')}"))
    site = state.get("site")
    if site:
        since, ping = parse_ts(site["since"]), parse_ts(site.get("next_ping"))
        out.append(
            (
                "Visa site",
                f"{site['state']} since {fmt_clock(since, cfg)}, {site.get('pings', 0)} ping(s) unanswered, "
                f"next {fmt_clock(ping, cfg) if ping else 'now'}; last: {site.get('last_error')}",
            )
        )
    if cfg.renew_url:
        out.append(("Renew page", f"{cfg.renew_url} ({'listening' if listening(cfg) else 'not running'})"))
    if chat is None and config_path is not None:
        out.append(("Settings", f"{config_path}{'' if config_path.exists() else ' (missing)'}"))
        out.append(("State", str(cfg.state_dir)))
    return out


def text(cfg: Config, config_path: Path) -> str:
    """For the terminal: aligned labels."""
    return "\n".join(f"{label + ':':<12}{value}" for label, value in rows(cfg, config_path))


def telegram_text(cfg: Config, chat: str) -> str:
    """For /status (HTML parse mode)."""
    lines = [f"<b>Visa slot watch - {esc(cfg.label)}</b>"]
    lines += [f"<b>{esc(label)}:</b> {esc(value)}" for label, value in rows(cfg, chat=chat)]
    return "\n".join(lines)
