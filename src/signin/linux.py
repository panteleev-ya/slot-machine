#!/usr/bin/env python3
"""Renew the visa-site session from Google Chrome on Linux, with no physical screen.

Chrome is the real Google Chrome, drawing into a virtual screen (Xvfb), not
Chrome's headless mode. The same sign-in steps as the Mac driver run: open the
form, fill it, follow Continue, and post the session to the Pi. Nothing is
booked or rescheduled.

    src/signin/docker/run.sh                 # start Chrome, do not open the visa site
    src/signin/docker/run.sh --once          # one sign-in, then exit
    src/signin/docker/run.sh --every 50 --jitter 3

`python -m signin` selects this driver on Linux. --check is the container's
default when no arguments are passed. The visa site is opened only for a real cycle.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from signin import cdp as cdp_client
from signin import mac

DEBUG_PORT = 9222
CHECK_JS = r"""
JSON.stringify({
  webdriver: navigator.webdriver === true,
  ua: navigator.userAgent,
  headless: /HeadlessChrome/.test(navigator.userAgent),
  platform: navigator.platform
})
"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m signin",
        description="Renew the visa-site session from Linux Chrome.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="start Chrome on the virtual screen and exit before opening the visa site",
    )
    parser.add_argument("--once", action="store_true", help="sign in once and exit")
    parser.add_argument(
        "--tries", type=int, default=mac.TRIES, help=f"sign-in attempts per cycle (default {mac.TRIES})"
    )
    parser.add_argument(
        "--every", type=float, default=mac.EVERY_MINUTES, help="minutes between renewals (default 12)"
    )
    parser.add_argument(
        "--jitter",
        type=float,
        default=mac.JITTER_MINUTES,
        help="random ± minutes added to --every (default 0)",
    )
    parser.add_argument(
        "--credentials", type=Path, default=mac.CREDENTIALS, help="AIS_EMAIL / AIS_PASSWORD file"
    )
    parser.add_argument(
        "--settings", type=Path, default=mac.SETTINGS, help="slot-machine.env with RENEW_URL and RENEW_TOKEN"
    )
    return parser.parse_args(argv)


def allowed_page(url: str) -> bool:
    """The check may open a blank tab. Every other navigation stays on the visa site."""
    if url == "about:blank":
        return True
    parsed = urllib.parse.urlsplit(url)
    return parsed.scheme == "https" and parsed.hostname == "ais.usvisa-info.com"


def browser_problem(info: dict) -> str | None:
    """Why this Chrome would be a poor stand-in for a normal mini-PC browser."""
    agent = str(info.get("ua") or "")
    if info.get("webdriver") is True:
        return "navigator.webdriver is true, so this Chrome looks automated"
    if info.get("headless") is True or "HeadlessChrome" in agent:
        return "This Chrome is headless. The mini-PC test needs a real window on a virtual screen."
    if "Chrome/" not in agent:
        return "Chrome did not report a Chrome User-Agent"
    return None


def chrome_command(profile: Path, port: int) -> list[str]:
    """Headed Chrome. No automation switch, so navigator.webdriver stays unset."""
    binary = os.environ.get("CHROME_BIN", "google-chrome")
    command = [
        binary,
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        "--ozone-platform=x11",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--window-size=1280,800",
        "--window-position=0,0",
        "--lang=en-US",
        "about:blank",
    ]
    if os.environ.get("CHROME_NO_SANDBOX") == "1":
        command.insert(-1, "--no-sandbox")
    return command


def profile_dir() -> Path:
    path = Path(os.environ.get("CHROME_PROFILE", Path.home() / ".cache" / "slot-machine" / "linux-chrome"))
    path.mkdir(parents=True, exist_ok=True)
    cleared = False
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        leftover = path / name
        if leftover.exists() or leftover.is_symlink():
            leftover.unlink()
            cleared = True
    if cleared:
        mac._say("Cleared a leftover Chrome profile lock.")
    return path


def _chrome_log(profile: Path) -> str:
    path = profile / "chrome.log"
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[-800:].strip()


def _chrome_present(binary: str) -> bool:
    return Path(binary).is_file() or shutil.which(binary) is not None


