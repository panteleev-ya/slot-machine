"""One scheduled run end to end against the fake site and fake Telegram."""

import json

import pytest

from slot_machine.runner import run_check

from fakes import VALID_SESSION

JAR = {"_yatri_session": VALID_SESSION}


def sent(tg) -> list[str]:
    return [m["text"] for m in tg.fake.messages]


def state(cfg) -> dict:
    return json.loads((cfg.state_dir / "state.json").read_text())


@pytest.fixture
def started(cfg, ais, tg, clock):
    """Cookie set and baseline sent at T0, earliest Sep 1."""
    ais.fake.dates = ["2027-09-01", "2027-10-01"]
    code, res = run_check(cfg, force=True, new_cookies=JAR)
    assert (code, res.kind) == (0, "ok")
    assert len(tg.fake.messages) == 1
    return cfg


def test_first_run_sends_baseline_and_saves_private_files(started, ais, tg, clock):
    msg = tg.fake.messages[0]
    assert "Visa slot watch started - Astana" in msg["text"]
    assert msg["parse_mode"] == "HTML" and msg["disable_notification"] == "false"
    assert msg["chat_id"] == "42"
    for name in ("state.json", "cookies.json"):
        assert (started.state_dir / name).stat().st_mode & 0o777 == 0o600
    assert json.loads((started.state_dir / "cookies.json").read_text()) == JAR
    assert state(started)["session_set_at"] == clock.now.isoformat()


def test_min_interval_skips_without_a_request(started, ais, clock):
    clock.advance(minutes=10)
    assert run_check(started) == (0, None)
    assert ais.fake.days_requests() == 1


def test_force_ignores_min_interval(started, ais, clock):
    clock.advance(minutes=10)
    assert run_check(started, force=True)[1].kind == "ok"
    assert ais.fake.days_requests() == 2


def test_hourly_run_alerts_on_sooner_date(started, ais, tg, clock):
    clock.advance(hours=1)
    ais.fake.dates = ["2027-08-01"]
    assert run_check(started)[1].kind == "ok"
    assert "Sooner visa date opened" in sent(tg)[-1]


@pytest.mark.parametrize(
    "change, detail",
    [
        ({"schedule_id": ""}, "AIS_SCHEDULE_ID / AIS_FACILITY_ID not set"),
        ({"facility_id": ""}, "AIS_SCHEDULE_ID / AIS_FACILITY_ID not set"),
    ],
)
def test_missing_ids_skip(cfg, ais, clock, change, detail):
    for k, v in change.items():
        setattr(cfg, k, v)
    code, res = run_check(cfg)
    assert (code, res.kind, res.detail) == (0, "skipped", detail)
    assert ais.fake.days_requests() == 0


def test_no_cookie_skips(cfg, ais, clock):
    code, res = run_check(cfg)
    assert res.detail == "no session cookie yet (run --set-cookie)"
    assert ais.fake.days_requests() == 0


def test_empty_answer_pauses_for_backoff_hours(started, ais, clock):
    clock.advance(hours=1)
    ais.fake.dates = []
    assert run_check(started)[1].kind == "empty"
    clock.advance(hours=1)
    assert run_check(started)[1].detail == "backing off until 10:00 (2h 0m)"
    clock.advance(hours=2)
    ais.fake.dates = ["2027-09-01"]
    assert run_check(started)[1].kind == "ok"
    assert ais.fake.days_requests() == 3


@pytest.mark.parametrize("via", ["check-now", "manual"])
def test_local_force_with_an_empty_answer_renews_the_pause(started, ais, clock, capsys, via):
    clock.advance(hours=1)
    ais.fake.dates = []
    run_check(started)  # 07:00: pause until 10:00
    clock.advance(hours=1)
    assert run_check(started, force=True, via=via)[1].kind == "empty"
    assert state(started)["backoff_until"] == "2026-11-05T11:00:00+00:00"
    assert f"check: empty  no dates in reply; scheduled checks pause until 11:00 (3h 0m) [{via}]" in (
        capsys.readouterr().out
    )


