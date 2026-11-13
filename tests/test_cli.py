"""The command line, as cron and you would run it."""

import pytest

from slot_machine.cli import main

from fakes import VALID_SESSION


def run(env_file, *args) -> int:
    return main(["--config", str(env_file), *args])


def test_set_cookie_then_hourly_then_status(env_file, ais, tg, clock, capsys):
    ais.fake.dates = ["2027-09-01"]
    assert run(env_file, "--set-cookie", VALID_SESSION) == 0
    assert "Cookie works. Earliest date right now: 2027-09-01." in capsys.readouterr().out
    clock.advance(hours=1)
    ais.fake.dates = ["2027-08-01"]
    assert run(env_file) == 0
    assert "check: ok 2027-08-01" in capsys.readouterr().out
    assert run(env_file, "--status") == 0
    out = capsys.readouterr().out
    assert "Location:   Astana (facility 134, schedule 1234)" in out
    assert "Earliest:   Sun Aug 1, 2027" in out
    assert "Session:    cookie saved; active for 1h 0m" in out
    assert [m["text"].splitlines()[0] for m in tg.fake.messages] == [
        "<b>✅ Visa slot watch started - Astana</b>",
        "<b>🟢 Sooner visa date opened - Astana</b>",
    ]


def test_set_cookie_prompts_when_no_value(env_file, ais, clock, monkeypatch, capsys):
    monkeypatch.setattr("getpass.getpass", lambda prompt: VALID_SESSION)
    ais.fake.dates = ["2027-09-01"]
    assert run(env_file, "--set-cookie") == 0
    assert "Cookie works" in capsys.readouterr().out


def test_set_cookie_rejected_by_site(env_file, ais, clock, capsys):
    assert run(env_file, "--set-cookie", "x" * 40) == 1
    assert "Not saved: the site rejected that session" in capsys.readouterr().out


def test_set_cookie_empty_answer(env_file, ais, clock, capsys):
    assert run(env_file, "--set-cookie", VALID_SESSION) == 0
    assert (
        "Cookie works, but no dates were returned (none open, or throttled). "
        "Scheduled checks pause until 09:00 (3h 0m)."
    ) in capsys.readouterr().out


def test_set_cookie_garbage_not_saved(env_file, tmp_path):
    with pytest.raises(SystemExit, match="Not saved"):
        run(env_file, "--set-cookie", "nope")
    assert not (tmp_path / "state" / "cookies.json").exists()


def test_config_path_from_environment(env_file, ais, tg, clock, monkeypatch):
    monkeypatch.setenv("SLOT_MACHINE_CONFIG", str(env_file))
    assert main(["--test-telegram"]) == 0
    assert tg.fake.messages[-1]["text"] == "✅ Test message from slot-machine - notifications work."


def test_default_config_location_is_xdg(env_file, tmp_path, tg, clock):
    home_cfg = tmp_path / "home" / ".config" / "slot-machine" / "slot-machine.env"
    home_cfg.parent.mkdir(parents=True)
    env_file.rename(home_cfg)
    assert main(["--test-telegram"]) == 0
    assert len(tg.fake.messages) == 1


def test_status_shows_where_files_live(env_file, tmp_path, capsys):
    assert run(env_file, "--status") == 0
    out = capsys.readouterr().out
    assert f"Settings:   {env_file}\n" in out
    assert f"State:      {tmp_path / 'state'}\n" in out
    assert "Earliest:   none / unknown" in out and "Last check: never" in out


def test_version(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == "slot-machine 0.1.0"


def test_test_telegram_failure(env_file, tg, capsys):
    tg.fake.fail_next, tg.fake.fail_status = 1, 400
    assert run(env_file, "--test-telegram") == 1
    assert "42: FAILED - check TELEGRAM_BOT_TOKEN" in capsys.readouterr().out


def test_find_chat_id(env_file, tg, capsys):
    assert run(env_file, "--find-chat-id") == 0
    assert "TELEGRAM_CHAT_ID=42    (yarik)" in capsys.readouterr().out


def test_find_chat_id_bad_token(env_file, tg, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999:wrong")
    with pytest.raises(SystemExit, match="HTTP 401"):
        run(env_file, "--find-chat-id")


def test_discover_finds_schedule_and_locations(env_file, ais, clock, monkeypatch, capsys):
    ais.fake.dates = ["2027-09-01"]
    run(env_file, "--set-cookie", VALID_SESSION)
    capsys.readouterr()
    env_file.write_text(env_file.read_text().replace("AIS_SCHEDULE_ID=1234\n", ""))
    assert run(env_file, "--discover") == 0
    out = capsys.readouterr().out
    assert "Schedule ids found: 1234" in out
    assert "AIS_SCHEDULE_ID=1234" in out
    assert "   134  Astana" in out
    assert "   135  Almaty & Co" in out


def test_discover_needs_cookie(env_file):
    with pytest.raises(SystemExit, match="Run --set-cookie first"):
        run(env_file, "--discover")


def test_discover_with_expired_session(env_file, ais, clock):
    ais.fake.dates = ["2027-09-01"]
    run(env_file, "--set-cookie", VALID_SESSION)
    ais.fake.sessions.clear()
    with pytest.raises(SystemExit, match="isn't valid any more"):
        run(env_file, "--discover")


def test_commands_are_mutually_exclusive(env_file):
    with pytest.raises(SystemExit):
        run(env_file, "--status", "--discover")
