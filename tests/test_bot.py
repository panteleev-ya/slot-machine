"""Telegram commands: /check from listed chats, nothing for anyone else."""

import json

import pytest

from slot_machine import bot
from slot_machine.cli import main
from slot_machine.runner import run_check
from slot_machine.store import single_instance

from fakes import VALID_SESSION

JAR = {"_yatri_session": VALID_SESSION}
OWNER, FRIEND, STRANGER = "42", "77", "99"


def replies(tg, chat) -> list[str]:
    return [m["text"] for m in tg.fake.messages if m["chat_id"] == chat]


@pytest.fixture
def live(cfg, ais, tg, clock):
    """Two listed chats, cookie set, baseline sent (earliest Sep 1); the 'hi' already handled."""
    cfg.chat_id = f"{OWNER},{FRIEND}"
    ais.fake.dates = ["2027-09-01"]
    run_check(cfg, force=True, new_cookies=JAR)
    bot.poll(cfg, 0)
    tg.fake.messages.clear()
    return cfg


def test_check_from_a_listed_chat_runs_a_check_and_answers(live, ais, tg, clock, capsys):
    clock.advance(minutes=1)  # well inside MIN_INTERVAL_MINUTES: /check ignores it
    tg.fake.say(FRIEND, "/check", "yanika")
    bot.poll(live, 0)
    assert ais.fake.days_requests() == 2
    assert replies(tg, FRIEND) == [
        "✅ Checked just now. Earliest date: Wed Sep 1, 2027 (in 300 days)\n"
        "If it's sooner than before, everyone also gets the usual alert."
    ]
    assert replies(tg, OWNER) == []
    assert "check: ok 2027-09-01 [telegram:77]" in capsys.readouterr().out


def test_sooner_date_found_by_check_alerts_everyone(live, ais, tg, clock):
    clock.advance(minutes=1)
    ais.fake.dates = ["2027-08-01"]
    tg.fake.say(OWNER, "/check")
    bot.poll(live, 0)
    assert [m["chat_id"] for m in tg.fake.messages if "Sooner visa date opened" in m["text"]] == [
        OWNER,
        FRIEND,
    ]
    assert replies(tg, OWNER)[-1].startswith("✅ Checked just now. Earliest date: Sun Aug 1, 2027")


def test_strangers_get_nothing_but_find_chat_id_still_shows_them(live, ais, tg, clock, capsys, env_file):
    tg.fake.say(STRANGER, "/check", "stranger")
    tg.fake.say(STRANGER, "/start", "stranger")
    bot.poll(live, 0)
    assert replies(tg, STRANGER) == []
    assert ais.fake.days_requests() == 1
    assert "message from 99, not in TELEGRAM_CHAT_ID; ignored" in capsys.readouterr().out
    # the next poll confirms them, and Telegram forgets them; --find-chat-id still lists the stranger
    bot.poll(live, 0)
    assert tg.fake.updates == []
    seen = json.loads((live.state_dir / "telegram.json").read_text())["seen_chats"]
    assert seen == {STRANGER: "stranger"}
    main(["--config", str(env_file), "--find-chat-id"])
    out = capsys.readouterr().out
    assert "TELEGRAM_CHAT_ID=99    (stranger)" in out


def test_find_chat_id_marks_chats_already_listed(env_file, tg, capsys):
    main(["--config", str(env_file), "--find-chat-id"])
    assert "TELEGRAM_CHAT_ID=42    (yarik)  already in TELEGRAM_CHAT_ID" in capsys.readouterr().out


def test_find_chat_id_while_the_poller_holds_updates(env_file, tg, capsys):
    tg.fake.updates_status = 409
    assert main(["--config", str(env_file), "--find-chat-id"]) == 0
    assert "No messages yet" in capsys.readouterr().out


def test_cooldown_between_checks(live, ais, tg, clock):
    tg.fake.say(OWNER, "/check")
    tg.fake.say(FRIEND, "/check")
    bot.poll(live, 0)
    assert ais.fake.days_requests() == 2  # baseline + one /check
    assert replies(tg, FRIEND) == ["A check ran less than a minute ago. Try again in 2m."]
    clock.advance(minutes=2)
    tg.fake.say(FRIEND, "/check")
    bot.poll(live, 0)
    assert ais.fake.days_requests() == 3


def test_each_command_is_handled_once(live, ais, tg, clock):
    tg.fake.say(OWNER, "/check")
    bot.poll(live, 0)
    clock.advance(minutes=5)
    bot.poll(live, 0)
    bot.poll(live, 0)
    assert ais.fake.days_requests() == 2


def test_a_crash_mid_command_never_reruns_it(live, ais, tg, clock, monkeypatch):
    tg.fake.say(OWNER, "/check")

    def boom(*a, **kw):
        raise RuntimeError("power cut")

    monkeypatch.setattr(bot, "check", boom)
    with pytest.raises(RuntimeError):
        bot.poll(live, 0)
    monkeypatch.undo()
    bot.poll(live, 0)
    assert ais.fake.days_requests() == 1  # the /check was skipped, not repeated


