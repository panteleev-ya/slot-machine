#!/usr/bin/env python3
"""Keep a visa-site session alive from the Mac Chrome you already use.

Opens a visible window in the running Google Chrome, signs in (three tries),
follows Continue, and posts `_yatri_session` plus that Chrome's User-Agent to
the Pi's renewal page, the same POST the extension sends. Then it waits and
does it again. The first cycle uses the session already in Chrome. Every later
cycle signs out first, so the Pi receives a new session.

It does not book or reschedule. The cookie and the renewal token are never printed.

    PYTHONPATH=src python3 -m signin --init
    # edit config/ais-login.env (AIS_EMAIL, AIS_PASSWORD), chmod 600
    PYTHONPATH=src python3 -m signin                  # every 12 min
    PYTHONPATH=src python3 -m signin --every 50 --jitter 3

On Linux, including the Docker image, `python -m signin` drives Chrome on a
virtual screen instead. See src/signin/docker/run.sh.

The first Mac run asks macOS to control Google Chrome, and to read the
"Chrome Safe Storage" keychain item so the HttpOnly cookie can be sent.
Allow both. Ctrl+C stops the loop and leaves the Chrome window as it is.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from slot_machine import sitewatch
from slot_machine.config import DEFAULT_UA
from slot_machine.timeutil import fmt_dur, parse_ts

SIGN_IN_URL = "https://ais.usvisa-info.com/en-kz/niv/users/sign_in"
SIGN_OUT_URL = "https://ais.usvisa-info.com/en-kz/niv/users/sign_out"
HOME_URL = sitewatch.home_url("https://ais.usvisa-info.com", "en-kz")  # what pings ask for
CHROME_APP = Path("/Applications/Google Chrome.app")
CHROME_ROOT = Path.home() / "Library/Application Support/Google/Chrome"
REPO_ROOT = Path(__file__).resolve().parents[2]
CREDENTIALS = REPO_ROOT / "config" / "ais-login.env"
SETTINGS = REPO_ROOT / "config" / "slot-machine.env"
TRIES = 3
EVERY_MINUTES = 12  # quick test; --every 50 --jitter 3 is the overnight pace
JITTER_MINUTES = 0
RETRY_MINUTES = 2  # after a failed cycle; doubles with each failure in a row, up to RETRY_MAX_MINUTES
RETRY_MAX_MINUTES = 60
PI_POLL_MINUTES = 5  # how often to ask again while the Pi doesn't answer
FORM_WAIT_S = 30
AFTER_SUBMIT_S = 25
APPLESCRIPT_TIMEOUT_S = 20
COOKIE_WAIT_S = 30
STEP_MIN_S = 1.0
STEP_MAX_S = 3.0
STEP_JITTER_S = 0.3

# Returned by the page. "appointment" is the schedule page after Continue.
# "signed_in" has left the form but is not that page yet. The sign-in page sets
# _yatri_session immediately, so the cookie is not a signal and is not read here.
KIND_SIGN_IN = "sign_in"
KIND_APPOINTMENT = "appointment"
KIND_SIGNED_IN = "signed_in"
KIND_OTHER = "other"


class ProbeError(Exception):
    """A setup or Chrome-control failure. The message never includes the password."""


class PiUnreachable(ProbeError):
    """The Pi's renewal page didn't answer. A session signed in now would have nowhere to go."""


class SiteUnreachable(ProbeError):
    """A visa-site page didn't load, the way it doesn't when the site is down. Pings decide."""


def display_path(path: Path) -> str:
    """A repo-relative path when the file lives in this checkout."""
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def parse_env(text: str) -> dict[str, str]:
    values = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_credentials(path: Path) -> tuple[str, str]:
    if not path.is_file():
        raise ProbeError(f"Missing {display_path(path)}. Run: python -m signin --init")
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise ProbeError(
            f"{display_path(path)} is readable by others (mode {mode:o}). Run: chmod 600 {display_path(path)}"
        )
    data = parse_env(path.read_text(encoding="utf-8"))
    email = data.get("AIS_EMAIL", "").strip()
    password = data.get("AIS_PASSWORD", "")
    if not email or not password:
        raise ProbeError(f"Set AIS_EMAIL and AIS_PASSWORD in {display_path(path)}")
    return email, password


def init_credentials(path: Path) -> None:
    if path.exists():
        raise ProbeError(f"{display_path(path)} already exists; leaving it untouched")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("# Visa-site sign-in for python -m signin. chmod 600. git-ignored.\n")
        fh.write("AIS_EMAIL=\n")
        fh.write("AIS_PASSWORD=\n")
    shown = display_path(path)
    print(f"Wrote {shown} (mode 600). Fill in the two values, then run the probe again.")


def page_kind(url: str, has_sign_in_form: bool) -> str:
    """What this URL is worth. The sign-in form wins, even if a redirect mentions the schedule."""
    if has_sign_in_form or "sign_in" in url:
        return KIND_SIGN_IN
    if "/niv/schedule/" in url or "/appointment" in url:
        return KIND_APPOINTMENT
    if "/niv/" in url:
        return KIND_SIGNED_IN
    return KIND_OTHER


def private_file(path: Path) -> None:
    if not path.is_file():
        raise ProbeError(f"Missing {display_path(path)}")
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise ProbeError(
            f"{display_path(path)} is readable by others (mode {mode:o}). Run: chmod 600 {display_path(path)}"
        )


def load_renew_settings(path: Path) -> tuple[str, str]:
    """Pi address and bearer token. The token stays in memory and is never printed."""
    private_file(path)
    data = parse_env(path.read_text(encoding="utf-8"))
    url = data.get("RENEW_URL", "").strip().rstrip("/")
    token = data.get("RENEW_TOKEN", "").strip()
    if not url or not token:
        raise ProbeError(f"Set RENEW_URL and RENEW_TOKEN in {display_path(path)} (make renew-token)")
    if not url.startswith("http://"):
        raise ProbeError("RENEW_URL must be the Pi's http address")
    return url, token


def step_delay(rng=None) -> float:
    """A short pause between actions: 1–3 seconds, then ±0.3s."""
    draw = rng or random.uniform
    return max(0.2, draw(STEP_MIN_S, STEP_MAX_S) + draw(-STEP_JITTER_S, STEP_JITTER_S))


def pause_step(why: str) -> None:
    """A short pause, announced so a quiet terminal is not a hang."""
    seconds = step_delay()
    _say(f"Waiting {seconds:.1f}s {why}.")
    time.sleep(seconds)


def cycle_seconds(every_minutes: float, jitter_minutes: float, rng=None) -> float:
    """How long to wait after a successful send. Jitter is symmetric, in minutes."""
    if every_minutes <= 0:
        raise ProbeError("--every must be positive")
    if jitter_minutes < 0 or jitter_minutes >= every_minutes:
        raise ProbeError("--jitter must be zero or a positive number smaller than --every")
    draw = rng or random.uniform
    delta = draw(-jitter_minutes, jitter_minutes) if jitter_minutes else 0.0
    return (every_minutes + delta) * 60


def cookie_ok(value: str) -> bool:
    return len(value) >= 20 and not any(c.isspace() for c in value) and value.isascii()


def session_body(cookie: str, user_agent: str) -> bytes:
    if not cookie_ok(cookie):
        raise ProbeError("the session cookie does not look usable")
    if not (10 <= len(user_agent) <= 512) or not all(" " <= char <= "~" for char in user_agent):
        raise ProbeError("the browser User-Agent is not usable")
    return json.dumps({"cookie": cookie, "user_agent": user_agent}).encode()


def chrome_key_from_password(password: str) -> bytes:
    """Key Chrome uses on macOS for v10 cookie values (PBKDF2 of the Safe Storage secret)."""
    return hashlib.pbkdf2_hmac("sha1", password.encode(), b"saltysalt", 1003, 16)


def decrypt_chrome_cookie(encrypted: bytes, key: bytes) -> str:
    """Turn a Chrome `encrypted_value` into the cookie text. Never includes it in errors."""
    if not encrypted.startswith(b"v10"):
        raise ProbeError("Chrome's session cookie uses an encryption this probe does not know")
    proc = subprocess.run(
        [
            "openssl",
            "enc",
            "-d",
            "-aes-128-cbc",
            "-K",
            key.hex(),
            "-iv",
            (b" " * 16).hex(),
        ],
        input=encrypted[3:],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise ProbeError("Could not decrypt the session cookie")
    plain = proc.stdout
    candidates = [plain, plain[32:]] if len(plain) > 32 else [plain]
    for candidate in candidates:
        if all(32 <= byte < 127 for byte in candidate) and cookie_ok(candidate.decode("ascii")):
            return candidate.decode("ascii")
    raise ProbeError("Could not decrypt the session cookie")


def applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _js_file(source: str) -> Path:
    directory = CREDENTIALS.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="signin-probe-", suffix=".js", dir=directory)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(source)
    return Path(name)


def run_osascript(source: str, env: dict[str, str] | None = None, timeout: int | None = None) -> str:
    """Run AppleScript. The password is never placed in argv; callers pass it inside a mode-600 file."""
    merged = os.environ.copy()
    if env:
        merged.update(env)
    try:
        proc = subprocess.run(
            ["osascript", "-"],
            input=source,
            text=True,
            capture_output=True,
            env=merged,
            timeout=APPLESCRIPT_TIMEOUT_S + 5 if timeout is None else timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as err:
        raise ProbeError("Chrome did not answer AppleScript in time") from err
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ProbeError(_chrome_error(detail))
    return proc.stdout.strip()


def _chrome_error(detail: str) -> str:
    lowered = detail.lower()
    if (
        "javascript through applescript is turned off" in lowered
        or "allow javascript from apple events" in lowered
    ):
        return (
            "Chrome refused AppleScript. Turn on View → Developer → "
            "Allow JavaScript from Apple Events, then run this again."
        )
    if "assistive" in lowered or "accessibility" in lowered:
        return (
            "macOS blocked clicking the alert. Allow this terminal under "
            "System Settings → Privacy & Security → Accessibility."
        )
    if "system events" in lowered and ("not authorized" in lowered or "-1743" in detail):
        return (
            "macOS blocked clicking the alert. Allow this terminal to control "
            "System Events (Privacy & Security → Automation)."
        )
    if "not authorized" in lowered or "-1743" in detail:
        return (
            "macOS blocked control of Chrome. Run the probe again and allow "
            "the automation prompt (System Settings → Privacy & Security → Automation)."
        )
    if "timed out" in lowered or "-1712" in detail:
        return "AppleEvent timed out"
    if detail:
        return f"Chrome control failed: {detail.splitlines()[0][:300]}"
    return "Chrome control failed"


OPEN_WINDOW = """
tell application "Google Chrome"
  activate
  set probeWindow to make new window
  set URL of active tab of probeWindow to __URL__
  return id of probeWindow
