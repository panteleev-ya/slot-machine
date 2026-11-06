"""The "watch resumed" message after a renewal the bot asked for: it goes to every chat that
heard the session ended, so nobody else signs in for nothing."""

import json
from datetime import timedelta

import pytest

from slot_machine import renew
from slot_machine.ais import FetchResult
from slot_machine.runner import run_check
from slot_machine.watcher import EXPIRY_TOLD, process

from fakes import VALID_SESSION
from helpers import T0, make_cfg, ok

JAR = {"_yatri_session": VALID_SESSION}
FRESH = {"_yatri_session": "f" * 40}
OWNER, FRIEND = "42", "77"


def lines(tg, chat=None) -> list[str]:
    return [m["text"].splitlines()[0] for m in tg.fake.messages if chat is None or m["chat_id"] == chat]


def resumed(tg) -> list[dict]:
    return [m for m in tg.fake.messages if "Visa slot watch resumed" in m["text"]]


def state(cfg) -> dict:
    return json.loads((cfg.state_dir / "state.json").read_text())


@pytest.fixture
def ended(cfg, ais, tg, clock):
    """Two chats; the session ended an hour after the baseline and both were told."""
    cfg.chat_id = f"{OWNER},{FRIEND}"
    ais.fake.dates = ["2027-09-01"]
    ais.fake.sessions.add(FRESH["_yatri_session"])
    run_check(cfg, force=True, new_cookies=JAR)
    ais.fake.sessions.discard(VALID_SESSION)
    clock.advance(hours=1)
    run_check(cfg)
    for chat in (OWNER, FRIEND):
        assert lines(tg, chat)[-1] == "<b>🔑 Visa slot watch paused - the AIS session ended</b>"
    assert state(cfg)[EXPIRY_TOLD] == [OWNER, FRIEND]
    tg.fake.messages.clear()
    return cfg


def test_renewal_after_the_alert_is_confirmed_to_everyone_told(ended, tg, clock):
    clock.advance(minutes=20)
    run_check(ended, force=True, new_cookies=FRESH)
    msgs = resumed(tg)
    assert [m["chat_id"] for m in msgs] == [OWNER, FRIEND]
    for msg in msgs:
        assert msg["text"] == (
            "<b>🔓 Visa slot watch resumed - Astana</b>\n"
            "The new session works; checks continue on schedule. Checks were paused for 20m.\n"
            "Earliest date now: Wed Sep 1, 2027 (in 300 days)"
        )
    assert "renew_requested_at" not in state(ended) and EXPIRY_TOLD not in state(ended)
    clock.advance(hours=1)
    run_check(ended)
    assert len(resumed(tg)) == 2  # once per chat


def test_renewal_through_the_extension_is_confirmed_too(ended, tg, clock):
    ended.renew_url, ended.renew_token = "http://yarikos.local:8080", "T" * 43
    clock.advance(minutes=5)
    status, result = renew.apply_cookie(ended, FRESH["_yatri_session"], via="extension")
    assert status == 200 and result["ok"]
    assert [m["chat_id"] for m in resumed(tg)] == [OWNER, FRIEND]


def test_only_chats_that_heard_it_ended_hear_it_resumed(cfg, ais, tg, clock):
    cfg.chat_id = f"{OWNER},{FRIEND}"
    ais.fake.dates = ["2027-09-01"]
    ais.fake.sessions.add(FRESH["_yatri_session"])
    run_check(cfg, force=True, new_cookies=JAR)
    ais.fake.sessions.discard(VALID_SESSION)
    tg.fake.fail_chats = {FRIEND}
    clock.advance(hours=1)
    run_check(cfg)  # the "session ended" alert reaches only the owner
    tg.fake.fail_chats = set()
    run_check(cfg, force=True, new_cookies=FRESH)
    assert [m["chat_id"] for m in resumed(tg)] == [OWNER]


def test_a_chat_removed_meanwhile_hears_nothing(ended, tg):
    ended.chat_id = OWNER
    run_check(ended, force=True, new_cookies=FRESH)
    assert [m["chat_id"] for m in resumed(tg)] == [OWNER]