@pytest.mark.parametrize("text", ["/help", "/start", "/whatever", "/check_status"])
def test_other_commands_get_help(live, tg, text):
    tg.fake.say(FRIEND, text)
    bot.poll(live, 0)
    assert replies(tg, FRIEND) == [bot.HELP]


def test_plain_text_gets_no_answer(live, tg):
    tg.fake.say(FRIEND, "thanks!")
    bot.poll(live, 0)
    assert replies(tg, FRIEND) == []


def test_command_addressed_to_the_bot_by_name(live, ais, tg, clock):
    tg.fake.say(FRIEND, "/check@SlotBot please")
    bot.poll(live, 0)
    assert ais.fake.days_requests() == 2


def test_check_with_expired_session(live, ais, tg, clock):
    ais.fake.sessions.clear()
    clock.advance(hours=1)
    run_check(live)  # expires
    tg.fake.messages.clear()
    clock.advance(minutes=5)
    tg.fake.say(FRIEND, "/check")
    bot.poll(live, 0)
    assert replies(tg, FRIEND) == [
        "The visa-site session has ended, so nothing was checked. The owner needs to sign in again."
    ]
    assert ais.fake.days_requests() == 2


def test_check_while_a_scheduled_check_runs(live, tg, clock):
    tg.fake.say(FRIEND, "/check")
    with single_instance(live.state_dir):  # the cron check holds the main lock
        bot.poll(live, 0)
    assert replies(tg, FRIEND) == [
        "Another check is running right now. Its result arrives as the usual alerts."
    ]


def test_check_that_gets_an_empty_answer(live, ais, tg, clock):
    ais.fake.dates = []
    tg.fake.say(FRIEND, "/check")
    bot.poll(live, 0)
    assert replies(tg, FRIEND) == [
        "No dates came back (none open, or the site is throttling). Scheduled checks pause for 3h 0m."
    ]


def test_check_that_fails(live, ais, tg, clock):
    ais.fake.status = 500
    tg.fake.say(FRIEND, "/check")
    bot.poll(live, 0)
    assert replies(tg, FRIEND) == ["⚠️ The check failed: HTTP 500"]


def test_telegram_unreachable_or_busy(live, tg, capsys):
    tg.fake.updates_status = 409
    assert bot.poll(live, 0) == 0
    assert "telegram getUpdates HTTP 409" in capsys.readouterr().out


def test_second_poller_steps_aside(live, tg):
    tg.fake.say(FRIEND, "/check")
    with single_instance(live.state_dir, bot.LOCK_NAME):
        assert bot.poll(live, 0) == 0
    assert tg.fake.messages == []
    assert tg.fake.updates[-1]["message"]["text"] == "/check"  # left for the poller holding the lock


def test_not_configured(cfg, capsys):
    cfg.chat_id = ""
    assert bot.poll(cfg, 0) == 0
    assert "telegram not configured; not polling" in capsys.readouterr().out


def test_poll_from_the_command_line(env_file, ais, tg, clock):
    ais.fake.dates = ["2027-09-01"]
    main(["--config", str(env_file), "--set-cookie", VALID_SESSION])
    tg.fake.say("42", "/check")
    clock.advance(minutes=3)
    assert main(["--config", str(env_file), "--poll-telegram", "0"]) == 0
    assert tg.fake.messages[-1]["text"].startswith("✅ Checked just now")


def test_cron_runs_are_tagged_in_the_log(env_file, ais, clock, capsys):
    ais.fake.dates = ["2027-09-01"]
    main(["--config", str(env_file), "--set-cookie", VALID_SESSION])
    assert "check: ok 2027-09-01 [set-cookie]" in capsys.readouterr().out
    clock.advance(minutes=5)
    main(["--config", str(env_file), "--via", "cron"])
    assert "(< MIN_INTERVAL_MINUTES); skipping [cron]" in capsys.readouterr().out
    clock.advance(hours=1)
    main(["--config", str(env_file), "--via", "cron"])
    assert "check: ok 2027-09-01 [cron]" in capsys.readouterr().out


def test_commands_queued_while_offline_are_ignored(live, ais, tg, clock, capsys):
    old = int(clock.now.timestamp()) - 11 * 60
    tg.fake.say(FRIEND, "/check", date=old)
    tg.fake.say(STRANGER, "/start", "stranger", date=old)
    tg.fake.say(OWNER, "/check", date=int(clock.now.timestamp()) - 60)
    bot.poll(live, 0)
    assert replies(tg, FRIEND) == []
    assert ais.fake.days_requests() == 2  # only the owner's fresh one
    assert "/check from 77 is too old to act on; ignored" in capsys.readouterr().out
    assert json.loads((live.state_dir / "telegram.json").read_text())["seen_chats"] == {STRANGER: "stranger"}