end tell
"""

EVAL_JS = """
with timeout of __TIMEOUT__ seconds
  tell application "Google Chrome"
    set jsPath to system attribute "PROBE_JS_FILE"
    set jsSource to read (POSIX file jsPath) as «class utf8»
    return execute (active tab of (window id __WINDOW__)) javascript jsSource
  end tell
end timeout
"""

# A native alert() blocks the page until OK is clicked. Arming replaces it before we
# click Sign In; the System Events script covers an alert that is already on screen.
DIALOG_JS = r"""
function probeArmDialogs() {
  if (window.__probeDialogsArmed) return;
  window.__probeDialogs = window.__probeDialogs || [];
  const note = (msg) => {
    const text = String(msg ?? "").replace(/\s+/g, " ").trim().slice(0, 200);
    if (text) window.__probeDialogs.push(text);
  };
  window.alert = note;
  window.confirm = (msg) => { note(msg); return true; };
  window.prompt = (msg) => { note(msg); return ""; };
  window.__probeDialogsArmed = true;
}
function probeClickPageOk() {
  const nodes = [...document.querySelectorAll("[role='dialog'], .modal.show, .modal.in, .ui-dialog")];
  for (const node of nodes) {
    const box = node.getBoundingClientRect();
    if (box.width < 40 || box.height < 40) continue;
    const button = [...node.querySelectorAll("button, input[type='button'], a")].find((el) =>
      /^ok$/i.test((el.innerText || el.value || "").trim())
    );
    if (!button) continue;
    const message = (node.innerText || "").replace(/\s+/g, " ").trim().slice(0, 200);
    window.__probeDialogs = window.__probeDialogs || [];
    if (message) window.__probeDialogs.push(message);
    button.click();
    return true;
  }
  return false;
}
function probeDialogFlash(flash) {
  const extra = (window.__probeDialogs || []).join(" | ");
  return [flash, extra].filter(Boolean).join(" | ").slice(0, 300);
}
"""

# The groups page shows a green Continue button. It is often a submit input, not an
# <a href=".../continue_actions">. Only that label is clicked, and only on this site.
CONTINUE_FIND_JS = r"""
function probeContinueControl() {
  const onSite = (url) => !url || String(url).startsWith("https://ais.usvisa-info.com/");
  const labelOf = (el) => (el.innerText || el.value || "").replace(/\s+/g, " ").trim();
  const controls = [...document.querySelectorAll("a, button, input[type='submit'], input[type='button']")];
  const labeled = controls.find((el) => {
    if (!/^continue$/i.test(labelOf(el))) return false;
    if (el.href && !onSite(el.href)) return false;
    const form = el.closest("form");
    if (form && form.action && !onSite(form.action)) return false;
    return true;
  });
  if (labeled) return labeled;
  return controls.find((el) => {
    const href = el.href || (el.form && el.form.action) || "";
    if (!href.startsWith("https://ais.usvisa-info.com/") || !href.includes("continue_actions")) return false;
    return !/reschedule/i.test(labelOf(el));
  }) || null;
}
"""

STATUS_JS = (
    DIALOG_JS
    + CONTINUE_FIND_JS
    + r"""
