"""The renewal page: the extension's API, the link from the alert, the token script."""

import html
import json
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from slot_machine import bot, renew
from slot_machine.config import load_config
from slot_machine.renew_link import link_token, link_token_ok, renew_link
from slot_machine.runner import run_check
from slot_machine.store import single_instance

from fakes import VALID_SESSION

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "T" * 43
JAR = {"_yatri_session": VALID_SESSION}
FRESH = "f" * 40  # a second session the fake site accepts
OWNER, FRIEND = "42", "77"


def call(server, method, path, body=None, headers=None):
    request = urllib.request.Request(server.url + path, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=20) as resp:
            return resp.status, resp.headers, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode()


def api(server, path, data=None, token=TOKEN, method=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    status, _, text = call(server, method or ("POST" if body else "GET"), path, body, headers)
    return status, json.loads(text)


def form(server, **fields):
    body = urllib.parse.urlencode(fields).encode()
    return call(server, "POST", "/renew", body, {"Content-Type": "application/x-www-form-urlencoded"})


def state(cfg) -> dict:
    return json.loads((cfg.state_dir / "state.json").read_text())


@pytest.fixture
def live(cfg, ais, tg, clock):
    """Renewal configured, a working session, baseline sent (earliest Sep 1)."""
    cfg.chat_id = f"{OWNER},{FRIEND}"
    cfg.renew_url, cfg.renew_token = "http://yarikos.local:8080", TOKEN
    ais.fake.dates = ["2027-09-01"]
    ais.fake.sessions.add(FRESH)
    run_check(cfg, force=True, new_cookies=JAR)
    return cfg


@pytest.fixture
def expired(live, ais, tg, clock):
    ais.fake.sessions.discard(VALID_SESSION)
    clock.advance(hours=1)
    run_check(live)
    assert state(live)["session_expired_at"]
    return live


@pytest.fixture
def server(live):
    httpd = renew.make_server(live, 0, host="127.0.0.1")
    httpd.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


# --------------------------------------------------------------------------- settings


def write_env(tmp_path, text):
    path = tmp_path / "x.env"
    path.write_text(text)
    path.chmod(0o600)
    return path


def test_renewal_settings(tmp_path):
    cfg = load_config(write_env(tmp_path, f"RENEW_URL=http://yarikos.local:8080/\nRENEW_TOKEN={TOKEN}\n"))
    assert (cfg.renew_url, cfg.renew_token) == ("http://yarikos.local:8080", TOKEN)
    assert load_config(write_env(tmp_path, "RENEW_URL=\nRENEW_TOKEN=\n")).renew_url == ""  # off


@pytest.mark.parametrize(
    "url, message",
    [
        ("http://yarikos.local", "must look like"),  # no port
        ("https://yarikos.local:8443", "must look like"),
        ("http://yarikos.local:8080/renew", "must look like"),
        ("yarikos.local:8080", "must look like"),
        ("http://yarikos.local:80", "1024 or more"),
    ],
)
def test_bad_renew_url(tmp_path, url, message):
    with pytest.raises(SystemExit, match=message):
        load_config(write_env(tmp_path, f"RENEW_URL={url}\nRENEW_TOKEN={TOKEN}\n"))


@pytest.mark.parametrize("token", ["", "short", "x" * 31, "y" * 40 + "!"])
def test_renew_url_needs_a_proper_token(tmp_path, token):
    with pytest.raises(SystemExit, match="make renew-token"):
        load_config(write_env(tmp_path, f"RENEW_URL=http://yarikos.local:8080\nRENEW_TOKEN={token}\n"))


# --------------------------------------------------------------------------- the link


def test_link_only_while_the_session_is_ended(live, expired):
    st = state(expired)
    link = renew_link(expired, st)
    assert link.startswith("http://yarikos.local:8080/?t=") and len(link.split("t=")[1]) == 32
    assert link_token_ok(expired, st, link.split("t=")[1])
    assert not link_token_ok(expired, st, "0" * 32)
    assert not link_token_ok(expired, st, "")
    assert renew_link(live, {}) is None  # no expiry, no link


def test_link_depends_on_the_secret_and_the_expiry(expired):
    st = state(expired)
    original = link_token(expired, st)
    assert link_token(expired, dict(st, session_expired_at="2026-11-05T09:00:00+00:00")) != original
    expired.renew_token = "U" * 43
    assert link_token(expired, st) != original  # rotating the secret kills old links
    expired.renew_url = ""
    assert link_token(expired, st) is None


def test_session_ended_alert_links_to_the_page(expired, tg):
    alert = next(m for m in tg.fake.messages if "session ended" in m["text"])
    assert alert["chat_id"] == OWNER
    token = link_token(expired, state(expired))
    assert f'<a href="http://yarikos.local:8080/?t={token}">renewal page</a>' in alert["text"]
    assert "click the slot-machine extension in Chrome" in alert["text"]


# --------------------------------------------------------------------------- the extension's API


def test_extension_sends_a_new_session(server, expired, ais, tg, capsys):
    status, data = api(server, "/api/cookie", {"cookie": FRESH})
    assert (status, data) == (
        200,
        {"ok": True, "message": "Session saved. Earliest date: Wed Sep 1, 2027 (in 300 days)"},
    )
    st = state(expired)
    assert st["session_expired_at"] is None
    assert json.loads((expired.state_dir / "cookies.json").read_text()) == {"_yatri_session": FRESH}
    out = capsys.readouterr().out
    assert "check: ok 2027-09-01 [extension]" in out and "renew: new session from extension works" in out
    assert FRESH not in out and TOKEN not in out


def test_status_for_the_popup(server, expired):
    status, data = api(server, "/api/status")
    assert status == 200 and data["ok"] and data["expired"] is True
    assert data["session"].startswith("EXPIRED since")
    assert data["earliest"] == "Wed Sep 1, 2027 (in 300 days)"
    assert data["sign_in_url"] == "https://ais.usvisa-info.com/en-kz/niv/users/sign_in".replace(
        "https://ais.usvisa-info.com", expired.ais_base
    )
    assert VALID_SESSION not in json.dumps(data)


@pytest.mark.parametrize("token", [None, "wrong", TOKEN[:-1], TOKEN + "x"])
def test_api_needs_the_token(server, live, ais, token):
    assert api(server, "/api/status", token=token)[0] == 401
    status, data = api(server, "/api/cookie", {"cookie": FRESH}, token=token)
    assert status == 401 and not data["ok"]
    assert ais.fake.days_requests() == 1  # nothing was checked


def test_api_rejects_bad_input(server, live, ais):
    assert api(server, "/api/cookie", {"cookie": "nope"}) == (
        400,
        {"ok": False, "message": "Not saved: that doesn't look like a _yatri_session cookie value."},
    )
    status, _, _ = call(server, "POST", "/api/cookie", b"not json", {"Authorization": f"Bearer {TOKEN}"})
    assert status == 400
    big = json.dumps({"cookie": "x" * 20_000}).encode()
    status, _, _ = call(server, "POST", "/api/cookie", big, {"Authorization": f"Bearer {TOKEN}"})
    assert status == 413
    assert ais.fake.days_requests() == 1


def test_api_when_the_site_rejects_the_session(server, live):
    status, data = api(server, "/api/cookie", {"cookie": "z" * 40})
    assert status == 200 and data == {
        "ok": False,
        "message": "The site didn't accept that session. Sign in again, then send the new one.",
    }


def test_api_while_a_check_runs(server, live):
    with single_instance(live.state_dir):
        status, data = api(server, "/api/cookie", {"cookie": FRESH})
    assert status == 409 and "Send it again in a minute" in data["message"]


def test_preflight_from_the_extension(server):
    status, headers, _ = call(
        server,
        "OPTIONS",
        "/api/cookie",
        headers={"Origin": "chrome-extension://abc", "Access-Control-Request-Method": "POST"},
    )
    assert status == 204
    assert headers["Access-Control-Allow-Origin"] == "chrome-extension://abc"
    assert headers["Access-Control-Allow-Private-Network"] == "true"
    _, headers, _ = call(server, "OPTIONS", "/api/cookie", headers={"Origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in headers


# --------------------------------------------------------------------------- the page


def test_page_without_a_link_shows_nothing_useful(server, expired):
    status, headers, page = call(server, "GET", "/")
    assert status == 200 and "only through the link in the latest" in page
    assert "<form" not in page
    assert headers["Referrer-Policy"] == "no-referrer" and headers["Cache-Control"] == "no-store"


def test_page_with_the_link_offers_the_form(server, expired):
    token = link_token(expired, state(expired))
    status, _, page = call(server, "GET", f"/?t={token}")
    assert status == 200 and '<form method="post" action="/renew">' in page
    assert f'name="t" value="{token}"' in page and "EXPIRED since" in page


def test_pasting_on_the_page_renews_and_kills_the_link(server, expired, capsys):
    token = link_token(expired, state(expired))
    status, _, page = form(server, t=token, cookie=FRESH)
    assert status == 200 and "Session saved. Earliest date: Wed Sep 1, 2027" in page
    assert "<form" not in page
    assert "check: ok 2027-09-01 [renew-page]" in capsys.readouterr().out
    status, _, page = form(server, t=token, cookie=FRESH)  # the same link again
    assert status == 403 and "This link has expired" in page
    _, _, page = call(server, "GET", f"/?t={token}")
    assert "nothing to renew right now" in page


def test_page_after_a_rejected_session_offers_a_fresh_link(server, expired, clock):
    old = link_token(expired, state(expired))
    clock.advance(minutes=3)  # you sign in again; the new expiry gets a new time
    status, _, page = form(server, t=old, cookie="z" * 40)
    assert "The site didn't accept that session" in html.unescape(page)
    new = link_token(expired, state(expired))
    assert new != old and f'name="t" value="{new}"' in page


def test_page_rejects_a_bad_cookie_and_keeps_the_form(server, expired):
    token = link_token(expired, state(expired))
    status, _, page = form(server, t=token, cookie="nope")
    assert status == 400 and "Not saved" in page and f'value="{token}"' in page


def test_page_escapes_what_it_echoes(server, expired):
    _, _, page = call(server, "GET", "/?t=%3Cscript%3E")
    assert "<script>" not in page


def test_responses_never_contain_the_session(server, expired):
    token = link_token(expired, state(expired))
    for path in ("/", f"/?t={token}"):
        assert VALID_SESSION not in call(server, "GET", path)[2]
    assert VALID_SESSION not in json.dumps(api(server, "/api/status")[1])


# --------------------------------------------------------------------------- running it


def test_serve_is_off_without_settings(cfg):
    assert renew.serve(cfg) == 0  # returns at once


def test_second_server_steps_aside(live):
    with single_instance(live.state_dir, renew.LOCK_NAME):
        assert renew.serve(live) == 0


def test_busy_port_is_reported(live, capsys, monkeypatch):
    def busy(cfg, port):
        raise OSError(98, "Address in use")

    monkeypatch.setattr(renew, "make_server", busy)
    assert renew.serve(live) == 1
    assert "renew page: can't listen on port 8080: Address in use" in capsys.readouterr().out


def test_listens_on_ipv4_and_ipv6(live):
    if not socket.has_ipv6:
        pytest.skip("no IPv6")
    httpd = renew.make_server(live, 0)  # default: dual stack
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        for host in ("127.0.0.1", "[::1]"):
            with urllib.request.urlopen(f"http://{host}:{port}/", timeout=10) as resp:
                assert resp.status == 200
    finally:
        httpd.shutdown()
        httpd.server_close()


# --------------------------------------------------------------------------- /renew in Telegram


def renew_reply(cfg, tg, chat):
    tg.fake.messages.clear()
    tg.fake.say(chat, "/renew")
    bot.poll(cfg, 0)
    return [m["text"] for m in tg.fake.messages if m["chat_id"] == chat]


def test_renew_command_gives_the_owner_the_link(expired, tg):
    bot.poll(expired, 0)  # the fake's initial "hi"
    [reply] = renew_reply(expired, tg, OWNER)
    token = link_token(expired, state(expired))
    assert f'<a href="http://yarikos.local:8080/?t={token}">renewal page</a>' in reply


def test_renew_command_is_owner_only(expired, tg):
    bot.poll(expired, 0)
    assert renew_reply(expired, tg, FRIEND) == ["Only the owner can renew the visa-site session."]


def test_renew_command_with_an_active_session(live, tg):
    bot.poll(live, 0)
    [reply] = renew_reply(live, tg, OWNER)
    assert reply.startswith("The session is active for") and reply.endswith("Nothing to renew.")


def test_renew_command_without_the_page(cfg, tg, ais, clock):
    ais.fake.dates = ["2027-09-01"]
    run_check(cfg, force=True, new_cookies=JAR)
    bot.poll(cfg, 0)
    assert renew_reply(cfg, tg, OWNER) == [
        "The renewal page isn't set up. Run `make renew-token` on the Mac."
    ]


# --------------------------------------------------------------------------- make renew-token


def run_token_script(local):
    return subprocess.run(
        [sys.executable, str(ROOT / "deploy" / "renew_token.py"), str(local)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},  # pbcopy may exist; the token must not be printed either way
    )


def test_token_script_fills_both_values_without_printing_the_token(tmp_path):
    local = write_env(tmp_path, "# keep\nTELEGRAM_CHAT_ID=42\nRENEW_URL=\nRENEW_TOKEN=\n")
    proc = run_token_script(local)
    assert proc.returncode == 0, proc.stderr
    text = local.read_text()
    token = next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith("RENEW_TOKEN="))
    assert len(token) >= 43 and token not in proc.stdout + proc.stderr
    assert "RENEW_URL=http://yarikos.local:8080\n" in text and "# keep\nTELEGRAM_CHAT_ID=42\n" in text
    assert local.stat().st_mode & 0o777 == 0o600
    cfg = load_config(local)  # valid as written
    assert cfg.renew_token == token


def test_token_script_rotates_and_keeps_a_custom_url(tmp_path):
    local = write_env(tmp_path, f"RENEW_URL=http://10.0.0.85:9000\nRENEW_TOKEN={TOKEN}\n")
    run_token_script(local)
    text = local.read_text()
    assert "RENEW_URL=http://10.0.0.85:9000\n" in text and TOKEN not in text


def test_token_script_needs_the_keys(tmp_path):
    local = write_env(tmp_path, "TELEGRAM_CHAT_ID=42\n")
    proc = run_token_script(local)
    assert proc.returncode != 0 and "make config" in proc.stderr


# --------------------------------------------------------------------------- the extension


def test_extension_asks_for_as_little_as_possible():
    manifest = json.loads((ROOT / "extension" / "manifest.json").read_text())
    assert manifest["manifest_version"] == 3
    assert sorted(manifest["permissions"]) == ["cookies", "storage"]
    assert manifest["host_permissions"] == ["https://ais.usvisa-info.com/*", "http://yarikos.local/*"]
    assert "<all_urls>" not in json.dumps(manifest)
    for name in ("popup.html", "popup.js", "options.html", "options.js", "style.css"):
        assert (ROOT / "extension" / name).exists()
    popup = (ROOT / "extension" / "popup.js").read_text()
    assert "innerHTML" not in popup  # network text is only ever set as textContent
