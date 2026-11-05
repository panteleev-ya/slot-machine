"""Telegram Bot API: send a message, list the chats that wrote to the bot."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import Config
from .timeutil import log


def send_message(cfg: Config, text: str, silent: bool = False, chat_id: str | None = None) -> bool:
    """Send one HTML message to one chat (default: the owner's); retry once unless
    Telegram rejected it outright."""
    chat_id = chat_id or cfg.owner_chat
    if not (cfg.bot_token and chat_id):
        log("telegram not configured; message not sent")
        return False
    data = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_notification": "true" if silent else "false",
            "link_preview_options": json.dumps({"is_disabled": True}),
        }
    ).encode()
    url = f"{cfg.telegram_api}/bot{cfg.bot_token}/sendMessage"
    for attempt in range(2):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
                if json.loads(r.read().decode()).get("ok"):
                    return True
        except urllib.error.HTTPError as e:  # never log the URL: it contains the token
            log(f"telegram HTTP {e.code} for chat {chat_id}: {e.read()[:200]!r}")
            if 400 <= e.code < 500 and e.code != 429:
                return False
        except Exception as e:
            log(f"telegram failed: {type(e).__name__}")
        if attempt == 0:
            time.sleep(3)
    return False


def find_chats(cfg: Config) -> dict:
    """{chat_id: name} for everyone who recently messaged the bot."""
    if not cfg.bot_token:
        raise SystemExit("Put TELEGRAM_BOT_TOKEN in the settings file first.")
    try:
        with urllib.request.urlopen(f"{cfg.telegram_api}/bot{cfg.bot_token}/getUpdates", timeout=20) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Telegram said HTTP {e.code} - check the token.") from None
    except Exception as e:
        raise SystemExit(f"Telegram request failed: {type(e).__name__}") from None
    chats = {}
    for update in data.get("result", []):
        chat = (update.get("message") or update.get("channel_post") or {}).get("chat") or {}
        if "id" in chat:
            chats[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name") or ""
    return chats
