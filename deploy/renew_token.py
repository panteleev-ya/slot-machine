#!/usr/bin/env python3
"""Set up (or rotate) the renewal page's secret in your local settings.

    python3 deploy/renew_token.py [--copy] [LOCAL]     (default: config/slot-machine.env)

Writes a new random RENEW_TOKEN, sets RENEW_URL to http://yarikos.local:8080 if it's
empty, and copies the token to the macOS clipboard (pbcopy) for the Chrome extension's
options. The token is never printed. `make renew-token` runs this, then push-config.
Rotating it means pasting the new one into every installed extension again.

--copy only copies the current token (for another person's extension), changing nothing.
`make share-extension` uses it.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_URL = "http://yarikos.local:8080"


def set_values(text: str, token: str) -> tuple[str, str]:
    """Return (new text, RENEW_URL in effect). Both keys must exist (run sync first)."""
    url = ""
    out = []
    for line in text.splitlines(keepends=True):
        if line.startswith("RENEW_TOKEN="):
            line = f"RENEW_TOKEN={token}\n"
        elif m := re.match(r"^RENEW_URL=(.*)$", line.rstrip("\n")):
            url = m.group(1).strip().strip("'\"") or DEFAULT_URL
            line = f"RENEW_URL={url}\n"
        out.append(line)
    new = "".join(out)
    if "RENEW_TOKEN=" not in new or not url:
        raise SystemExit("RENEW_URL/RENEW_TOKEN missing from the settings: run `make config` first.")
    return new, url


def to_clipboard(token: str) -> bool:
    if not shutil.which("pbcopy"):
        return False
    return subprocess.run(["pbcopy"], input=token.encode(), check=False).returncode == 0


def current_token(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("RENEW_TOKEN="):
            return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def copy_current(local: Path) -> int:
    token = current_token(local.read_text(encoding="utf-8"))
    if not token:
        raise SystemExit("No RENEW_TOKEN yet: run `make renew-token` first.")
    if not to_clipboard(token):
        raise SystemExit(f"Couldn't use the clipboard; copy RENEW_TOKEN from {local} by hand.")
    print("The current RENEW_TOKEN is on the clipboard (unchanged).")
    return 0


def main(local: Path, copy: bool = False) -> int:
    if not local.exists():
        raise SystemExit(f"No {local} - run `make config` first.")
    if copy:
        return copy_current(local)
    token = secrets.token_urlsafe(32)  # 43 characters, [A-Za-z0-9_-]
    new, url = set_values(local.read_text(encoding="utf-8"), token)
    tmp = local.with_name(local.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(new)
    os.replace(tmp, local)
    copied = to_clipboard(token)
    print(f"New RENEW_TOKEN saved in {local.name} (RENEW_URL={url}).")
    if copied:
        print("It's on the clipboard: paste it into the slot-machine extension's options.")
    else:
        print(f"Copy RENEW_TOKEN from {local} into the slot-machine extension's options.")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    copy = "--copy" in args
    rest = [a for a in args if a != "--copy"]
    sys.exit(main(Path(rest[0]) if rest else ROOT / "config" / "slot-machine.env", copy))
