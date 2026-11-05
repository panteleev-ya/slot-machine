"""The link in a "session ended" alert, for the renewal page.

The link's token is derived from RENEW_TOKEN and the moment the session ended, so
nothing extra is stored and the server can check it. It stops working as soon as a
new session is saved (session_expired_at changes), so a link found later in the chat
history is useless. Pure: no clock, no files.
"""

from __future__ import annotations

import hashlib
import hmac

from .config import Config


def link_token(cfg: Config, state: dict) -> str | None:
    """Valid while the session that ended at state["session_expired_at"] isn't renewed."""
    expired_at = state.get("session_expired_at")
    if not (cfg.renew_url and cfg.renew_token and expired_at):
        return None
    message = f"renew-link:{expired_at}".encode()
    return hmac.new(cfg.renew_token.encode(), message, hashlib.sha256).hexdigest()[:32]


def renew_link(cfg: Config, state: dict) -> str | None:
    token = link_token(cfg, state)
    return f"{cfg.renew_url}/?t={token}" if token else None


def link_token_ok(cfg: Config, state: dict, given: str) -> bool:
    expected = link_token(cfg, state)
    return bool(expected and given) and hmac.compare_digest(expected, given)
