"""Fake AIS (visa site) and Telegram servers for the tests and the Pi smoke run.

Standard library only, so the same file runs on the Pi:

    python3 fakes.py --port-file /tmp/fakes.json

writes {"ais": "http://127.0.0.1:P", "telegram": "http://127.0.0.1:Q"} to the port
file and serves until killed. Scenario control over HTTP:

    POST {ais}/_control       JSON body, any of: dates, status, body, expire,
                              rotate_cookie, add_session
    GET  {ais}/_requests      {"count": N}
    GET  {telegram}/_messages list of sent messages
    POST {telegram}/_control  JSON body: {"fail_next": N, "fail_status": 500}
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VALID_SESSION = "s" * 40
BOT_TOKEN = "123456:TEST-token"
CHAT_ID = 42

ACCOUNT_PAGE = """<html><body>
<a href="/{locale}/niv/schedule/{schedule_id}/continue_actions">Continue</a>
</body></html>"""

APPOINTMENT_PAGE = """<html><body><form>
<select name="appointments[consulate_appointment][facility_id]"
        id="appointments_consulate_appointment_facility_id" class="required">
  <option value=""></option>
  <option value="{facility_id}">Astana</option>
  <option value="135">Almaty &amp; Co</option>
</select>
</form></body></html>"""

SIGN_IN_PAGE = """<html><body><form><input type="email" id="user_email"></form></body></html>"""


class FakeAIS:
    """Just enough of ais.usvisa-info.com for slot-machine to talk to."""

    def __init__(self, locale="en-kz", schedule_id="1234", facility_id="134"):
        self.locale = locale
        self.schedule_id = schedule_id
        self.facility_id = facility_id
        self.sessions = {VALID_SESSION}
        self.dates: list[str] = []
        self.status = 200  # forced status for days.json (403, 429, 500, ...)
        self.body: bytes | None = None  # raw body override for days.json
        self.rotate_cookie: str | None = None  # re-issue _yatri_session with this value
        self.requests: list[tuple[str, str, dict]] = []
        self.lock = threading.Lock()

    def control(self, cmd: dict) -> None:
        with self.lock:
            if "dates" in cmd:
                self.dates = list(cmd["dates"])
            if "status" in cmd:
                self.status = int(cmd["status"])
            if "body" in cmd:
                self.body = None if cmd["body"] is None else str(cmd["body"]).encode()
            if cmd.get("expire"):
                self.sessions.clear()
            if "rotate_cookie" in cmd:
                self.rotate_cookie = cmd["rotate_cookie"]
            if cmd.get("add_session"):
                self.sessions.add(cmd["add_session"])

    def days_requests(self) -> int:
        return sum(1 for _, path, _ in self.requests if "/appointment/days/" in path)

    def handle(self, h: BaseHTTPRequestHandler) -> None:
        path = urllib.parse.urlsplit(h.path).path
        self.requests.append((h.command, path, dict(h.headers)))
        if h.command == "POST" and path == "/_control":
            self.control(json.loads(h.rfile.read(int(h.headers.get("Content-Length", 0))) or b"{}"))
            return send(h, 200, b'{"ok": true}', "application/json")
        if path == "/_requests":
            return send(h, 200, json.dumps({"count": self.days_requests()}).encode(), "application/json")

        cookies = parse_cookie_header(h.headers.get("Cookie", ""))
        signed_in = cookies.get("_yatri_session") in self.sessions
        base = f"/{self.locale}/niv"
        if re.fullmatch(
            rf"{base}/schedule/{self.schedule_id}/appointment/days/{self.facility_id}\.json", path
        ):
            if not signed_in:
                return send(h, 302, b"", headers={"Location": f"https://example.test{base}/users/sign_in"})
            if self.status != 200:
                return send(h, self.status, b"")
            body = (
                self.body
                if self.body is not None
                else json.dumps([{"date": d, "business_day": True} for d in self.dates]).encode()
            )
            # like the real site: every JSON reply re-issues the session cookie (same value
            # unless a test asks for a new one), plus the ETag the browser would cache against
            session = self.rotate_cookie or cookies["_yatri_session"]
            self.sessions.add(session)
            extra = {
                "Set-Cookie": f"_yatri_session={session}; path=/; HttpOnly; Secure; SameSite=None",
                "ETag": 'W/"0123456789abcdef"',
            }
            return send(h, 200, body, "application/json", extra)
        if path == f"{base}/account":
            if not signed_in:
                return send(h, 200, SIGN_IN_PAGE.encode(), "text/html")
            page = ACCOUNT_PAGE.format(locale=self.locale, schedule_id=self.schedule_id)
            return send(h, 200, page.encode(), "text/html")
        if path == f"{base}/schedule/{self.schedule_id}/appointment":
            if not signed_in:
                return send(h, 200, SIGN_IN_PAGE.encode(), "text/html")
            return send(h, 200, APPOINTMENT_PAGE.format(facility_id=self.facility_id).encode(), "text/html")
        return send(h, 404, b"not found")


class FakeTelegram:
    """sendMessage and getUpdates for one bot token."""

    def __init__(self, token=BOT_TOKEN, chat_id=CHAT_ID):
        self.token = token
        self.chat_id = chat_id
        self.messages: list[dict] = []
        self.fail_next = 0
        self.fail_status = 500

    def handle(self, h: BaseHTTPRequestHandler) -> None:
        path = urllib.parse.urlsplit(h.path).path
        if path == "/_messages":
            return send(h, 200, json.dumps(self.messages).encode(), "application/json")
        if h.command == "POST" and path == "/_control":
            cmd = json.loads(h.rfile.read(int(h.headers.get("Content-Length", 0))) or b"{}")
            self.fail_next = int(cmd.get("fail_next", self.fail_next))
            self.fail_status = int(cmd.get("fail_status", self.fail_status))
            return send(h, 200, b'{"ok": true}', "application/json")
        if not path.startswith(f"/bot{self.token}/"):
            return send(
                h, 401, b'{"ok": false, "error_code": 401, "description": "Unauthorized"}', "application/json"
            )
        method = path.rsplit("/", 1)[-1]
        if method == "getUpdates":
            updates = [
                {
                    "update_id": 1,
                    "message": {
                        "message_id": 1,
                        "text": "hi",
                        "chat": {
                            "id": self.chat_id,
                            "type": "private",
                            "first_name": "Yarik",
                            "username": "yarik",
                        },
                    },
                }
            ]
            return send(h, 200, json.dumps({"ok": True, "result": updates}).encode(), "application/json")
        if method == "sendMessage" and h.command == "POST":
            if self.fail_next > 0:
                self.fail_next -= 1
                return send(
                    h, self.fail_status, b'{"ok": false, "description": "fake failure"}', "application/json"
                )
            form = urllib.parse.parse_qs(h.rfile.read(int(h.headers.get("Content-Length", 0))).decode())
            msg = {k: v[0] for k, v in form.items()}
            self.messages.append(msg)
            return send(
                h,
                200,
                json.dumps({"ok": True, "result": {"message_id": len(self.messages)}}).encode(),
                "application/json",
            )
        return send(h, 404, b'{"ok": false}', "application/json")


def parse_cookie_header(value: str) -> dict:
    jar = {}
    for part in value.split(";"):
        name, eq, val = part.strip().partition("=")
        if eq:
            jar[name] = val
    return jar


def send(h, status, body=b"", ctype="text/plain", headers=None):
    h.send_response(status)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(body)))
    for k, v in (headers or {}).items():
        h.send_header(k, v)
    h.end_headers()
    h.wfile.write(body)


class Server:
    """Run a fake on 127.0.0.1 in a background thread."""

    def __init__(self, fake):
        self.fake = fake

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                fake.handle(self)

            do_POST = do_GET

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def main() -> None:
    p = argparse.ArgumentParser(description="Run fake AIS and Telegram servers until killed.")
    p.add_argument("--port-file", required=True)
    args = p.parse_args()
    with Server(FakeAIS()) as ais, Server(FakeTelegram()) as tg:
        with open(args.port_file, "w") as f:
            json.dump(
                {
                    "ais": ais.url,
                    "telegram": tg.url,
                    "session": VALID_SESSION,
                    "token": BOT_TOKEN,
                    "chat_id": CHAT_ID,
                },
                f,
            )
        threading.Event().wait()


if __name__ == "__main__":
    main()