def test_renewing_before_any_alert_sends_nothing(cfg, ais, tg, clock):
    ais.fake.dates = ["2027-09-01"]
    ais.fake.sessions.add(FRESH["_yatri_session"])
    run_check(cfg, force=True, new_cookies=JAR)
    clock.advance(minutes=30)
    run_check(cfg, force=True, new_cookies=FRESH)  # you renewed early, unasked
    assert resumed(tg) == []


def test_no_confirmation_if_the_alert_never_arrived(cfg, ais, tg, clock):
    ais.fake.dates = ["2027-09-01"]
    ais.fake.sessions.add(FRESH["_yatri_session"])
    run_check(cfg, force=True, new_cookies=JAR)
    ais.fake.sessions.discard(VALID_SESSION)
    tg.fake.fail_chats = {"42"}
    clock.advance(hours=1)
    run_check(cfg)  # the "session ended" alert fails, so it's rolled back
    assert state(cfg)["session_expired_at"] is None
    tg.fake.fail_chats = set()
    run_check(cfg, force=True, new_cookies=FRESH)
    assert resumed(tg) == []


def test_pause_counts_from_the_first_alert_after_a_failed_attempt(ended, ais, tg, clock):
    clock.advance(minutes=10)
    run_check(ended, force=True, new_cookies={"_yatri_session": "z" * 40})  # the site rejects it
    assert tg.fake.messages == []  # everyone already knows it ended; the sender sees the answer
    clock.advance(minutes=20)
    run_check(ended, force=True, new_cookies=FRESH)
    msgs = resumed(tg)
    assert [m["chat_id"] for m in msgs] == [OWNER, FRIEND]
    assert "Checks were paused for 30m." in msgs[0]["text"]


def test_confirmation_when_the_first_check_finds_no_dates(ended, ais, tg, clock):
    ais.fake.dates = []
    run_check(ended, force=True, new_cookies=FRESH)
    msg = resumed(tg)[0]
    assert "No dates came back at the first check (none open, or the site is throttling)." in msg["text"]


def test_failed_confirmation_is_retried(ended, tg, clock):
    tg.fake.fail_chats = {OWNER, FRIEND}
    run_check(ended, force=True, new_cookies=FRESH)
    assert resumed(tg) == [] and state(ended)["renew_requested_at"]
    assert state(ended)[EXPIRY_TOLD] == [OWNER, FRIEND]
    tg.fake.fail_chats = set()
    clock.advance(hours=1)
    run_check(ended)
    assert [m["chat_id"] for m in resumed(tg)] == [OWNER, FRIEND]


def test_decision_logic():
    cfg = make_cfg(chat_id=f"{OWNER},{FRIEND}")
    st = {
        "baseline_sent": True,
        "last_seen": "2027-09-01",
        "renew_requested_at": T0.isoformat(),
        "welcomed_chats": [OWNER, FRIEND],
    }
    later = T0 + timedelta(minutes=30)
    still = dict(st)
    # a rejected renewal: no second "session ended", and the original end time stays
    assert process(still, FetchResult("expired", detail="HTTP 302"), later, cfg) == []
    assert still["renew_requested_at"] == still["session_expired_at"] == T0.isoformat()
    # without a renewal in progress, an expiry is announced as usual
    fresh = {k: v for k, v in st.items() if k != "renew_requested_at"}
    assert [m.kind for m in process(fresh, FetchResult("expired", detail="HTTP 302"), later, cfg)] == [
        "expired"
    ]
    # state from before everyone was told: only the owner heard the session ended
    [m] = [m for m in process(dict(st), ok("2027-09-01"), T0, cfg) if m.kind == "renewed"]
    assert m.to == [OWNER] and m.rollback == {"renew_requested_at": T0.isoformat(), EXPIRY_TOLD: None}
    told = dict(st, **{EXPIRY_TOLD: [FRIEND, OWNER]})
    [m] = [m for m in process(told, ok("2027-09-01"), T0, cfg) if m.kind == "renewed"]
    assert m.to == [OWNER, FRIEND] and EXPIRY_TOLD not in told
