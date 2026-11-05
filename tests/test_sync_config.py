"""Local settings file: structure from the committed template, values from you."""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("sync_config", ROOT / "deploy" / "sync_config.py")
sync_config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync_config)

TEMPLATE = """# header
# --- Site
# schedule id comment
AIS_SCHEDULE_ID=
LOCATION_LABEL=Astana

# --- Telegram
TELEGRAM_BOT_TOKEN=
DIGEST_HOUR=9
"""


@pytest.fixture
def files(tmp_path):
    template = tmp_path / "x.env.example"
    template.write_text(TEMPLATE)
    return template, tmp_path / "x.env"


def test_creates_private_copy_of_template(files, capsys):
    template, local = files
    assert sync_config.sync(template, local) == 0
    assert local.read_text() == TEMPLATE
    assert local.stat().st_mode & 0o777 == 0o600
    assert "created" in capsys.readouterr().out


def test_keeps_values_and_takes_comments_from_template(files, capsys):
    template, local = files
    local.write_text("# my own note\nTELEGRAM_BOT_TOKEN=123:abc\nAIS_SCHEDULE_ID=555\nLOCATION_LABEL=\n")
    local.chmod(0o600)
    assert sync_config.sync(template, local) == 0
    assert local.read_text() == TEMPLATE.replace("AIS_SCHEDULE_ID=\n", "AIS_SCHEDULE_ID=555\n").replace(
        "LOCATION_LABEL=Astana",
        "LOCATION_LABEL=",  # an empty value you set stays empty
    ).replace("TELEGRAM_BOT_TOKEN=\n", "TELEGRAM_BOT_TOKEN=123:abc\n")
    out = capsys.readouterr().out
    assert "refreshed" in out and "new settings from the template: DIGEST_HOUR" in out


def test_second_run_changes_nothing(files, capsys):
    template, local = files
    sync_config.sync(template, local)
    before = local.read_text()
    assert sync_config.sync(template, local) == 0
    assert local.read_text() == before
    assert "up to date" in capsys.readouterr().out


def test_template_changes_flow_into_local_copy(files):
    template, local = files
    sync_config.sync(template, local)
    local.write_text(local.read_text().replace("AIS_SCHEDULE_ID=", "AIS_SCHEDULE_ID=555"))
    template.write_text(
        TEMPLATE.replace("# schedule id comment", "# better comment") + "# new\nBACKOFF_HOURS=3\n"
    )
    sync_config.sync(template, local)
    text = local.read_text()
    assert "# better comment\nAIS_SCHEDULE_ID=555\n" in text
    assert text.endswith("# new\nBACKOFF_HOURS=3\n")


def test_settings_unknown_to_template_are_kept_at_the_bottom(files, capsys):
    template, local = files
    local.write_text("AIS_SCHEDULE_ID=1\nOLD_SETTING=x\n")
    sync_config.sync(template, local)
    assert local.read_text().endswith(f"\n\n{sync_config.UNKNOWN_HEADER}\nOLD_SETTING=x\n")
    assert "ignored by the app): OLD_SETTING" in capsys.readouterr().out


def test_invalid_values_fail(files, capsys):
    template, local = files
    local.write_text("DIGEST_HOUR=99\n")
    assert sync_config.sync(template, local) == 1
    assert "DIGEST_HOUR must be 0-23" in capsys.readouterr().err


def test_fixes_permissions_of_existing_file(files):
    template, local = files
    sync_config.sync(template, local)
    local.chmod(0o644)
    sync_config.sync(template, local)
    assert local.stat().st_mode & 0o777 == 0o600


def test_real_template_round_trips(tmp_path):
    template = tmp_path / "t.env.example"
    shutil.copy(ROOT / "config" / "slot-machine.env.example", template)
    local = tmp_path / "t.env"
    assert sync_config.sync(template, local) == 0
    assert local.read_text() == template.read_text()


def test_local_settings_file_is_git_ignored():
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    ignored = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", "config/slot-machine.env"])
    template = subprocess.run(
        ["git", "-C", str(ROOT), "check-ignore", "-q", "config/slot-machine.env.example"]
    )
    assert ignored.returncode == 0, "config/slot-machine.env must be git-ignored"
    assert template.returncode == 1, "the template must stay tracked"


def run_script(*args):
    return subprocess.run(
        [sys.executable, str(ROOT / "deploy" / "sync_config.py"), *map(str, args)],
        capture_output=True,
        text=True,
    )


def test_cron_schedule_mode_prints_validated_value(tmp_path):
    local = tmp_path / "x.env"
    local.write_text("CRON_SCHEDULE=17,47   * * * *\n")
    local.chmod(0o600)
    proc = run_script("--cron-schedule", local)
    assert (proc.returncode, proc.stdout) == (0, "17,47 * * * *\n")


def test_cron_schedule_mode_default_without_settings(tmp_path):
    proc = run_script("--cron-schedule", tmp_path / "missing.env")
    assert (proc.returncode, proc.stdout) == (0, "17 * * * *\n")


def test_cron_schedule_mode_refuses_bad_value(tmp_path):
    local = tmp_path / "x.env"
    local.write_text("CRON_SCHEDULE=17 * * * *; id\n")
    local.chmod(0o600)
    proc = run_script("--cron-schedule", local)
    assert proc.returncode == 1 and proc.stdout == ""
    assert "CRON_SCHEDULE must be" in proc.stderr
