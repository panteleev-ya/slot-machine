"""The built single-file app, run the way cron runs it on the Pi."""

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from fakes import VALID_SESSION

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def app(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("dist") / "slot-machine"
    subprocess.run(
        [sys.executable, str(ROOT / "deploy" / "build.py"), str(out)], check=True, capture_output=True
    )
    return out


def run(app, env_file, *args, home=None):
    env = {"PATH": os.environ["PATH"], "HOME": str(home or env_file.parent / "home")}
    return subprocess.run(
        [sys.executable, str(app), "--config", str(env_file), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_archive_holds_only_the_package(app):
    names = zipfile.ZipFile(app).namelist()
    assert "__main__.py" in names
    assert "slot_machine/cli.py" in names
    assert not [n for n in names if "__pycache__" in n or n.endswith(".pyc")]
    assert not [n for n in names if not (n == "__main__.py" or n.startswith("slot_machine/"))]
    assert app.read_bytes().startswith(b"#!/usr/bin/env python3\n")


def test_version_from_the_archive(app, tmp_path):
    proc = subprocess.run([sys.executable, str(app), "--version"], capture_output=True, text=True)
    assert proc.returncode == 0 and proc.stdout.strip() == "slot-machine 0.1.0"


def test_full_cycle_from_the_archive(app, env_file, ais, tg, tmp_path):
    ais.fake.dates = ["2027-09-01"]
    proc = run(app, env_file, "--set-cookie", VALID_SESSION)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Cookie works. Earliest date right now: 2027-09-01." in proc.stdout
    ais.fake.dates = ["2027-08-01"]
    proc = run(app, env_file, "--force")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert [m["text"].splitlines()[0] for m in tg.fake.messages] == [
        "<b>✅ Visa slot watch started - Astana</b>",
        "<b>🟢 Sooner visa date opened - Astana</b>",
    ]
    state_dir = tmp_path / "state"
    assert json.loads((state_dir / "cookies.json").read_text()) == {"_yatri_session": VALID_SESSION}
    assert (state_dir / "state.json").stat().st_mode & 0o777 == 0o600
