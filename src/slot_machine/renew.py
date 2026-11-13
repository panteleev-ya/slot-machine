"""The renewal page: a small HTTP server on the home network that takes a fresh
_yatri_session and runs one check with it, like `make set-cookie`.

Two ways in, both write-only (nothing here ever returns a cookie):
  POST /api/cookie   JSON {"cookie": ..., "user_agent": ...}, header "Authorization: Bearer RENEW_TOKEN".
                     Used by the Chrome extension (extension/).
  GET  /?t=TOKEN     a page with a paste box; TOKEN comes from the "session ended" alert
  POST /renew        and only works until that session is renewed (renew_link.py).
  GET  /api/status   (Bearer) session status for the extension's popup; the sign-in loop
                     (src/signin) calls it to see that the Pi is there before signing in.

Cron starts `slot-machine --serve` every minute; if a server is already running, the
new one exits at once, so cron doubles as a watchdog. Plain http: home network only.

The session's checks use the User-Agent of the browser that sent it (the extension's
"user_agent", else the request's own User-Agent header): the site rejects a session
sent from another browser version.
"""

from __future__ import annotations

import contextlib
import hmac
import html
import json
import socket
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import ais, timeutil
from .config import Config
from .messages import pause_note, session_line
from .renew_link import link_token, link_token_ok
from .runner import run_check
from .store import load_json, single_instance
from .timeutil import fmt_day, local, log
from .watcher import current_earliest

LOCK_NAME = ".renew.lock"
MAX_BODY = 16_384  # a _yatri_session is about 1 KB


# --------------------------------------------------------------------------- the work


KEPT = " The Pi keeps the session it has, which still works."


def kept_instead(cfg: Config, submitted: dict) -> bool:
    """After run_check: the submitted session was not saved, and the one the Pi kept still works."""
    state = load_json(cfg.state_dir / "state.json", {})
    current = load_json(cfg.state_dir / "cookies.json", {}).get("_yatri_session")
    return (
        bool(current) and current != submitted.get("_yatri_session") and not state.get("session_expired_at")
    )


def _pause_note(cfg: Config) -> str:
    now = timeutil.utcnow()
    return pause_note(cfg, load_json(cfg.state_dir / "state.json", {}), now)


def apply_cookie(cfg: Config, raw: str, via: str, user_agent: str | None = None) -> tuple[int, dict]:
    """Save the cookie and run one check with the browser's User-Agent.
    Returns (HTTP status, {"ok", "message"})."""
    try:
        cookies = ais.parse_cookie_input(raw or "")
    except ValueError as e:
        return 400, {"ok": False, "message": f"Not saved: {e}."}
    ua = ais.clean_user_agent(user_agent)
    _, res = run_check(cfg, force=True, new_cookies=cookies, via=via, new_user_agent=ua)
    if res is None:
        return 409, {"ok": False, "message": "A check is running right now. Send it again in a minute."}
    today = local(timeutil.utcnow(), cfg).date()
    kept = KEPT if res.kind not in ("ok", "empty", "skipped") and kept_instead(cfg, cookies) else ""
    if res.kind == "ok":
        log(f"renew: new session from {via} works")
        return 200, {"ok": True, "message": f"Session saved. Earliest date: {fmt_day(res.dates[0], today)}"}
    if res.kind == "empty":
        return 200, {
            "ok": True,
            "message": " ".join(
                ["Session saved, but no dates came back (none open, or throttled).", _pause_note(cfg)]
            ).rstrip(),
        }
    if res.kind == "expired":
        return 200, {
            "ok": False,
            "message": (
                "Not saved: the site treats that session as signed out. Sign in in this browser "
                f"until you see your appointment page, then send again.{kept}"
            ),
        }
    if res.kind == "skipped":
        return 200, {"ok": False, "message": f"Session saved, but nothing was checked: {res.detail}."}
    if kept:
        return 200, {
            "ok": False,
            "message": f"Not saved: the check failed ({res.detail}).{kept} Try again later.",
        }
    return 200, {"ok": False, "message": f"Session saved, but the check failed: {res.detail}."}


def status(cfg: Config) -> dict:
    state = load_json(cfg.state_dir / "state.json", {})
    now = timeutil.utcnow()
    cur = current_earliest(state)
    return {
        "session": session_line(cfg, state, now),
        "expired": bool(state.get("session_expired_at")),
        "earliest": fmt_day(cur, local(now, cfg).date()) if cur else None,
        "sign_in_url": cfg.sign_in_url,
    }


