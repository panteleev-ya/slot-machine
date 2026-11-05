"""State files and the small formatting helpers."""

import json
from datetime import date

import pytest

from slot_machine.store import load_json, save_json
from slot_machine.timeutil import days_between, fmt_day, fmt_dur, fmt_short, plural


def test_save_json_is_private_and_atomic(tmp_path):
    path = tmp_path / "sub" / "state.json"
    save_json(path, {"b": 1, "a": [1, 2]})
    assert json.loads(path.read_text()) == {"a": [1, 2], "b": 1}
    assert path.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "sub" / "state.json.tmp").exists()


def test_load_json_missing_and_corrupt(tmp_path, capsys):
    assert load_json(tmp_path / "none.json", {"d": 1}) == {"d": 1}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_json(bad, {}) == {}
    assert "unreadable; starting fresh" in capsys.readouterr().out


@pytest.mark.parametrize("n, out", [(1, "1 day"), (2, "2 days"), (0, "0 days"), (-1, "-1 day")])
def test_plural(n, out):
    assert plural(n, "day") == out


def test_fmt_day():
    today = date(2026, 11, 5)
    assert fmt_day("2026-11-05", today) == "Thu Nov 5, 2026 (today)"
    assert fmt_day("2026-11-06", today) == "Fri Nov 6, 2026 (in 1 day)"
    assert fmt_day("2027-09-01", today) == "Wed Sep 1, 2027 (in 300 days)"


def test_fmt_short_and_days_between():
    assert fmt_short("2027-01-09") == "Jan 9, 2027"
    assert days_between("2027-08-01", "2027-09-01") == 31


@pytest.mark.parametrize(
    "seconds, out",
    [
        (30, "less than a minute"),
        (60, "1m"),
        (3600 + 120, "1h 2m"),
        (2 * 86400 + 3 * 3600 + 59, "2d 3h"),
    ],
)
def test_fmt_dur(seconds, out):
    assert fmt_dur(seconds) == out
