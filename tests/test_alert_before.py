"""Per-chat alert cutoffs: /set_alert_before and who gets which sooner-date alert."""

import json

import pytest

from slot_machine import bot
from slot_machine.cli import main
from slot_machine.runner import run_check
from slot_machine.watcher import process

from fakes import VALID_SESSION
from helpers import T0, make_cfg, ok

JAR = {"_yatri_session": VALID_SESSION}
OWNER, FRIEND = "42", "77"


def replies(tg, chat) -> list[str]:
    return [m["text"] for m in tg.fake.messages if m["chat_id"] == chat]


def sooner_to(tg) -> list[str]:
    return [m["chat_id"] for m in tg.fake.messages if "Sooner visa date opened" in m["text"]]


def say(live, tg, chat, text):
    tg.fake.messages.clear()
    tg.fake.say(chat, text)
    bot.poll(live, 0)
    return replies(tg, chat)


@pytest.fixture
def live(cfg, ais, tg, clock):
    cfg.chat_id = f"{OWNER},{FRIEND}"
    ais.fake.dates = ["2027-09-01"]
    run_check(cfg, force=True, new_cookies=JAR)
    bot.poll(cfg, 0)  # handle the fake's initial "hi"
    tg.fake.messages.clear()
    return cfg


# --------------------------------------------------------------------------- the decision logic


def started(cfg):
    state = {}
    process(state, ok("2027-09-01"), T0, cfg)
    return state


def test_each_chat_gets_only_dates_before_its_own_cutoff():
    cfg = make_cfg(chat_id=f"{OWNER},{FRIEND}")
    state = started(cfg)
    cutoffs = {FRIEND: "2027-08-15"}
    [m] = process(state, ok("2027-08-20"), T0, cfg, cutoffs)
    assert m.kind == "sooner" and m.to == [OWNER]  # Aug 20 isn't before the friend's Aug 15
    [mine, theirs] = process(state, ok("2027-08-10"), T0, cfg, cutoffs)
    assert (mine.to, theirs.to) == ([OWNER], [FRIEND])  # each compared with its own cutoff
    assert "before your current appointment" not in mine.text  # the owner has none: vs the last check
    assert "5 days before your current appointment, Sun Aug 15, 2027." in theirs.text


def test_alert_compares_with_the_appointment_not_the_previous_check():
    """2026-11-06: "Was Aug 22, 2028 at the previous check: 97 days sooner" said little; what
    matters is how much sooner than the appointment you already have."""
    cfg = make_cfg(chat_id=f"{OWNER},{FRIEND}", alert_only_before="2028-05-24")
    state = started(cfg)
    state["last_seen"] = "2028-08-22"
    [m] = process(state, ok("2028-05-17"), T0, cfg)
    assert m.to == [OWNER, FRIEND]  # same cutoff: one message for both
    assert m.text.splitlines()[1:3] == [
        "Earliest: Wed May 17, 2028 (in 559 days)",
        "7 days before your current appointment, Wed May 24, 2028.",
    ]
    assert "previous check" not in m.text


def test_chats_with_the_same_cutoff_share_one_alert():
    cfg = make_cfg(chat_id=f"{OWNER},{FRIEND},99", alert_only_before="2027-09-01")
    state = started(cfg)
    msgs = process(state, ok("2027-08-10"), T0, cfg, {"99": "2027-08-20"})
    assert [m.to for m in msgs] == [[OWNER, FRIEND], ["99"]]
    assert "22 days before" in msgs[0].text and "10 days before" in msgs[1].text


def test_settings_default_applies_until_a_chat_sets_its_own():
    cfg = make_cfg(chat_id=f"{OWNER},{FRIEND}", alert_only_before="2027-08-15")
    state = started(cfg)
    assert process(state, ok("2027-08-20"), T0, cfg) == []  # nobody: both use the default
    [m] = process(state, ok("2027-08-19"), T0, cfg, {OWNER: ""})  # owner said "off"
    assert m.to == [OWNER]


def test_no_alert_when_nobody_wants_that_date():
    cfg = make_cfg(chat_id=f"{OWNER},{FRIEND}")
    state = started(cfg)
    assert process(state, ok("2027-08-20"), T0, cfg, {OWNER: "2027-08-01", FRIEND: "2027-07-01"}) == []
    assert state["last_seen"] == "2027-08-20"  # still tracked, as before


# --------------------------------------------------------------------------- the command


def test_set_alert_before(live, tg):
    assert say(live, tg, FRIEND, "/set_alert_before 2027-06-30") == [
        "OK. You'll get sooner-date alerts only for dates before Wed Jun 30, 2027 (in 237 days).\n"
        "The earliest date at the last check, Sep 1, 2027, is after that.\n"
        "The daily summary still comes."
    ]
    saved = json.loads((live.state_dir / "telegram.json").read_text())["alert_before"]
    assert saved == {FRIEND: "2027-06-30"}


def test_cutoff_already_beaten_says_so(live, tg):
    [reply] = say(live, tg, FRIEND, "/set_alert_before 2027-12-31")
    assert "The earliest date at the last check, Sep 1, 2027, is already before that." in reply


def test_show_setting(live, tg):
    assert say(live, tg, FRIEND, "/set_alert_before") == [
        "You get an alert for every sooner date (the default from the settings).\n"
        "Change it: /set_alert_before YYYY-MM-DD, or /set_alert_before off"
    ]
    say(live, tg, FRIEND, "/set_alert_before 2027-06-30")
    [reply] = say(live, tg, FRIEND, "/set_alert_before")
    assert reply.startswith("You get alerts only for dates before Wed Jun 30, 2027 (in 237 days).")