def test_telegram_check_with_an_empty_answer_keeps_the_pause(started, ais, clock, capsys):
    clock.advance(hours=1)
    ais.fake.dates = []
    run_check(started)  # 07:00: pause until 10:00
    clock.advance(hours=1)
    assert run_check(started, force=True, via="telegram:42")[1].kind == "empty"
    assert state(started)["backoff_until"] == "2026-11-05T10:00:00+00:00"
    assert "the pause until 10:00 (2h 0m) stays as it was [telegram:42]" in capsys.readouterr().out
    clock.advance(minutes=30)
    assert run_check(started)[1].detail == "backing off until 10:00 (1h 30m)"  # cron still waits


def test_throttled_returns_1_and_pauses(started, ais, clock):
    clock.advance(hours=1)
    ais.fake.status = 429
    assert run_check(started)[0] == 1
    clock.advance(hours=1)
    assert run_check(started)[1].kind == "skipped"
    assert ais.fake.days_requests() == 2


def test_expired_session_alerts_once_and_stops_requesting(started, ais, tg, clock):
    ais.fake.sessions.clear()
    clock.advance(hours=1)
    code, res = run_check(started)
    assert (code, res.kind) == (1, "expired")
    assert "the AIS session ended" in sent(tg)[-1]
    for _ in range(3):
        clock.advance(hours=1)
        assert run_check(started)[1].detail == "session expired (run --set-cookie)"
    assert ais.fake.days_requests() == 2
    assert len(tg.fake.messages) == 2
    # a fresh cookie resumes
    ais.fake.sessions.add("n" * 40)
    assert run_check(started, force=True, new_cookies={"_yatri_session": "n" * 40})[1].kind == "ok"
    assert state(started)["session_expired_at"] is None


def test_rotated_cookie_is_saved(started, ais, clock):
    clock.advance(hours=1)
    ais.fake.rotate_cookie = "r" * 40
    run_check(started)
    saved = json.loads((started.state_dir / "cookies.json").read_text())
    assert saved == {"_yatri_session": "r" * 40}


def test_failed_telegram_send_is_retried_next_run(started, ais, tg, clock):
    clock.advance(hours=1)
    ais.fake.dates = ["2027-08-01"]
    tg.fake.fail_next = 2  # both attempts of this run fail
    run_check(started)
    assert len(tg.fake.messages) == 1
    assert state(started)["last_seen"] == "2027-09-01"  # rolled back
    clock.advance(hours=1)
    run_check(started)
    assert "Sooner visa date opened" in sent(tg)[-1]


def test_telegram_server_error_retried_within_run(started, ais, tg, clock):
    clock.advance(hours=1)
    ais.fake.dates = ["2027-08-01"]
    tg.fake.fail_next = 1
    run_check(started)
    assert "Sooner visa date opened" in sent(tg)[-1]


def test_telegram_client_error_not_retried(started, ais, tg, clock):
    clock.advance(hours=1)
    ais.fake.dates = ["2027-08-01"]
    tg.fake.fail_next, tg.fake.fail_status = 2, 400
    run_check(started)
    assert tg.fake.fail_next == 1  # one attempt only
    assert len(tg.fake.messages) == 1


def test_dry_run_prints_and_saves_nothing(cfg, ais, tg, clock, capsys):
    ais.fake.dates = ["2027-09-01"]
    (cfg.state_dir).mkdir(parents=True)
    (cfg.state_dir / "cookies.json").write_text(json.dumps(JAR))
    run_check(cfg, dry_run=True)
    assert "--- would send (baseline) to 42 ---" in capsys.readouterr().out
    assert tg.fake.messages == []
    assert not (cfg.state_dir / "state.json").exists()


def test_second_run_waits_for_lock(cfg, clock, capsys):
    fcntl = pytest.importorskip("fcntl")
    cfg.state_dir.mkdir(parents=True)
    with open(cfg.state_dir / ".slot-machine.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert run_check(cfg) == (0, None)
    assert "another run is in progress" in capsys.readouterr().out
