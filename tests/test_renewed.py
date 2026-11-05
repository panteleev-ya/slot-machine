"""The "watch resumed" message after a renewal the bot asked for."""

import json

import pytest

from slot_machine import renew
from slot_machine.ais import FetchResult
from slot_machine.runner import run_check
from slot_machine.watcher import process

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
    """Two chats; the session ended an hour after the baseline and the owner was told."""
    cfg.chat_id = f"{OWNER},{FRIEND}"
    ais.fake.dates = ["2027-09-01"]
    ais.fake.sessions.add(FRESH["_yatri_session"])
    run_check(cfg, force=True, new_cookies=JAR)
    ais.fake.sessions.discard(VALID_SESSION)
    clock.advance(hours=1)
    run_check(cfg)
    assert lines(tg, OWNER)[-1] == "<b>🔑 Visa slot watch paused - the AIS session ended</b>"
    tg.fake.messages.clear()
    return cfg


def test_renewal_after_the_alert_is_confirmed_to_the_owner(ended, tg, clock):
    clock.advance(minutes=20)
    run_check(ended, force=True, new_cookies=FRESH)
    [msg] = resumed(tg)
    assert msg["chat_id"] == OWNER
    assert msg["text"] == (
        "<b>🔓 Visa slot watch resumed - Astana</b>\n"
        "The new session works; checks continue on schedule. Checks were paused for 20m.\n"
        "Earliest date now: Wed Sep 1, 2027 (in 300 days)"
    )
    assert "renew_requested_at" not in state(ended)
    clock.advance(hours=1)
    run_check(ended)
    assert len(resumed(tg)) == 1  # once


def test_renewal_through_the_extension_is_confirmed_too(ended, tg, clock):
    ended.renew_url, ended.renew_token = "http://yarikos.local:8080", "T" * 43
    clock.advance(minutes=5)
    status, result = renew.apply_cookie(ended, FRESH["_yatri_session"], via="extension")
    assert status == 200 and result["ok"]
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
    assert lines(tg, OWNER) == ["<b>🔑 Visa slot watch paused - the AIS session ended</b>"]
    clock.advance(minutes=20)
    run_check(ended, force=True, new_cookies=FRESH)
    [msg] = resumed(tg)
    assert "Checks were paused for 30m." in msg["text"]


def test_confirmation_when_the_first_check_finds_no_dates(ended, ais, tg, clock):
    ais.fake.dates = []
    run_check(ended, force=True, new_cookies=FRESH)
    [msg] = resumed(tg)
    assert "No dates came back at the first check (none open, or the site is throttling)." in msg["text"]


def test_failed_confirmation_is_retried(ended, tg, clock):
    tg.fake.fail_chats = {OWNER}
    run_check(ended, force=True, new_cookies=FRESH)
    assert resumed(tg) == [] and state(ended)["renew_requested_at"]
    tg.fake.fail_chats = set()
    clock.advance(hours=1)
    run_check(ended)
    assert [m["chat_id"] for m in resumed(tg)] == [OWNER]


def test_decision_logic():
    cfg = make_cfg(chat_id=f"{OWNER},{FRIEND}")
    st = {
        "baseline_sent": True,
        "last_seen": "2027-09-01",
        "renew_requested_at": T0.isoformat(),
        "welcomed_chats": [OWNER, FRIEND],
    }
    assert [m.kind for m in process(dict(st), FetchResult("expired", detail="HTTP 302"), T0, cfg)] == [
        "expired"
    ]
    still = dict(st)
    process(still, FetchResult("expired", detail="HTTP 302"), T0, cfg)
    assert still["renew_requested_at"] == T0.isoformat()  # kept until a session works
    [m] = [m for m in process(dict(st), ok("2027-09-01"), T0, cfg) if m.kind == "renewed"]
    assert m.to == [OWNER] and m.rollback == {"renew_requested_at": T0.isoformat()}
