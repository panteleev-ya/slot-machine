#!/usr/bin/env python3
"""Create or refresh your local settings file from the committed template.

    python3 deploy/sync_config.py [TEMPLATE] [LOCAL]
    (defaults: config/slot-machine.env.example -> config/slot-machine.env)

The template (committed) owns the structure: which settings exist, their order and
their comments. The local file (git-ignored, chmod 600) owns the values. Each run
rewrites the local file as "template, with your values filled in", so new settings
and updated comments show up and nothing you set is lost:

  * a value you set is kept, even when it's empty in the template;
  * a setting the template added appears with its template default;
  * a setting the template no longer has is kept at the bottom, marked unknown;
  * comments you wrote in the local file are not kept (put notes in the template).

Then it validates the result with the app's own parser, so a bad value is caught
on the Mac before it reaches the server. Exit code 1 means invalid settings.

Read-only modes for make and the deploy scripts (validated, one value on stdout):

    sync_config.py --cron-schedule [LOCAL]   CRON_SCHEDULE (the default without settings)
    sync_config.py --server [LOCAL]          SERVER_USER@SERVER_HOST, for ssh
    sync_config.py --server-host [LOCAL]     SERVER_HOST alone
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from slot_machine.config import load_config, read_env_file  # noqa: E402

KEY_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=")
UNKNOWN_HEADER = "# --- Not in the template (slot-machine ignores these) ------------------------"


def render(template_text: str, values: dict) -> tuple[str, list[str], list[str]]:
    """Return (new local text, settings added from the template, unknown settings kept)."""
    out, seen, added = [], set(), []
    for line in template_text.splitlines():
        m = KEY_LINE.match(line)
        if not m:
            out.append(line)
            continue
        key = m.group(1)
        seen.add(key)
        if key in values:
            out.append(f"{key}={values[key]}")
        else:
            out.append(line)
            added.append(key)
    unknown = [k for k in values if k not in seen]
    if unknown:
        out += ["", UNKNOWN_HEADER, *(f"{k}={values[k]}" for k in unknown)]
    return "\n".join(out).rstrip("\n") + "\n", added, unknown


def write_private(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    path.chmod(0o600)


def sync(template: Path, local: Path) -> int:
    existed = local.exists()
    values = read_env_file(local) if existed else {}
    text, added, unknown = render(template.read_text(encoding="utf-8"), values)
    changed = not existed or local.read_text(encoding="utf-8") != text
    if changed:
        write_private(local, text)
    elif local.stat().st_mode & 0o077:
        local.chmod(0o600)

    name = local.relative_to(ROOT) if local.is_relative_to(ROOT) else local
    if not existed:
        print(f"created {name} from the template - fill in your values")
    elif changed:
        print(f"refreshed {name} from the template (your values kept)")
        if added:
            print(f"  new settings from the template: {', '.join(added)}")
    else:
        print(f"{name} is up to date with the template")
    if unknown:
        print(f"  not in the template (kept at the bottom, ignored by the app): {', '.join(unknown)}")
    try:
        load_config(local)
    except SystemExit as e:
        print(f"  {e}", file=sys.stderr)
        return 1
    return 0


NO_SERVER = (
    "No server set: put SERVER_HOST (and SERVER_USER) in config/slot-machine.env "
    "(make edit-config), or pass PI=user@host"
)
MODES = {
    "--cron-schedule": lambda cfg: cfg.cron_schedule,
    "--server": lambda cfg: cfg.ssh_target,
    "--server-host": lambda cfg: cfg.server_host,
}


def print_value(mode: str, local: Path) -> int:
    """Print one validated value from the local settings (environment overrides apply)."""
    try:
        value = MODES[mode](load_config(local))
    except SystemExit as e:
        print(e, file=sys.stderr)
        return 1
    if not value:
        print(NO_SERVER, file=sys.stderr)
        return 1
    print(value)
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] and sys.argv[1] in MODES:
        local = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "config" / "slot-machine.env"
        sys.exit(print_value(sys.argv[1], local))
    args = [Path(a) for a in sys.argv[1:3]]
    template = args[0] if args else ROOT / "config" / "slot-machine.env.example"
    local = args[1] if len(args) > 1 else ROOT / "config" / "slot-machine.env"
    sys.exit(sync(template, local))
