"""Several Telegram chats: who gets what, welcomes for chats added later, partial failures."""

import json

import pytest

from slot_machine.cli import main
from slot_machine.config import load_config
from slot_machine.runner import run_check

from fakes import VALID_SESSION

JAR = {"_yatri_session": VALID_SESSION}
OWNER, FRIEND = "42", "77"


def sent(tg) -> list[tuple[str, str]]:
    """(chat id, first line) of every message, in order."""
    return [(m["chat_id"], m["text"].splitlines()[0]) for m in tg.fake.messages]


def state(cfg) -> dict:
    return json.loads((cfg.state_dir / "state.json").read_text())


@pytest.fixture
def two(cfg, ais, tg, clock):
    """Owner + friend from the start; baseline sent at T0, earliest Sep 1."""
    cfg.chat_id = f"{OWNER}, {FRIEND}"
    ais.fake.dates = ["2027-09-01"]
    run_check(cfg, force=True, new_cookies=JAR)
    return cfg


def test_chat_list_parsing(tmp_path):
    path = tmp_path / "x.env"
    path.write_text("TELEGRAM_CHAT_ID= 42, 77 ,42,,-100123\n")
    path.chmod(0o600)
    cfg = load_config(path)
    assert cfg.chat_ids == ["42", "77", "-100123"]  # trimmed, de-duplicated, group ids allowed
    assert cfg.owner_chat == "42"


def test_bad_chat_id_is_a_config_error(tmp_path):
    path = tmp_path / "x.env"
    path.write_text("TELEGRAM_CHAT_ID=42 77\n")
    path.chmod(0o600)
    with pytest.raises(SystemExit, match="comma-separate several chats"):
        load_config(path)


def test_baseline_and_date_alerts_go_to_everyone(two, ais, tg, clock):
    clock.advance(hours=1)
    ais.fake.dates = ["2027-08-01"]
    run_check(two)
    assert sent(tg) == [
        (OWNER, "<b>✅ Visa slot watch started - Astana</b>"),
        (FRIEND, "<b>✅ Visa slot watch started - Astana</b>"),
        (OWNER, "<b>🟢 Sooner visa date opened - Astana</b>"),
        (FRIEND, "<b>🟢 Sooner visa date opened - Astana</b>"),
    ]
    assert state(two)["welcomed_chats"] == [OWNER, FRIEND]


def test_daily_summary_goes_to_everyone_quietly(two, ais, tg, clock):
    clock.advance(days=1, hours=3)  # 09:00 next day
    run_check(two)
    digests = [m for m in tg.fake.messages if "Daily visa slot summary" in m["text"]]
    assert [m["chat_id"] for m in digests] == [OWNER, FRIEND]
    assert {m["disable_notification"] for m in digests} == {"true"}


def test_housekeeping_alerts_go_to_the_owner_only(two, ais, tg, clock):
    ais.fake.status = 500
    for _ in range(3):
        clock.advance(hours=1)
        run_check(two)
    ais.fake.status = 200
    ais.fake.sessions.clear()
    clock.advance(hours=1)
    run_check(two)
    later = sent(tg)[2:]
    assert later == [
        (OWNER, "<b>⚠️ Visa slot watch: 3 checks in a row failed</b>"),
        (OWNER, "<b>🔑 Visa slot watch paused - the AIS session ended</b>"),
    ]


def test_chat_added_later_gets_one_welcome(cfg, ais, tg, clock):
    cfg.chat_id = OWNER
    ais.fake.dates = ["2027-09-01"]
    run_check(cfg, force=True, new_cookies=JAR)
    cfg.chat_id = f"{OWNER},{FRIEND}"  # you edit the settings
    clock.advance(hours=1)
    run_check(cfg)
    clock.advance(hours=1)
    run_check(cfg)
    welcomes = [m for m in tg.fake.messages if "subscribed" in m["text"]]
    assert [m["chat_id"] for m in welcomes] == [FRIEND]
    text = welcomes[0]["text"]
    assert "<b>👋 You're subscribed to the visa slot watch - Astana</b>" in text
    assert "Earliest date at the last check (Nov 5 07:00): Wed Sep 1, 2027" in text
    assert "You'll get an alert when a sooner date opens" in text
    assert welcomes[0]["disable_notification"] == "false"


def test_welcome_goes_out_even_while_the_session_is_expired(cfg, ais, tg, clock):
    cfg.chat_id = OWNER
    ais.fake.dates = ["2027-09-01"]
    run_check(cfg, force=True, new_cookies=JAR)
    ais.fake.sessions.clear()
    clock.advance(hours=1)
    run_check(cfg)  # expired
    cfg.chat_id = f"{OWNER},{FRIEND}"
    clock.advance(hours=1)
    run_check(cfg)  # skipped: no request, but the welcome still goes out
    assert sent(tg)[-1] == (FRIEND, "<b>👋 You're subscribed to the visa slot watch - Astana</b>")
    assert ais.fake.days_requests() == 2


