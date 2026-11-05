#!/usr/bin/env python3
"""Set up (or rotate) the renewal page's secret in your local settings.

    python3 deploy/renew_token.py [LOCAL]     (default: config/slot-machine.env)

Writes a new random RENEW_TOKEN, sets RENEW_URL to http://yarikos.local:8080 if it's
empty, and copies the token to the macOS clipboard (pbcopy) for the Chrome extension's
options. The token is never printed. `make renew-token` runs this, then push-config.
Rotating it means pasting the new one into the extension again.
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


def main(local: Path) -> int:
    if not local.exists():
        raise SystemExit(f"No {local} - run `make config` first.")
    token = secrets.token_urlsafe(32)  # 43 characters, [A-Za-z0-9_-]
    new, url = set_values(local.read_text(encoding="utf-8"), token)
    tmp = local.with_name(local.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(new)
    os.replace(tmp, local)
    copied = False
    if shutil.which("pbcopy"):
        copied = subprocess.run(["pbcopy"], input=token.encode(), check=False).returncode == 0
    print(f"New RENEW_TOKEN saved in {local.name} (RENEW_URL={url}).")
    if copied:
        print("It's on the clipboard: paste it into the slot-machine extension's options.")
    else:
        print(f"Copy RENEW_TOKEN from {local} into the slot-machine extension's options.")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "config" / "slot-machine.env"))