(() => {
  probeArmDialogs();
  probeClickPageOk();
  const email = document.querySelector("#user_email, input[name='user[email]']");
  const frames = [...document.querySelectorAll("iframe")];
  const challenge = frames.some((frame) => {
    const src = frame.src || "";
    const title = (frame.title || "").toLowerCase();
    const recaptcha = src.includes("recaptcha") || src.includes("hcaptcha") || title.includes("captcha");
    if (!recaptcha) return false;
    if (src.includes("bframe") || src.includes("/captcha") || title.includes("challenge")) return true;
    const box = frame.getBoundingClientRect();
    return box.width > 300 && box.height > 300;
  });
  const flash = [...document.querySelectorAll(".alert, .alert-danger, .error, #error_explanation, [role=alert]")]
    .map((el) => (el.innerText || "").trim())
    .filter(Boolean)
    .slice(0, 3)
    .join(" | ")
    .slice(0, 300);
  const cont = probeContinueControl();
  return JSON.stringify({
    url: location.href,
    hasSignInForm: Boolean(email),
    challenge,
    flash: probeDialogFlash(flash),
    title: (document.title || "").slice(0, 120),
    hasContinue: Boolean(cont),
    httpStatus: (performance.getEntriesByType("navigation")[0] || {}).responseStatus || 0,
    netError: ((document.querySelector(".error-code") || {}).textContent || "").trim().slice(0, 60),
  });
})()
"""
)

FILL_JS = (
    DIALOG_JS
    + r"""
(() => {
  probeArmDialogs();
  probeClickPageOk();
  const emailValue = __EMAIL_JSON__;
  const passwordValue = __PASSWORD_JSON__;
  const form = document.querySelector("form[action*='sign_in'], form#new_user");
  const email = document.querySelector("#user_email, input[name='user[email]']");
  const password = document.querySelector("#user_password, input[name='user[password]']");
  if (!form || !email || !password) {
    return JSON.stringify({ ok: false, error: "sign-in form is missing" });
  }
  const setValue = (el, value) => {
    const desc = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
    desc.set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  };
  setValue(email, emailValue);
  setValue(password, passwordValue);
  const boxes = [...form.querySelectorAll("input[type='checkbox']")];
  if (boxes.length === 0) {
    return JSON.stringify({ ok: false, error: "sign-in form has no agreement checkbox" });
  }
  for (const box of boxes) {
    if (box.checked) continue;
    const label = box.id ? document.querySelector(`label[for='${CSS.escape(box.id)}']`) : null;
    const widget = box.closest(".icheckbox, .icheck");
    if (label) label.click();
    if (!box.checked && widget) widget.click();
    if (!box.checked) {
      box.checked = true;
      box.dispatchEvent(new Event("change", { bubbles: true }));
    }
  }
  const unticked = boxes.filter((box) => !box.checked).length;
  return JSON.stringify({
    ok: email.value.length > 0 && password.value.length > 0 && unticked === 0,
    error: unticked ? "agreement box stayed unticked" : "",
    emailFilled: email.value.length > 0,
    passwordFilled: password.value.length > 0,
  });
})()
"""
)

SUBMIT_JS = (
    DIALOG_JS
    + r"""