def test_failed_welcome_is_retried_without_bothering_others(cfg, ais, tg, clock):
    cfg.chat_id = OWNER
    ais.fake.dates = ["2027-09-01"]
    run_check(cfg, force=True, new_cookies=JAR)
    cfg.chat_id = f"{OWNER},{FRIEND}"
    tg.fake.fail_chats = {FRIEND}  # e.g. they haven't sent /start yet
    clock.advance(hours=1)
    run_check(cfg)
    assert FRIEND not in state(cfg)["welcomed_chats"]
    tg.fake.fail_chats = set()
    clock.advance(hours=1)
    run_check(cfg)
    assert sent(tg)[1:] == [(FRIEND, "<b>👋 You're subscribed to the visa slot watch - Astana</b>")]


def test_partly_delivered_alert_is_not_resent(two, ais, tg, clock):
    tg.fake.fail_chats = {FRIEND}
    clock.advance(hours=1)
    ais.fake.dates = ["2027-08-01"]
    run_check(two)
    tg.fake.fail_chats = set()
    clock.advance(hours=1)
    run_check(two)  # same date: nothing new
    assert [c for c, line in sent(tg) if "Sooner" in line] == [OWNER]


def test_alert_that_reached_nobody_is_retried(two, ais, tg, clock):
    tg.fake.fail_chats = {OWNER, FRIEND}
    clock.advance(hours=1)
    ais.fake.dates = ["2027-08-01"]
    run_check(two)
    tg.fake.fail_chats = set()
    clock.advance(hours=1)
    run_check(two)
    assert [c for c, line in sent(tg) if "Sooner" in line] == [OWNER, FRIEND]


def test_state_from_before_chat_lists_welcomes_only_new_chats(cfg, ais, tg, clock):
    cfg.chat_id = OWNER
    ais.fake.dates = ["2027-09-01"]
    run_check(cfg, force=True, new_cookies=JAR)
    st = state(cfg)
    del st["welcomed_chats"]  # what the Pi's state.json looks like today
    (cfg.state_dir / "state.json").write_text(json.dumps(st))
    cfg.chat_id = f"{OWNER},{FRIEND}"
    clock.advance(hours=1)
    run_check(cfg)
    assert sent(tg)[1:] == [(FRIEND, "<b>👋 You're subscribed to the visa slot watch - Astana</b>")]


def test_removed_chat_is_forgotten_and_welcomed_again_if_re_added(two, ais, tg, clock):
    two.chat_id = OWNER
    clock.advance(hours=1)
    run_check(two)
    assert state(two)["welcomed_chats"] == [OWNER]
    two.chat_id = f"{OWNER},{FRIEND}"
    clock.advance(hours=1)
    run_check(two)
    assert sent(tg)[-1] == (FRIEND, "<b>👋 You're subscribed to the visa slot watch - Astana</b>")


def test_dry_run_shows_recipients(two, ais, tg, clock, capsys):
    clock.advance(hours=1)
    ais.fake.dates = ["2027-08-01"]
    run_check(two, dry_run=True)
    assert f"--- would send (sooner) to {OWNER}, {FRIEND} ---" in capsys.readouterr().out


def test_test_telegram_and_status_cover_every_chat(env_file, ais, tg, clock, capsys, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", f"{OWNER},{FRIEND}")
    tg.fake.fail_chats = {FRIEND}
    assert main(["--config", str(env_file), "--test-telegram"]) == 1
    out = capsys.readouterr().out
    assert f"{OWNER}: sent" in out and f"{FRIEND}: FAILED" in out
    ais.fake.dates = ["2027-09-01"]
    monkeypatch.setenv("TELEGRAM_CHAT_ID", OWNER)
    main(["--config", str(env_file), "--set-cookie", VALID_SESSION])
    monkeypatch.setenv("TELEGRAM_CHAT_ID", f"{OWNER},{FRIEND}")
    capsys.readouterr()
    main(["--config", str(env_file), "--status"])
    assert (
        f"Chats:      {OWNER} (owner), {FRIEND}; welcome on the next check: {FRIEND}"
        in capsys.readouterr().out
    )


def test_status_with_state_from_before_chat_lists(env_file, ais, tg, clock, capsys, monkeypatch):
    ais.fake.dates = ["2027-09-01"]
    monkeypatch.setenv("TELEGRAM_CHAT_ID", OWNER)
    main(["--config", str(env_file), "--set-cookie", VALID_SESSION])
    path = env_file.parent / "state" / "state.json"
    st = json.loads(path.read_text())
    del st["welcomed_chats"]
    path.write_text(json.dumps(st))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", f"{OWNER},{FRIEND}")
    capsys.readouterr()
    main(["--config", str(env_file), "--status"])
    assert f"; welcome on the next check: {FRIEND}\n" in capsys.readouterr().out
