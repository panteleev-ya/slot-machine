"""Files on disk: private JSON (state, cookies) and the one-run-at-a-time lock."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

from .timeutil import log

try:
    import fcntl  # POSIX: stops two runs from overlapping
except ImportError:  # pragma: no cover
    fcntl = None

LOCK_NAME = ".slot-machine.lock"


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except ValueError:
        log(f"warning: {path.name} is unreadable; starting fresh")
        return default


def save_json(path: Path, data) -> None:
    """Write atomically, readable by the owner only (cookies.json is a login)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


@contextmanager
def single_instance(state_dir: Path):
    """Yield True if this run holds the lock, False if another run does."""
    state_dir.mkdir(parents=True, exist_ok=True)
    if fcntl is None:
        yield True
        return
    with open(state_dir / LOCK_NAME, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