(() => {
  probeArmDialogs();
  probeClickPageOk();
  const form = document.querySelector("form[action*='sign_in'], form#new_user");
  const button = form && form.querySelector("input[type='submit'], button[type='submit']");
  if (!button) return JSON.stringify({ ok: false, error: "no sign-in button" });
  button.click();
  return JSON.stringify({ ok: true });
})()
"""
)

SIGN_OUT_JS = r"""
(() => {
  const link = [...document.querySelectorAll("a")].find((el) => (el.href || "").includes("sign_out"));
  if (!link) return JSON.stringify({ ok: false });
  link.click();
  return JSON.stringify({ ok: true });
})()
"""

UA_JS = r"""
JSON.stringify({ ua: navigator.userAgent })
"""

CONTINUE_JS = (
    DIALOG_JS
    + CONTINUE_FIND_JS
    + r"""
(() => {
  const link = probeContinueControl();
  if (!link) return JSON.stringify({ ok: false, error: "no continue link" });
  link.click();
  return JSON.stringify({ ok: true });
})()
"""
)


def build_fill_js(email: str, password: str) -> str:
    return FILL_JS.replace("__EMAIL_JSON__", json.dumps(email)).replace(
        "__PASSWORD_JSON__", json.dumps(password)
    )


def _applescript_url(url: str) -> str:
    if not url.startswith("https://ais.usvisa-info.com/"):
        raise ProbeError("Refusing to open a URL outside the visa site")
    return applescript_string(url)


def open_sign_in_window() -> str:
    if not CHROME_APP.exists():
        raise ProbeError(f"Google Chrome is not installed at {CHROME_APP}")
    raw = run_osascript(OPEN_WINDOW.replace("__URL__", _applescript_url(SIGN_IN_URL)))
    window_id = raw.strip()
    if not window_id or window_id == "missing value":
        raise ProbeError(f"Chrome did not return a window id ({raw!r})")
    return window_id


def chrome_js(window_id: str, source: str, timeout: int = APPLESCRIPT_TIMEOUT_S) -> str:
    path = _js_file(source)
    script = EVAL_JS.replace("__TIMEOUT__", str(int(timeout))).replace(
        "__WINDOW__", applescript_string(window_id)
    )
    try:
        return run_osascript(script, {"PROBE_JS_FILE": str(path)})
    finally:
        path.unlink(missing_ok=True)


def set_tab_url(window_id: str, url: str) -> None:
    run_osascript(
        'tell application "Google Chrome"\n'
        f"  set URL of active tab of window id {applescript_string(window_id)} to {_applescript_url(url)}\n"
        "end tell\n"
    )
    pause_step("for the new page to settle")


def browser_user_agent(window_id: str) -> str:
    raw = chrome_js(window_id, UA_JS, timeout=15)
    try:
        data = json.loads(raw) if raw and raw != "missing value" else {}
    except json.JSONDecodeError as err:
        raise ProbeError("Chrome did not report its User-Agent") from err
    agent = str(data.get("ua") or "")
    if not (10 <= len(agent) <= 512) or not all(" " <= char <= "~" for char in agent):
        raise ProbeError("Chrome did not report its User-Agent")
    return agent


def _cookie_file(profile: Path) -> Path | None:
    for candidate in (profile / "Network" / "Cookies", profile / "Cookies"):
        if candidate.is_file():
            return candidate
    return None


def _query_cookie(db: Path, *, nolock: bool = False) -> tuple[int, str, bytes] | None:
    extra = "&nolock=1" if nolock else ""
    uri = "file:" + urllib.parse.quote(db.as_posix(), safe="/") + "?mode=ro" + extra
    conn = sqlite3.connect(uri, uri=True)
    try:
        row = conn.execute(
            """
            SELECT last_access_utc, value, encrypted_value
            FROM cookies
            WHERE name = '_yatri_session' AND host_key LIKE '%ais.usvisa-info.com'
            ORDER BY last_access_utc DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    access, value, encrypted = row
    if isinstance(encrypted, memoryview):
        encrypted = encrypted.tobytes()
    return int(access or 0), value or "", bytes(encrypted or b"")


def _cookie_rows() -> list[tuple[int, str, bytes]]:
    """One row per profile. Reads the live store first; a copy is the fallback."""
    if not CHROME_ROOT.is_dir():
        raise ProbeError(f"Chrome profile data is not in {CHROME_ROOT}")
    rows = []
    copies: list[Path] = []
    try:
        for profile in CHROME_ROOT.iterdir():
            src = _cookie_file(profile) if profile.is_dir() else None
            if src is None:
                continue
            row = None
            # A just-set cookie often sits in the WAL until Chrome checkpoints it.
            wal = Path(str(src) + "-wal")
            if wal.is_file() and wal.stat().st_size > 0:
                dest = _copy_cookie_db(src)
                copies.append(dest)
                try:
                    row = _query_cookie(dest)
                except (OSError, sqlite3.Error) as err:
                    raise ProbeError("Could not read Chrome's cookie store.") from err
            if row is None:
                try:
                    row = _query_cookie(src, nolock=True)
                except sqlite3.Error:
                    row = None
            if row is not None:
                rows.append(row)
    finally:
        for dest in copies:
            dest.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(str(dest) + suffix).unlink(missing_ok=True)
    return rows