@pytest.mark.parametrize("word", ["off", "OFF", "none", "clear", "all"])
def test_off_means_every_sooner_date(live, tg, word):
    say(live, tg, FRIEND, "/set_alert_before 2027-06-30")
    assert say(live, tg, FRIEND, f"/set_alert_before {word}") == [
        "OK. You'll get an alert for every sooner date."
    ]
    assert json.loads((live.state_dir / "telegram.json").read_text())["alert_before"] == {FRIEND: ""}


@pytest.mark.parametrize("arg", ["30.06.2027", "2027-13-01", "2027-02-30", "tomorrow", "<b>x</b>"])
def test_bad_dates_are_refused(live, tg, arg):
    [reply] = say(live, tg, FRIEND, f"/set_alert_before {arg}")
    assert reply.startswith("That's not a date I can use:")
    assert "<b>" not in reply  # escaped, Telegram would reject broken HTML
    assert (
        "alert_before" not in json.loads((live.state_dir / "telegram.json").read_text())
        or not json.loads((live.state_dir / "telegram.json").read_text())["alert_before"]
    )


@pytest.mark.parametrize("arg", ["2026-11-05", "2026-01-01"])
def test_past_dates_are_refused(live, tg, arg):
    [reply] = say(live, tg, FRIEND, f"/set_alert_before {arg}")
    assert "has already passed" in reply


def test_strangers_cannot_set_anything(live, tg):
    tg.fake.say("99", "/set_alert_before 2027-01-01")
    bot.poll(live, 0)
    assert replies(tg, "99") == []
    assert "alert_before" not in json.loads((live.state_dir / "telegram.json").read_text())


# --------------------------------------------------------------------------- end to end


def test_cutoff_changes_who_gets_the_next_alert(live, ais, tg, clock):
    say(live, tg, FRIEND, "/set_alert_before 2027-08-15")
    tg.fake.messages.clear()
    clock.advance(hours=1)
    ais.fake.dates = ["2027-08-20"]
    run_check(live)
    assert sooner_to(tg) == [OWNER]
    clock.advance(hours=1)
    ais.fake.dates = ["2027-08-10"]
    run_check(live)
    assert sooner_to(tg) == [OWNER, OWNER, FRIEND]


def test_status_shows_each_chats_cutoff(live, tg, env_file, capsys, monkeypatch):
    say(live, tg, FRIEND, "/set_alert_before 2027-06-30")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", f"{OWNER},{FRIEND}")
    capsys.readouterr()
    main(["--config", str(env_file), "--status"])
    assert f"Chats:      {OWNER} (owner), {FRIEND} [before 2027-06-30]" in capsys.readouterr().out


def test_help_lists_the_command(live, tg):
    [reply] = say(live, tg, FRIEND, "/help")
    assert "/set_alert_before YYYY-MM-DD" in reply


def test_with_a_cutoff_every_change_before_it_alerts():
    """The table from 2026-11-06: appointment May 24, 2028; earliest date at each check."""
    cfg = make_cfg(chat_id=OWNER, alert_only_before="2028-05-24")
    state = started(cfg)
    state["last_seen"] = "2028-08-22"
    heard = []
    for earliest in [
        "2028-05-17",
        "2028-05-17",
        "2028-05-20",
        "2028-05-20",
        "2028-05-10",
        "2028-08-22",
        "2028-05-20",
    ]:
        msgs = process(state, ok(earliest), T0, cfg)
        heard.append(msgs[0].text.splitlines()[0] if msgs else None)
    sooner = "<b>🟢 Sooner visa date opened - Astana</b>"
    later = "<b>🟡 Earliest visa date moved later, still before your appointment - Astana</b>"
    assert heard == [sooner, None, later, None, sooner, None, sooner]


def test_later_date_says_the_earlier_one_is_gone():
    cfg = make_cfg(chat_id=OWNER, alert_only_before="2028-05-24")
    state = started(cfg)
    state["last_seen"] = "2028-05-17"
    [m] = process(state, ok("2028-05-20"), T0, cfg)
    assert m.text.splitlines()[1:4] == [
        "Earliest: Sat May 20, 2028 (in 562 days)",
        "4 days before your current appointment, Wed May 24, 2028.",
        "May 17, 2028 is no longer open.",
    ]
    assert m.silent is False and m.rollback == {"last_seen": "2028-05-17"}


def test_without_a_cutoff_only_sooner_dates_alert():
    cfg = make_cfg(chat_id=f"{OWNER},{FRIEND}", alert_only_before="2028-05-24")
    state = started(cfg)
    state["last_seen"] = "2028-05-17"
    [m] = process(state, ok("2028-05-20"), T0, cfg, {FRIEND: ""})  # the friend turned the cutoff off
    assert m.to == [OWNER]
    mine, theirs = process(state, ok("2028-05-12"), T0, cfg, {FRIEND: ""})  # sooner: both hear it
    assert (mine.to, theirs.to) == ([OWNER], [FRIEND])
    assert "Was May 20, 2028 at the previous check: 8 days sooner." in theirs.text
    assert process(state, ok("2028-05-12"), T0, cfg, {FRIEND: ""}) == []  # unchanged: nobody


def test_a_later_date_past_the_cutoff_is_silent():
    cfg = make_cfg(chat_id=OWNER, alert_only_before="2028-05-24")
    state = started(cfg)
    state["last_seen"] = "2028-05-17"
    assert process(state, ok("2028-05-30"), T0, cfg) == []
