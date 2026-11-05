"""Settings file parsing and validation."""

from pathlib import Path

import pytest

from slot_machine.config import (
    ENV_KEYS,
    Config,
    default_config_path,
    default_state_dir,
    load_config,
    read_env_file,
)


def write(tmp_path, text, mode=0o600) -> Path:
    path = tmp_path / "x.env"
    path.write_text(text)
    path.chmod(mode)
    return path


def test_missing_file_gives_defaults(tmp_path):
    cfg = load_config(tmp_path / "nope.env")
    assert cfg.locale == "en-kz" and cfg.digest_hour == 9 and cfg.digest_silent is True
    assert cfg.min_interval_minutes == 30 and cfg.backoff_hours == 3 and cfg.error_alert_after == 3


def test_parses_values_comments_and_quotes(tmp_path):
    cfg = load_config(
        write(
            tmp_path,
            """
# comment
AIS_SCHEDULE_ID=1234
AIS_FACILITY_ID = "134"
LOCATION_LABEL='Astana embassy'
DIGEST_HOUR=7
DIGEST_SILENT=no
MIN_INTERVAL_MINUTES=15.5
ALERT_ONLY_BEFORE=2027-04-30
not a setting line
AIS_BASE_URL=http://example.test/
""",
        )
    )
    assert cfg.schedule_id == "1234" and cfg.facility_id == "134"
    assert cfg.label == "Astana embassy"
    assert cfg.digest_hour == 7 and cfg.digest_silent is False
    assert cfg.min_interval_minutes == 15.5
    assert cfg.alert_only_before == "2027-04-30"
    assert cfg.ais_base == "http://example.test"


def test_environment_overrides_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DIGEST_HOUR", "21")
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "s"))
    cfg = load_config(write(tmp_path, "DIGEST_HOUR=7\n"))
    assert cfg.digest_hour == 21
    assert cfg.state_dir == tmp_path / "s"


def test_empty_value_keeps_default(tmp_path):
    assert load_config(write(tmp_path, "DIGEST_HOUR=\n")).digest_hour == 9


@pytest.mark.parametrize(
    "line, message",
    [
        ("DIGEST_HOUR=24", "DIGEST_HOUR must be 0-23"),
        ("DIGEST_HOUR=nine", "DIGEST_HOUR='nine' is not valid"),
        ("ALERT_ONLY_BEFORE=30.04.2027", "ALERT_ONLY_BEFORE must look like"),
        ("AIS_SCHEDULE_ID=abc", "AIS_SCHEDULE_ID should be a number"),
        ("AIS_FACILITY_ID=13x", "AIS_FACILITY_ID should be a number"),
    ],
)
def test_invalid_values_stop_with_a_clear_message(tmp_path, line, message):
    with pytest.raises(SystemExit, match=message):
        load_config(write(tmp_path, line + "\n"))


def test_warns_when_file_is_readable_by_others(tmp_path, capsys):
    read_env_file(write(tmp_path, "A=1\n", mode=0o644))
    assert "readable by other users" in capsys.readouterr().out


def test_no_warning_for_private_file(tmp_path, capsys):
    assert read_env_file(write(tmp_path, "A=1\n")) == {"A": "1"}
    assert capsys.readouterr().out == ""


# --------------------------------------------------------------------------- default locations


def test_default_paths_follow_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
    assert default_config_path() == tmp_path / "cfg" / "slot-machine" / "slot-machine.env"
    assert default_state_dir() == tmp_path / "st" / "slot-machine"
    assert Config().state_dir == tmp_path / "st" / "slot-machine"


def test_default_paths_fall_back_to_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/is/ignored")  # the XDG spec says so
    home = tmp_path / "home"
    assert default_config_path() == home / ".config" / "slot-machine" / "slot-machine.env"
    assert default_state_dir() == home / ".local" / "state" / "slot-machine"


def test_config_env_var_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("SLOT_MACHINE_CONFIG", str(tmp_path / "x.env"))
    assert default_config_path() == tmp_path / "x.env"


def test_example_settings_file_matches_real_settings(tmp_path):
    import re

    example = Path(__file__).resolve().parent.parent / "config" / "slot-machine.env.example"
    keys = set(re.findall(r"^([A-Z_]+)=", example.read_text(), re.M))
    assert keys <= set(ENV_KEYS), "unknown keys in the example"
    assert set(ENV_KEYS) - keys == {"AIS_BASE_URL", "TELEGRAM_API_BASE"}, "settings missing from the example"
    copy = write(tmp_path, example.read_text())
    assert load_config(copy).schedule_id == ""  # loads cleanly as shipped


# --------------------------------------------------------------------------- cron schedule


@pytest.mark.parametrize(
    "value, expected",
    [
        ("", "17 * * * *"),  # default
        ("17,47 * * * *", "17,47 * * * *"),
        ("*/20  7-23 * * mon-fri", "*/20 7-23 * * mon-fri"),  # extra spaces collapse
    ],
)
def test_cron_schedule(tmp_path, value, expected):
    assert load_config(write(tmp_path, f"CRON_SCHEDULE={value}\n")).cron_schedule == expected


@pytest.mark.parametrize(
    "value",
    [
        "17 * * *",  # four fields
        "17 * * * * *",  # six
        "@hourly",
        "17 * * * *; rm -rf ~",
        "17 * * * $(id)",
        "17 * * * `id`",
        "17 * * * * echo hi",
        "17 * * * %",
        "17 * * * '*'",
    ],
)
def test_cron_schedule_rejects_anything_but_five_plain_fields(tmp_path, value):
    with pytest.raises(SystemExit, match="CRON_SCHEDULE must be the five crontab time fields"):
        load_config(write(tmp_path, f"CRON_SCHEDULE={value}\n"))