def _copy_cookie_db(src: Path) -> Path:
    CREDENTIALS.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="signin-probe-cookies-", suffix=".db", dir=CREDENTIALS.parent)
    os.close(fd)
    dest = Path(name)
    os.chmod(dest, 0o600)
    shutil.copy2(src, dest)
    os.chmod(dest, 0o600)
    for suffix in ("-wal", "-shm"):
        extra = Path(str(src) + suffix)
        if extra.is_file():
            copied = Path(str(dest) + suffix)
            shutil.copy2(extra, copied)
            os.chmod(copied, 0o600)
    return dest


def read_session_cookie() -> str:
    """The newest _yatri_session in any Chrome profile. The value is not logged."""
    rows = _cookie_rows()
    if not rows:
        raise ProbeError("No _yatri_session in the Chrome profiles yet")
    _access, value, encrypted = max(rows, key=lambda row: row[0])
    if cookie_ok(value):
        return value
    if not encrypted:
        raise ProbeError("The session cookie is empty")
    return decrypt_chrome_cookie(encrypted, _chrome_safe_storage_key())


def _chrome_safe_storage_key() -> bytes:
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage", "-a", "Chrome"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as err:
        raise ProbeError(
            "Keychain did not answer. Allow Chrome Safe Storage if a prompt is waiting."
        ) from err
    password = proc.stdout.strip()
    if proc.returncode != 0 or not password:
        raise ProbeError(
            "Can't read the Chrome Safe Storage key. Allow the keychain prompt if macOS showed one, then run again."
        )
    return chrome_key_from_password(password)


def cookie_or_none() -> str | None:
    try:
        return read_session_cookie()
    except ProbeError:
        return None


def wait_for_signed_in_cookie(avoid: str | None) -> str:
    """The session from after sign-in. The value set on the sign-in page itself is not sent."""
    _say(f"Waiting up to {COOKIE_WAIT_S:.0f}s for Chrome to store the signed-in session.")
    deadline = time.monotonic() + COOKIE_WAIT_S
    while True:
        found = cookie_or_none()
        if found and found != avoid:
            pause_step("and reading that session once more")
            again = cookie_or_none()
            return again or found
        if time.monotonic() >= deadline:
            raise ProbeError("Chrome has not stored the signed-in session yet. Not sending.")
        time.sleep(1)