# --------------------------------------------------------------------------- the page

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Renew the visa-site session</title>
<style>
  :root {{ color-scheme: light dark; --fg: #1b1b1b; --bg: #fafafa; --muted: #666; --ok: #17803d; --bad: #b42318; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --fg: #eee; --bg: #161616; --muted: #aaa; --ok: #4ade80; --bad: #f87171; }} }}
  body {{ font: 16px/1.5 system-ui, sans-serif; color: var(--fg); background: var(--bg);
         max-width: 36rem; margin: 2rem auto; padding: 0 1rem; }}
  h1 {{ font-size: 1.3rem; }} .muted {{ color: var(--muted); }} .ok {{ color: var(--ok); }} .bad {{ color: var(--bad); }}
  textarea {{ width: 100%; min-height: 6rem; font: 13px ui-monospace, monospace; box-sizing: border-box; }}
  button {{ font: inherit; padding: .5rem 1.2rem; margin-top: .5rem; }}
</style></head><body>
<h1>Renew the visa-site session</h1>
{body}
</body></html>"""


def page_body(cfg: Config, token: str | None, result: dict | None = None) -> str:
    state = load_json(cfg.state_dir / "state.json", {})
    parts = []
    if result:
        css = "ok" if result["ok"] else "bad"
        parts.append(f'<p class="{css}"><b>{html.escape(result["message"])}</b></p>')
    if token and link_token_ok(cfg, state, token):
        parts.append(
            f'<p class="muted">Session: {html.escape(session_line(cfg, state, timeutil.utcnow()))}</p>'
        )
        parts.append(
            "<ol>"
            f'<li>Sign in at <a href="{html.escape(cfg.sign_in_url)}" target="_blank" rel="noopener">'
            "ais.usvisa-info.com</a>. Don't sign out afterwards: that would end the session.</li>"
            "<li>Click the slot-machine extension in Chrome. Or copy the value of the "
            "<code>_yatri_session</code> cookie (DevTools → Application → Cookies) and paste it here:</li>"
            "</ol>"
            '<form method="post" action="/renew">'
            f'<input type="hidden" name="t" value="{html.escape(token)}">'
            '<textarea name="cookie" required autocomplete="off" spellcheck="false" '
            'placeholder="_yatri_session value"></textarea>'
            "<button>Send to the Pi</button></form>"
        )
    elif not state.get("session_expired_at"):
        if not result:
            parts.append("<p>The session is active, so there's nothing to renew right now.</p>")
    elif not result or not result["ok"]:
        parts.append(
            "<p>This page takes a new session only through the link in the latest "
            "“session ended” message, or from the Chrome extension.</p>"
        )
    return "\n".join(parts)


# --------------------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = "slot-machine"
    sys_version = ""
    timeout = 30  # a stalled client can't hold the (single-threaded) server for long

    @property
    def cfg(self) -> Config:
        return self.server.cfg

    def log_message(self, *args):  # never log request lines: link tokens ride in the query
        pass

    # ---- helpers

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")  # the page URL carries the token
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        origin = self.headers.get("Origin", "")
        if origin.startswith("chrome-extension://"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data: dict) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def _page(self, status: int, token: str | None, result: dict | None = None) -> None:
        body = PAGE.format(body=page_body(self.cfg, token, result)).encode()
        self._send(status, body, "text/html; charset=utf-8")

    def _body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            return None
        return self.rfile.read(length)

    def _bearer_ok(self) -> bool:
        given = self.headers.get("Authorization", "")
        return given.startswith("Bearer ") and hmac.compare_digest(given[7:].strip(), self.cfg.renew_token)

    # ---- routes

    def do_OPTIONS(self):  # CORS / private-network preflight from the extension
        self.send_response(204)
        origin = self.headers.get("Origin", "")
        if origin.startswith("chrome-extension://"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Vary", "Origin")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/":
            token = urllib.parse.parse_qs(url.query).get("t", [None])[0]
            return self._page(200, token)
        if url.path == "/api/status":
            if not self._bearer_ok():
                return self._json(
                    401, {"ok": False, "message": "Wrong or missing token (extension options)."}
                )
            return self._json(200, {"ok": True, **status(self.cfg)})
        return self._send(404, b"not found", "text/plain")

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        body = self._body()
        if body is None:
            return self._send(413, b"too large", "text/plain")
        if path == "/api/cookie":
            if not self._bearer_ok():
                return self._json(
                    401, {"ok": False, "message": "Wrong or missing token (extension options)."}
                )
            try:
                data = json.loads(body or b"{}")
                raw, ua = data.get("cookie", ""), data.get("user_agent")
            except (ValueError, AttributeError):
                return self._json(400, {"ok": False, "message": 'Send JSON like {"cookie": "..."}.'})
            ua = ua or self.headers.get("User-Agent")
            status_code, result = apply_cookie(self.cfg, str(raw), via="extension", user_agent=ua)
            return self._json(status_code, result)
        if path == "/renew":
            form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
            token = form.get("t", [""])[0]
            state = load_json(self.cfg.state_dir / "state.json", {})
            if not link_token_ok(self.cfg, state, token):
                result = {"ok": False, "message": "This link has expired. Use the one in the latest message."}
                return self._page(403, None, result)
            status_code, result = apply_cookie(
                self.cfg,
                form.get("cookie", [""])[0],
                via="renew-page",
                user_agent=self.headers.get("User-Agent"),
            )
            # if it didn't work and the session is still ended, offer the form again with the new link token
            fresh = link_token(self.cfg, load_json(self.cfg.state_dir / "state.json", {}))
            return self._page(status_code, fresh, result)
        return self._send(404, b"not found", "text/plain")


class _Server(HTTPServer):
    allow_reuse_address = True

    def __init__(self, cfg: Config, address, family):
        self.address_family = family
        self.cfg = cfg
        super().__init__(address, Handler)

    def server_bind(self):
        if self.address_family == socket.AF_INET6:
            with contextlib.suppress(OSError):  # also accept IPv4 (yarikos.local resolves to both)
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def make_server(cfg: Config, port: int, host: str | None = None) -> HTTPServer:
    if host:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        return _Server(cfg, (host, port), family)
    try:
        return _Server(cfg, ("::", port), socket.AF_INET6)
    except OSError:
        return _Server(cfg, ("0.0.0.0", port), socket.AF_INET)


def serve(cfg: Config) -> int:
    """Run the page until killed; return at once if it's off or already running."""
    if not (cfg.renew_url and cfg.renew_token):
        return 0
    with single_instance(cfg.state_dir, LOCK_NAME) as got_lock:
        if not got_lock:
            return 0
        port = urllib.parse.urlsplit(cfg.renew_url).port
        try:
            httpd = make_server(cfg, port)
        except OSError as e:
            log(f"renew page: can't listen on port {port}: {e.strerror or e}")
            return 1
        log(f"renew page: listening on port {port}")
        with httpd:
            httpd.serve_forever()
    return 0