def stop_chrome(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def start_chrome(profile: Path, port: int) -> subprocess.Popen:
    if not os.environ.get("DISPLAY"):
        raise mac.ProbeError("DISPLAY is not set. Start this through src/signin/docker/run.sh.")
    binary = os.environ.get("CHROME_BIN", "google-chrome")
    if not _chrome_present(binary):
        raise mac.ProbeError(f"Google Chrome is not installed ({binary}).")
    log = (profile / "chrome.log").open("ab")
    command = chrome_command(profile, port)
    mac._say("Starting Google Chrome on the virtual screen.")
    if "--no-sandbox" in command:
        mac._say("Chrome sandbox is off (CHROME_NO_SANDBOX=1). A mini-PC should leave the sandbox on.")
    else:
        mac._say("Chrome sandbox is on.")
    return subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)


def wait_for_chrome(port: int, proc: subprocess.Popen, profile: Path, seconds: float = 180) -> None:
    mac._say(f"Waiting up to {seconds:.0f}s for Chrome to open its debugging port.")
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            detail = _chrome_log(profile)
            hint = ""
            if "sandbox" in detail.lower() or "namespace" in detail.lower():
                hint = " Set CHROME_NO_SANDBOX=1 to retry. A mini-PC should not need that."
            tail = f" {detail.splitlines()[-1][:240]}" if detail else ""
            raise mac.ProbeError(f"Chrome exited before it could be controlled.{tail}{hint}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as response:
                json.load(response)
            return
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            time.sleep(0.5)
    raise mac.ProbeError("Chrome did not open its debugging port. The virtual screen may have failed.")


def connect_page(port: int) -> cdp_client.Client:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
                pages = json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            time.sleep(0.3)
            continue
        if isinstance(pages, list):
            for page in pages:
                if not isinstance(page, dict):
                    continue
                socket_url = page.get("webSocketDebuggerUrl")
                if page.get("type") == "page" and socket_url:
                    return cdp_client.Client(str(socket_url))
        time.sleep(0.3)
    raise mac.ProbeError("Chrome opened no page to control")


def evaluate(page: cdp_client.Client, source: str, timeout: float = 20) -> str:
    try:
        result = page.call(
            "Runtime.evaluate",
            {"expression": source, "returnByValue": True, "userGesture": True},
            timeout=timeout,
        )
    except cdp_client.CDPError as err:
        raise mac.ProbeError(str(err)) from err
    details = result.get("exceptionDetails")
    if isinstance(details, dict):
        text = details.get("text") or details.get("exception", {}).get("description") or "JavaScript failed"
        raise mac.ProbeError(str(text)[:300])
    value = (result.get("result") or {}).get("value")
    return "" if value is None else str(value)


def navigate(page: cdp_client.Client, url: str) -> None:
    if not allowed_page(url):
        raise mac.ProbeError("Refusing to open a URL outside the visa site")
    try:
        result = page.call("Page.navigate", {"url": url})
    except cdp_client.CDPError as err:
        if "did not answer" in str(err):  # still connecting: the site may be down
            raise mac.SiteUnreachable(f"Chrome is still waiting for {url}") from err
        raise mac.ProbeError(str(err)) from err
    error = result.get("errorText")
    if error:  # net::ERR_CONNECTION_TIMED_OUT and the like
        raise mac.SiteUnreachable(f"Chrome did not open the page ({error})")
    mac.pause_step("for the new page to settle")


def read_status(page: cdp_client.Client) -> dict:
    raw = evaluate(page, mac.STATUS_JS)
    if not raw:
        return {"url": "", "hasSignInForm": False, "challenge": False, "flash": "", "hasContinue": False}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as err:
        raise mac.ProbeError("Chrome returned something that is not the status JSON") from err
    if not isinstance(data, dict):
        raise mac.ProbeError("Chrome returned something that is not the status JSON")
    return data


def wait_until(page: cdp_client.Client, predicate, seconds: float, *, what: str) -> dict | None:
    mac._say(f"Waiting up to {seconds:.0f}s {what}.")
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        try:
            last = read_status(page)
        except mac.ProbeError as err:
            text = str(err).lower()
            if not any(word in text for word in ("navigat", "destroyed", "closed", "did not answer")):
                raise
            last = None
        if last is not None and predicate(last):
            return last
        time.sleep(1)
    return last


def session_cookie(page: cdp_client.Client) -> str | None:
    """The live _yatri_session. DevTools can see it before Chrome has flushed the cookie file."""
    try:
        result = page.call("Network.getCookies", {"urls": ["https://ais.usvisa-info.com/"]})
    except cdp_client.CDPError as err:
        raise mac.ProbeError(str(err)) from err
    cookies = result.get("cookies")
    if not isinstance(cookies, list):
        return None
    for item in cookies:
        if not isinstance(item, dict) or item.get("name") != "_yatri_session":
            continue
        value = str(item.get("value") or "")
        if mac.cookie_ok(value):
            return value
    return None


def wait_for_signed_in_cookie(page: cdp_client.Client, avoid: str | None) -> str:
    mac._say(f"Waiting up to {mac.COOKIE_WAIT_S:.0f}s for Chrome to store the signed-in session.")
    deadline = time.monotonic() + mac.COOKIE_WAIT_S
    while True:
        found = session_cookie(page)
        if found and found != avoid:
            mac.pause_step("and reading that session once more")
            again = session_cookie(page)
            return again or found
        if time.monotonic() >= deadline:
            raise mac.ProbeError("Chrome has not stored the signed-in session yet. Not sending.")
        time.sleep(1)


def _click_navigated(err: mac.ProbeError) -> bool:
    text = str(err).lower()
    return any(word in text for word in ("navigat", "destroyed", "closed", "did not answer"))


def fill_and_submit(page: cdp_client.Client, email: str, password: str) -> None:
    raw = evaluate(page, mac.build_fill_js(email, password), timeout=15)
    try:
        result = json.loads(raw) if raw else {}
    except json.JSONDecodeError as err:
        raise mac.ProbeError("Chrome returned something that is not the fill result") from err
    if not result.get("ok"):
        raise mac.ProbeError(str(result.get("error") or "could not fill the sign-in form"))
    mac.pause_step("before clicking Sign In")
    try:
        evaluate(page, mac.SUBMIT_JS, timeout=8)
    except mac.ProbeError as err:
        if not _click_navigated(err):
            raise


def click_continue(page: cdp_client.Client) -> None:
    mac.pause_step("before following Continue")
    try:
        evaluate(page, mac.CONTINUE_JS, timeout=8)
    except mac.ProbeError as err:
        if not _click_navigated(err):
            raise


def browser_user_agent(page: cdp_client.Client) -> str:
    raw = evaluate(page, mac.UA_JS, timeout=15)
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as err:
        raise mac.ProbeError("Chrome did not report its User-Agent") from err
    agent = str(data.get("ua") or "")
    if not (10 <= len(agent) <= 512) or not all(" " <= char <= "~" for char in agent):
        raise mac.ProbeError("Chrome did not report its User-Agent")
    return agent


def save_failure_shot(page: cdp_client.Client) -> None:
    directory = os.environ.get("SHOTS_DIR", "")
    if not directory:
        return
    try:
        result = page.call("Page.captureScreenshot", {"format": "png"}, timeout=10)
        png = base64.b64decode(str(result.get("data") or ""))
        if not png:
            return
        path = Path(directory) / "last.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png)
        mac._say(f"Saved a picture of the Chrome window to {path}")
    except (mac.ProbeError, cdp_client.CDPError, OSError, ValueError) as err:
        mac._say(f"Could not save a picture of the window ({err}).")


def _after_sign_in(page: cdp_client.Client, status: dict | None) -> int:
    if status and status.get("challenge"):
        mac._say("A captcha puzzle is on screen. This probe will not click it.")
        save_failure_shot(page)
        return 1
    kind = mac._kind(status) if status else mac.KIND_OTHER
    if kind == mac.KIND_APPOINTMENT:
        mac._say(f"Reached the appointment page: {status.get('url')}")
        return 0
    if kind == mac.KIND_SIGNED_IN and status and status.get("hasContinue"):
        mac._say("Signed in. Following the Continue link to the schedule, and nothing else.")
        click_continue(page)
        status = wait_until(
            page,
            lambda item: mac._kind(item) == mac.KIND_APPOINTMENT,
            mac.AFTER_SUBMIT_S,
            what="for the appointment page",
        )
        if status and mac._kind(status) == mac.KIND_APPOINTMENT:
            mac._say(f"Reached the appointment page: {status.get('url')}")
            return 0
        url = (status or {}).get("url") or "unknown"
        mac._say(f"Signed in. The Continue link did not open the schedule page: {url}")
        save_failure_shot(page)
        return 2
    if kind == mac.KIND_SIGNED_IN:
        url = (status or {}).get("url") or "unknown"
        mac._say(f"Signed in, but there was no Continue link: {url}")
        mac._say("Nothing else was clicked.")
        save_failure_shot(page)
        return 2
    return -1


def run_attempt(
    page: cdp_client.Client, email: str, password: str, number: int, tries: int
) -> tuple[int, str | None]:
    mac._say(f"try {number}/{tries}: opening the sign-in page")
    navigate(page, mac.SIGN_IN_URL)
    status = wait_until(
        page,
        lambda item: bool(item.get("hasSignInForm"))
        or bool(item.get("challenge"))
        or mac._left_sign_in(item),
        mac.FORM_WAIT_S,
        what="for the sign-in form",
    )
    if status and mac._left_sign_in(status) and not status.get("hasSignInForm"):
        mac._say(f"try {number}/{tries}: this Chrome is already signed in, so the form was not submitted")
        return _after_sign_in(page, status), None
    if not status or not status.get("hasSignInForm"):
        # no more tries: pings of the home page tell whether the site is down (mac.renew_loop)
        raise mac.not_loaded(f"try {number}/{tries}: the sign-in form did not appear", status)
    if status.get("challenge"):
        mac._say("A captcha puzzle is on screen. This probe will not click it.")
        save_failure_shot(page)
        return 1, None
    mac.pause_step("before noting the cookie already on the sign-in page")
    anonymous = session_cookie(page)
    mac._say(f"try {number}/{tries}: filling the form and clicking Sign In")
    fill_and_submit(page, email, password)
    status = wait_until(page, mac._ready_after_submit, mac.AFTER_SUBMIT_S, what="for the page after Sign In")
    code = _after_sign_in(page, status)
    if code < 0:
        mac._say(f"try {number}/{tries}: still on the sign-in page{mac._flash(status or {})}")
    return code, anonymous


def sign_out(page: cdp_client.Client) -> None:
    mac._say("Signing out so this cycle gets a new session.")
    clicked = False
    try:
        raw = evaluate(page, mac.SIGN_OUT_JS, timeout=8)
        clicked = bool(json.loads(raw).get("ok")) if raw else False
    except (mac.ProbeError, json.JSONDecodeError) as err:
        clicked = isinstance(err, mac.ProbeError) and _click_navigated(err)
        if not clicked:
            mac._say("No sign-out control on the page; opening the sign-out address.")
    if not clicked:
        navigate(page, mac.SIGN_OUT_URL)
    status = wait_until(page, mac._signed_out, 20, what="for sign-out to finish")
    if not status or not mac._signed_out(status):
        navigate(page, mac.SIGN_OUT_URL)
        status = wait_until(page, mac._signed_out, 20, what="for sign-out to finish")
    if not status or not mac._signed_out(status):
        if mac.page_problem(status):
            raise mac.not_loaded("Signing out did not load the next page", status)
        raise mac.ProbeError("Signing out did not leave the appointment page.")
    if status.get("hasSignInForm"):
        return
    mac._say("Signed out. Opening the sign-in page.")
    navigate(page, mac.SIGN_IN_URL)
    status = wait_until(
        page, lambda item: bool(item.get("hasSignInForm")), mac.FORM_WAIT_S, what="for the sign-in form"
    )
    if not status or not status.get("hasSignInForm"):
        raise mac.not_loaded("The sign-in form did not appear after signing out", status)


def send_session(page: cdp_client.Client, renew_url: str, token: str, avoid: str | None) -> str:
    mac.pause_step("before reading Chrome's session")
    agent = browser_user_agent(page)
    cookie = wait_for_signed_in_cookie(page, avoid)
    mac._say("Sending this Chrome's session to the Pi.")
    message = mac.post_session(renew_url, token, cookie, agent)
    mac._say(f"Pi: {message}")
    return cookie


def renew_cycle(
    page: cdp_client.Client,
    email: str,
    password: str,
    tries: int,
    renew_url: str,
    token: str,
    previous: str | None,
    *,
    fresh: bool,
) -> tuple[int, str | None]:
    if fresh:
        sign_out(page)
    code = -1
    avoid = previous
    for number in range(1, tries + 1):
        code, seen = run_attempt(page, email, password, number, tries)
        if seen:
            avoid = seen
        if code >= 0:
            break
    if code == 1:
        return 1, previous
    if code != 0:
        mac._say("This cycle did not reach the appointment page.")
        save_failure_shot(page)
        return -1, previous
    cookie = send_session(page, renew_url, token, avoid)
    return 0, cookie


def open_browser(port: int) -> tuple[subprocess.Popen, cdp_client.Client, Path]:
    profile = profile_dir()
    proc = start_chrome(profile, port)
    try:
        wait_for_chrome(port, proc, profile)
        page = connect_page(port)
    except mac.ProbeError:
        stop_chrome(proc)
        raise
    return proc, page, profile


def run_check(port: int = DEBUG_PORT) -> int:
    mac._say("Checking Linux Chrome on a virtual screen. The visa site stays closed.")
    proc, page, _profile = open_browser(port)
    try:
        navigate(page, "about:blank")
        raw = evaluate(page, CHECK_JS)
        try:
            info = json.loads(raw)
        except json.JSONDecodeError as err:
            raise mac.ProbeError("Chrome did not report its browser details") from err
        if not isinstance(info, dict):
            raise mac.ProbeError("Chrome did not report its browser details")
        problem = browser_problem(info)
        if problem:
            raise mac.ProbeError(problem)
        token = "probe-roundtrip-0123456789"
        page.call(
            "Network.setCookie",
            {"name": "probe_roundtrip", "value": token, "url": "https://example.com/"},
        )
        stored = page.call("Network.getCookies", {"urls": ["https://example.com/"]})
        cookies = stored.get("cookies") if isinstance(stored, dict) else None
        found = isinstance(cookies, list) and any(
            isinstance(item, dict) and item.get("name") == "probe_roundtrip" and item.get("value") == token
            for item in cookies
        )
        if not found:
            raise mac.ProbeError("Chrome DevTools could not read a cookie it just stored")
        mac._say("Check passed. This Chrome is a normal window, not headless.")
        mac._say("navigator.webdriver is false.")
        mac._say(f"User-Agent: {info.get('ua')}")
        mac._say(f"Platform: {info.get('platform')}")
        mac._say("A test cookie round-tripped through DevTools. The visa site was not opened.")
        return 0
    finally:
        page.close()
        stop_chrome(proc)


def run_loop(args: argparse.Namespace, port: int = DEBUG_PORT) -> int:
    if args.tries < 1:
        raise mac.ProbeError("--tries must be at least 1")
    email, password = mac.load_credentials(args.credentials)
    renew_url, token = mac.load_renew_settings(args.settings)
    mac.cycle_seconds(args.every, args.jitter)
    pace = f"every {args.every:g} min" if args.jitter == 0 else f"every {args.every:g} min ± {args.jitter:g}"
    mac._say("Linux stand-in for a mini-PC: Google Chrome on a virtual screen, not headless.")
    mac._say(
        f"Renewing {pace}. The first cycle uses the current Chrome session; later cycles sign out and sign in."
    )
    proc, page, _profile = open_browser(port)
    previous: str | None = None

    def cycle(fresh: bool) -> int:
        nonlocal previous
        try:
            code, previous = renew_cycle(
                page, email, password, args.tries, renew_url, token, previous, fresh=fresh
            )
        except cdp_client.CDPError as err:
            save_failure_shot(page)
            raise mac.ProbeError(str(err)) from err
        except mac.ProbeError:
            save_failure_shot(page)
            raise
        return code

    try:
        return mac.renew_loop(
            cycle,
            every=args.every,
            jitter=args.jitter,
            renew_url=renew_url,
            token=token,
            user_agent=lambda: browser_user_agent(page),
            once=args.once,
            alive=lambda: proc.poll() is None,
        )
    finally:
        page.close()
        stop_chrome(proc)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.check:
        return run_check()
    return run_loop(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (mac.ProbeError, cdp_client.CDPError) as err:
        print(f"error: {err}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nStopped. The container exits and Chrome stops with it.", file=sys.stderr)
        sys.exit(130)