def post_session(renew_url: str, token: str, cookie: str, user_agent: str) -> str:
    """POST /api/cookie. Returns the Pi's message. Raises ProbeError without the cookie or token."""
    request = urllib.request.Request(
        renew_url + "/api/cookie",
        data=session_body(cookie, user_agent),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as err:
        raise ProbeError(_pi_error(err)) from err
    except urllib.error.URLError as err:
        raise ProbeError(f"Can't reach the Pi at {renew_url}.") from err
    if not isinstance(payload, dict):
        raise ProbeError("The Pi answered something that is not JSON.")
    message = str(payload.get("message") or "The Pi answered without a message.")
    if not payload.get("ok"):
        raise ProbeError(message)
    return message


def pi_status(renew_url: str, token: str) -> dict:
    """GET /api/status: the Pi's view of the session. Raises PiUnreachable if the page
    doesn't answer, ProbeError if it refuses the token."""
    request = urllib.request.Request(renew_url + "/api/status", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as err:
        if err.code == 401:
            raise ProbeError(
                "The Pi refused RENEW_TOKEN. Run make push-config, or check the settings."
            ) from err
        raise PiUnreachable(_pi_error(err)) from err
    except (urllib.error.URLError, OSError, ValueError) as err:
        raise PiUnreachable(f"Can't reach the Pi at {renew_url}.") from err
    if not isinstance(payload, dict):
        raise PiUnreachable("The Pi answered something that is not JSON.")
    return payload


def _now() -> datetime:
    return datetime.now(UTC)


def wait_for_pi(renew_url: str, token: str) -> None:
    """Return once the Pi's renewal page answers. Signing in before that would be for nothing:
    the session would have nowhere to go, and the next cycle would sign in again."""
    said = None
    while True:
        try:
            pi_status(renew_url, token)
        except PiUnreachable as err:
            note = f"{err} Not signing in: the session would have nowhere to go."
        else:
            if said:
                _say("The Pi answers. Going on.")
            return
        if note != said:
            _say(f"{note} Asking again every {PI_POLL_MINUTES} min.")
            said = note
        _wait(PI_POLL_MINUTES * 60)


def retry_seconds(failures: int) -> float:
    """Wait after `failures` failed cycles in a row: 2, 4, 8, ... minutes, at most RETRY_MAX_MINUTES."""
    return min(RETRY_MAX_MINUTES, RETRY_MINUTES * 2 ** max(0, failures - 1)) * 60


def _pi_error(err: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(err.read().decode())
    except (json.JSONDecodeError, OSError):
        payload = {}
    if isinstance(payload, dict) and payload.get("message"):
        return str(payload["message"])
    return f"The Pi answered HTTP {err.code}."


def send_session(window_id: str, renew_url: str, token: str, avoid: str | None) -> str:
    pause_step("before reading Chrome's session")
    agent = browser_user_agent(window_id)
    cookie = wait_for_signed_in_cookie(avoid)
    _say("Sending this Chrome's session to the Pi.")
    message = post_session(renew_url, token, cookie, agent)
    _say(f"Pi: {message}")
    return cookie


def sign_out(window_id: str) -> None:
    """End the browser session so the next sign-in is a new cookie. Does not touch other controls."""
    _say("Signing out so this cycle gets a new session.")
    clicked = False
    try:
        raw = chrome_js(window_id, SIGN_OUT_JS, timeout=8)
        clicked = bool(json.loads(raw).get("ok")) if raw and raw != "missing value" else False
    except (ProbeError, json.JSONDecodeError) as err:
        clicked = isinstance(err, ProbeError) and "timed out" in str(err).lower()
        if not clicked:
            _say("No sign-out control on the page; opening the sign-out address.")
    if not clicked:
        set_tab_url(window_id, SIGN_OUT_URL)
    status = wait_until(window_id, _signed_out, 20, what="for sign-out to finish")
    if not status or not _signed_out(status):
        set_tab_url(window_id, SIGN_OUT_URL)
        status = wait_until(window_id, _signed_out, 20, what="for sign-out to finish")
    if not status or not _signed_out(status):
        if page_problem(status):
            raise not_loaded("Signing out did not load the next page", status)
        raise ProbeError("Signing out did not leave the appointment page.")
    if status.get("hasSignInForm"):
        return
    _say("Signed out. Opening the sign-in page.")
    set_tab_url(window_id, SIGN_IN_URL)
    status = wait_until(
        window_id, lambda page: bool(page.get("hasSignInForm")), FORM_WAIT_S, what="for the sign-in form"
    )
    if not status or not status.get("hasSignInForm"):
        raise not_loaded("The sign-in form did not appear after signing out", status)


def read_status(window_id: str) -> dict:
    raw = chrome_js(window_id, STATUS_JS, timeout=15)
    if not raw or raw == "missing value":
        return {"url": "", "hasSignInForm": False, "challenge": False, "flash": "", "hasContinue": False}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as err:
        raise ProbeError("Chrome returned something that is not the status JSON") from err
    if not isinstance(data, dict):
        raise ProbeError("Chrome returned something that is not the status JSON")
    return data


DISMISS_OK = """
tell application "System Events"
  if not (exists process "Google Chrome") then return ""
  tell process "Google Chrome"
    set dismissed to ""
    repeat with w in windows
      try
        repeat with s in sheets of w
          if (name of buttons of s) contains "OK" then
            try
              set dismissed to dismissed & (value of static text 1 of s) & " "
            end try
            click button "OK" of s
          end if
        end repeat
      end try
      try
        if (name of buttons of w) contains "OK" and (count of buttons of w) <= 3 then
          try
            set dismissed to dismissed & (value of static text 1 of w) & " "
          end try
          click button "OK" of w
        end if
      end try
    end repeat
    return dismissed
  end tell
end tell
"""

_skip_native_dismiss = False


def dismiss_native_ok() -> str:
    """Click a Chrome alert's OK button, if one is up. Skip further tries when macOS forbids it."""
    global _skip_native_dismiss
    if _skip_native_dismiss:
        return ""
    try:
        text = run_osascript(DISMISS_OK, timeout=8)
    except ProbeError as err:
        message = str(err)
        if "Accessibility" in message or "System Events" in message:
            _skip_native_dismiss = True
            _say(message)
            return ""
        if "timed out" in message.lower():
            return ""
        raise
    text = " ".join(text.split())
    if text and text != "missing value":
        _say(f"Dismissed an alert ({text[:200]})")
        return text
    return ""


def wait_until(window_id: str, predicate, seconds: float, pause: float = 1.0, *, what: str) -> dict | None:
    """Poll the page. `what` is the rest of the log line, such as 'for the sign-in form'."""
    _say(f"Waiting up to {seconds:.0f}s {what}.")
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        dismiss_native_ok()
        try:
            last = read_status(window_id)
        except ProbeError as err:
            if "timed out" not in str(err).lower():
                raise
            dismiss_native_ok()
            last = None
        if last is not None and predicate(last):
            return last
        time.sleep(pause)
    return last


def _kind(status: dict) -> str:
    return page_kind(str(status.get("url") or ""), bool(status.get("hasSignInForm")))


def _locale_home(url: str) -> bool:
    """The page sign-out redirects to: https://ais.usvisa-info.com/en-kz/niv."""
    path = urllib.parse.urlsplit(url).path.rstrip("/")
    return path.endswith("/niv")


def _signed_out(page: dict) -> bool:
    """Sign-out is done when the form is back or the window is on the locale home."""
    url = str(page.get("url") or "")
    return bool(page.get("hasSignInForm")) or "sign_in" in url or _locale_home(url)


def _ready_after_submit(page: dict) -> bool:
    """True once Continue is on the page, the schedule is open, or a captcha puzzle is up.

    A groups URL is signed-in immediately, before its Continue button has been drawn.
    Waiting only for the URL made the probe give up on that button.
    """
    if page.get("challenge"):
        return True
    if not _left_sign_in(page):
        return False
    if _kind(page) == KIND_APPOINTMENT:
        return True
    return bool(page.get("hasContinue"))


def _left_sign_in(page: dict) -> bool:
    """True once the window has an authenticated visa-site page, or a captcha puzzle is up.

    An empty or not-yet-loaded document is not signed-in: the sign-in page itself
    is still a /niv/ URL, and a blank tab during navigation must not count.
    The locale home after sign-out is /en-kz/niv, which is not an authenticated page.
    """
    if page.get("challenge"):
        return True
    url = str(page.get("url") or "")
    if "sign_in" in url or page.get("hasSignInForm") or _locale_home(url):
        return False
    return "/niv/" in url


def page_problem(status: dict | None) -> str:
    """What the window shows instead of a visa-site page, if it tells: Chrome's own error page
    (no connection), or a gateway error. "" when it doesn't tell."""
    status = status or {}
    if str(status.get("url") or "").startswith("chrome-error://"):
        return f"Chrome couldn't load it: {status.get('netError') or status.get('title') or 'no details'}"
    code = int(status.get("httpStatus") or 0)
    return f"HTTP {code}" if code >= 500 else ""


def not_loaded(what: str, status: dict | None) -> SiteUnreachable:
    problem = page_problem(status) or _flash(status or {}).strip(" ()")
    return SiteUnreachable(f"{what} ({problem})" if problem else what)


def _say(text: str) -> None:
    print(text, flush=True)


def _flash(status: dict) -> str:
    flash = str(status.get("flash") or "").replace("\n", " ").strip()
    return f" ({flash})" if flash else ""


def fill_and_submit(window_id: str, email: str, password: str) -> None:
    raw = chrome_js(window_id, build_fill_js(email, password), timeout=15)
    try:
        result = json.loads(raw) if raw and raw != "missing value" else {}
    except json.JSONDecodeError as err:
        raise ProbeError("Chrome returned something that is not the fill result") from err
    if not result.get("ok"):
        raise ProbeError(str(result.get("error") or "could not fill the sign-in form"))
    pause_step("before clicking Sign In")
    try:
        chrome_js(window_id, SUBMIT_JS, timeout=8)
    except ProbeError as err:
        # The click navigates away, or an alert() is holding the page, so AppleScript
        # often loses the call. A native OK button is clicked; an overridden alert is not.
        if "timed out" not in str(err).lower():
            raise
        dismiss_native_ok()


def click_continue(window_id: str) -> None:
    """Follow only the post-login Continue link. Never click Reschedule or a submit."""
    pause_step("before following Continue")
    try:
        chrome_js(window_id, CONTINUE_JS, timeout=8)
    except ProbeError as err:
        if "timed out" not in str(err).lower():
            raise


def _after_sign_in(window_id: str, status: dict | None) -> int:
    """The window is past the form. Follow Continue, or report where it stopped. -1 means try again."""
    if status and status.get("challenge"):
        _say("A captcha puzzle is on screen. The window is left open; this probe will not click it.")
        return 1
    kind = _kind(status) if status else KIND_OTHER
    if kind == KIND_APPOINTMENT:
        _say(f"Reached the appointment page: {status.get('url')}")
        return 0
    if kind == KIND_SIGNED_IN and status and status.get("hasContinue"):
        _say("Signed in. Following the Continue link to the schedule, and nothing else.")
        click_continue(window_id)
        status = wait_until(
            window_id,
            lambda page: _kind(page) == KIND_APPOINTMENT,
            AFTER_SUBMIT_S,
            what="for the appointment page",
        )
        if status and _kind(status) == KIND_APPOINTMENT:
            _say(f"Reached the appointment page: {status.get('url')}")
            return 0
        url = (status or {}).get("url") or "unknown"
        _say(f"Signed in. The Continue link did not open the schedule page: {url}")
        return 2
    if kind == KIND_SIGNED_IN:
        url = (status or {}).get("url") or "unknown"
        _say(f"Signed in, but there was no Continue link: {url}")
        _say("Nothing else was clicked. The window is still open.")
        return 2
    return -1


def run_attempt(window_id: str, email: str, password: str, number: int, tries: int) -> tuple[int, str | None]:
    """One sign-in. Returns (exit code, cookie on the form before submit). -1 means try again."""
    _say(f"try {number}/{tries}: opening the sign-in page")
    set_tab_url(window_id, SIGN_IN_URL)
    status = wait_until(
        window_id,
        lambda page: bool(page.get("hasSignInForm")) or bool(page.get("challenge")) or _left_sign_in(page),
        FORM_WAIT_S,
        what="for the sign-in form",
    )
    if status and _left_sign_in(status) and not status.get("hasSignInForm"):
        _say(f"try {number}/{tries}: this Chrome is already signed in, so the form was not submitted")
        return _after_sign_in(window_id, status), None
    if not status or not status.get("hasSignInForm"):
        # no more tries: pings of the home page tell whether the site is down (renew_loop)
        raise not_loaded(f"try {number}/{tries}: the sign-in form did not appear", status)
    if status.get("challenge"):
        _say("A captcha puzzle is on screen. The window is left open; this probe will not click it.")
        return 1, None
    # The sign-in page already has a _yatri_session. Signing in replaces it; that earlier value
    # must not be sent to the Pi.
    pause_step("before noting the cookie already on the sign-in page")
    anonymous = cookie_or_none()
    _say(f"try {number}/{tries}: filling the form and clicking Sign In")
    fill_and_submit(window_id, email, password)
    status = wait_until(window_id, _ready_after_submit, AFTER_SUBMIT_S, what="for the page after Sign In")
    code = _after_sign_in(window_id, status)
    if code < 0:
        _say(f"try {number}/{tries}: still on the sign-in page{_flash(status or {})}")
    return code, anonymous


def _wait(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(min(30, deadline - time.monotonic()))


def _pause(seconds: float, what: str = "Next renewal") -> None:
    when = time.strftime("%H:%M", time.localtime(time.time() + seconds))
    _say(f"{what} at {when} ({seconds / 60:.1f} min). Ctrl+C stops. The Chrome window stays open.")
    _wait(seconds)


def _ping_agent(user_agent: Callable[[], str]) -> str:
    try:
        return user_agent()
    except Exception:  # Chrome on an error page may not say; any browser's will do for a ping
        return DEFAULT_UA


def renew_loop(
    cycle: Callable[[bool], int],
    *,
    every: float,
    jitter: float,
    renew_url: str,
    token: str,
    user_agent: Callable[[], str],
    once: bool = False,
    alive: Callable[[], bool] = lambda: True,
    home: str = HOME_URL,
) -> int:
    """The renewal schedule around `cycle(fresh)`, for the Mac and the Linux driver alike.

    `cycle` signs in and sends the session: 0 sent, 1 stop (a captcha), -1 try again later.
    It raises SiteUnreachable when a visa-site page didn't load, ProbeError for any other
    failure. Then the visa site gets the Pi's treatment (slot_machine.sitewatch): no sign-in,
    a ping of the home page right away and every PING_EVERY; after CONFIRM_PINGS unanswered
    it's down, and the first answer ends it. Other failures wait 2, 4, 8 ... 60 minutes.
    Before each sign-in the Pi must answer (wait_for_pi). Returns the exit code."""
    site = None  # the sitewatch record while the site looks down
    failures = 0
    number = 1
    agent = None
    every_ping = fmt_dur(sitewatch.PING_EVERY.total_seconds())
    while True:
        if site is not None:
            _wait(max(0.0, (parse_ts(site["next_ping"]) - _now()).total_seconds()))
            agent = agent or _ping_agent(user_agent)
            answered, detail = sitewatch.ping(home, agent)
            before, site = site, sitewatch.pinged(site, answered, detail, _now())
            if site is not None:
                if site["state"] != before["state"]:
                    _say(
                        f"ping: {detail}. The visa site is down. Pinging its home page every "
                        f"{every_ping}; no sign-in until it answers."
                    )
                elif site["state"] == "checking":
                    _say(f"ping {site['pings']}/{sitewatch.CONFIRM_PINGS}: {detail}")
                continue  # still down: pings are quiet
            if before["state"] == "down":
                outage = fmt_dur((_now() - parse_ts(before["since"])).total_seconds())
                _say(f"ping: {detail}. The visa site answers again after {outage}. Signing in now.")
                failures = 0
            else:
                _say(f"ping: {detail}. The home page answers, so the site isn't down.")
                _pause(retry_seconds(failures), f"{failures} failed cycle(s) in a row. Next try")
        wait_for_pi(renew_url, token)
        _say(f"cycle {number}: starting")
        fresh = number > 1  # the first cycle uses the session Chrome already has
        number += 1
        try:
            code = cycle(fresh)
        except SiteUnreachable as err:
            _say(f"error: {err}")
            if once or not alive():
                return 1
            failures += 1
            site = sitewatch.failed(None, str(err), _now())
            _say("The visa site may be down. Pinging its home page before signing in again.")
            continue
        except ProbeError as err:
            _say(f"error: {err}")
            if not alive():
                return 1
            code = -1
        if code == 1:
            return 1
        if once:
            return 0 if code == 0 else 1
        failures = 0 if code == 0 else failures + 1
        if failures:
            _pause(retry_seconds(failures), f"{failures} failed cycle(s) in a row. Next try")
        else:
            _pause(cycle_seconds(every, jitter))


def renew_cycle(
    window_id: str,
    email: str,
    password: str,
    tries: int,
    renew_url: str,
    token: str,
    previous: str | None,
    *,
    fresh: bool,
) -> tuple[int, str | None]:
    """One renewal. Returns (exit code, cookie sent). Code -1 means try again later."""
    if fresh:
        sign_out(window_id)
    code = -1
    avoid = previous
    for number in range(1, tries + 1):
        code, seen = run_attempt(window_id, email, password, number, tries)
        if seen:
            avoid = seen
        if code >= 0:
            break
    if code == 1:
        return 1, previous
    if code != 0:
        _say("This cycle did not reach the appointment page.")
        return -1, previous
    cookie = send_session(window_id, renew_url, token, avoid)
    return 0, cookie


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m signin",
        description="Renew the visa-site session from the Mac Chrome you already use.",
    )
    parser.add_argument(
        "--init", action="store_true", help="create the empty credentials file (mode 600) and exit"
    )
    parser.add_argument(
        "--tries", type=int, default=TRIES, help=f"sign-in attempts per cycle (default {TRIES})"
    )
    parser.add_argument(
        "--every", type=float, default=EVERY_MINUTES, help="minutes between renewals (default 12)"
    )
    parser.add_argument(
        "--jitter",
        type=float,
        default=JITTER_MINUTES,
        help="random ± minutes added to --every (default 0; overnight is --every 50 --jitter 3)",
    )
    parser.add_argument("--credentials", type=Path, default=CREDENTIALS, help="AIS_EMAIL / AIS_PASSWORD file")
    parser.add_argument(
        "--settings", type=Path, default=SETTINGS, help="slot-machine.env with RENEW_URL and RENEW_TOKEN"
    )
    args = parser.parse_args(argv)
    if args.tries < 1:
        raise ProbeError("--tries must be at least 1")
    if args.init:
        init_credentials(args.credentials)
        return 0
    email, password = load_credentials(args.credentials)
    renew_url, token = load_renew_settings(args.settings)
    cycle_seconds(args.every, args.jitter)  # reject a bad interval before opening Chrome
    pace = f"every {args.every:g} min" if args.jitter == 0 else f"every {args.every:g} min ± {args.jitter:g}"
    _say(
        f"Renewing {pace}. The first cycle uses the current Chrome session; later cycles sign out and sign in."
    )
    _say("Opening a new Chrome window on the sign-in page.")
    window_id = open_sign_in_window()
    wait_until(window_id, lambda page: bool(page.get("url")), 15, what="for the new Chrome window")
    previous: str | None = None

    def cycle(fresh: bool) -> int:
        nonlocal previous
        code, previous = renew_cycle(
            window_id, email, password, args.tries, renew_url, token, previous, fresh=fresh
        )
        return code

    return renew_loop(
        cycle,
        every=args.every,
        jitter=args.jitter,
        renew_url=renew_url,
        token=token,
        user_agent=lambda: browser_user_agent(window_id),
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ProbeError as err:
        print(f"error: {err}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nStopped. The Chrome window is left as it is.", file=sys.stderr)
        sys.exit(130)
