"""The decision logic: one check result + saved state -> messages to send."""

from datetime import timedelta

from visa_watch import FetchResult, current_earliest, process

from helpers import T0, empty, kinds, make_cfg, ok

SEP1, AUG20, AUG10, AUG1 = "2027-09-01", "2027-08-20", "2027-08-10", "2027-08-01"


def started(cfg=None, first=SEP1):
    """State after the first good check (baseline sent on day 1 at 06:00)."""
    cfg = cfg or make_cfg()
    state = {}
    assert kinds(process(state, ok(first), T0, cfg)) == ["baseline"]
    return state, cfg


# --------------------------------------------------------------------------- baseline


def test_first_good_check_sends_baseline_and_counts_as_todays_summary():
    state, msgs = {}, None
    msgs = process(state, ok(SEP1, "2027-10-01"), T0, make_cfg())
    assert kinds(msgs) == ["baseline"]
    assert "Earliest date now: Wed Sep 1, 2027" in msgs[0].text
    assert "quiet summary daily around 09:00" in msgs[0].text
    assert state["last_seen"] == SEP1
    assert state["last_digest_date"] == "2026-11-05"
    assert state["last_digest_earliest"] == SEP1
    assert state["baseline_sent"] is True


def test_baseline_mentions_alert_cutoff():
    msgs = process({}, ok(SEP1), T0, make_cfg(alert_only_before="2027-06-30"))
    assert "sooner date opens before Jun 30, 2027" in msgs[0].text


def test_first_check_empty_sends_baseline_without_date():
    state = {}
    msgs = process(state, empty(), T0, make_cfg())
    assert kinds(msgs) == ["baseline"]
    assert "No dates are offered right now" in msgs[0].text
    assert state["last_digest_earliest"] is None


# --------------------------------------------------------------------------- sooner alerts


def test_sooner_date_alerts_loudly_with_the_difference():
    state, cfg = started()
    msgs = process(state, ok(AUG1), T0 + timedelta(hours=1), cfg)
    assert kinds(msgs) == ["sooner"]
    assert msgs[0].silent is False
    assert "Earliest: Sun Aug 1, 2027" in msgs[0].text
    assert "Was Sep 1, 2027 at the previous check: 31 days sooner." in msgs[0].text
    assert cfg.appointment_url in msgs[0].text
    assert state["last_seen"] == AUG1


def test_same_or_later_date_is_quiet():
    state, cfg = started()
    assert process(state, ok(SEP1), T0 + timedelta(hours=1), cfg) == []
    assert process(state, ok("2027-10-01"), T0 + timedelta(hours=2), cfg) == []
    assert state["last_seen"] == "2027-10-01"


def test_slot_that_disappears_and_returns_alerts_again():
    state, cfg = started()
    assert kinds(process(state, ok(AUG1), T0 + timedelta(hours=1), cfg)) == ["sooner"]
    assert process(state, ok(SEP1), T0 + timedelta(hours=2), cfg) == []
    assert kinds(process(state, ok(AUG1), T0 + timedelta(hours=3), cfg)) == ["sooner"]


def test_alert_only_before_cutoff():
    state, cfg = started(make_cfg(alert_only_before="2027-08-15"))
    assert process(state, ok(AUG20), T0 + timedelta(hours=1), cfg) == []  # sooner, but not before cutoff
    assert state["last_seen"] == AUG20
    assert kinds(process(state, ok(AUG10), T0 + timedelta(hours=2), cfg)) == ["sooner"]


# --------------------------------------------------------------------------- empty answers


def test_empty_answer_backs_off_but_keeps_last_date():
    state, cfg = started()
    now = T0 + timedelta(hours=1)
    assert process(state, empty(), now, cfg) == []
    assert state["backoff_until"] == (now + timedelta(hours=3)).isoformat()
    assert state["last_seen"] == SEP1
    assert current_earliest(state) is None  # the newest good answer had no dates
    # the same date coming back after a short empty spell is not news
    assert process(state, ok(SEP1), now + timedelta(hours=3), cfg) == []
    assert state["backoff_until"] is None and state["empty_since"] is None


def test_a_day_of_empty_answers_means_dates_are_really_gone():
    state, cfg = started()
    process(state, empty(), T0 + timedelta(hours=1), cfg)
    process(state, empty(), T0 + timedelta(hours=25), cfg)
    assert state["last_seen"] is None
    msgs = process(state, ok(SEP1), T0 + timedelta(hours=26), cfg)
    assert "sooner" in kinds(msgs)
    sooner = next(m for m in msgs if m.kind == "sooner")
    assert "Visa dates available again" in sooner.text


# --------------------------------------------------------------------------- session / errors


def test_expired_session_alerts_with_how_long_it_lasted():
    state, cfg = started()
    state["session_set_at"] = T0.isoformat()
    process(state, ok(SEP1), T0 + timedelta(hours=5), cfg)
    msgs = process(state, FetchResult("expired", detail="HTTP 302"), T0 + timedelta(hours=6), cfg)
    assert kinds(msgs) == ["expired"]
    assert "That session lasted 5h 0m." in msgs[0].text
    assert "--set-cookie" in msgs[0].text
    assert state["session_expired_at"] == (T0 + timedelta(hours=6)).isoformat()


def test_error_alert_after_three_in_a_row_once_then_resets():
    state, cfg = started()
    err = FetchResult("error", detail="HTTP 500")
    t = T0
    sent = []
    for _ in range(5):
        t += timedelta(hours=1)
        sent += kinds(process(state, err, t, cfg))
    assert sent == ["error"]
    assert state["consecutive_errors"] == 5
    process(state, ok(SEP1), t + timedelta(hours=1), cfg)
    assert state["consecutive_errors"] == 0 and state["error_alert_sent"] is False


def test_error_alert_text():
    state, cfg = started()
    msgs = []
    for i in range(3):
        msgs = process(state, FetchResult("error", detail="HTTP 502"), T0 + timedelta(hours=i + 1), cfg)
    assert "3 checks in a row failed" in msgs[0].text
    assert "Last error: HTTP 502" in msgs[0].text


def test_throttled_backs_off_and_counts_as_failure():
    state, cfg = started()
    now = T0 + timedelta(hours=1)
    process(state, FetchResult("throttled", detail="HTTP 429"), now, cfg)
    assert state["backoff_until"] == (now + timedelta(hours=3)).isoformat()
    assert state["counts"]["failed"] == 1


def test_skipped_check_does_not_touch_last_check_time():
    state, cfg = started()
    before = state["last_check_at"]
    process(state, FetchResult("skipped", detail="backing off"), T0 + timedelta(hours=1), cfg)
    assert state["last_check_at"] == before


# --------------------------------------------------------------------------- daily digest


def test_digest_once_a_day_after_digest_hour():
    state, cfg = started()
    day2 = T0 + timedelta(days=1)  # Nov 6, 06:00
    assert process(state, ok(SEP1), day2 + timedelta(hours=2), cfg) == []  # 08:00: too early
    msgs = process(state, ok(AUG20), day2 + timedelta(hours=3, minutes=30), cfg)  # 09:30
    assert kinds(msgs) == ["sooner", "digest"]
    digest = msgs[1]
    assert digest.silent is True
    assert "Daily visa slot summary - Astana" in digest.text
    assert "Earliest now: Fri Aug 20, 2027" in digest.text
    assert "Since the last summary: 12 days sooner (was Sep 1, 2027)" in digest.text
    assert "Checks: 3 ok · 0 empty · 0 failed" in digest.text  # the baseline check counts too
    assert state["last_digest_date"] == "2026-11-06"
    assert state["last_digest_earliest"] == AUG20
    assert state["counts"] == {"ok": 0, "empty": 0, "failed": 0}
    assert state["best_since_digest"] is None
    assert process(state, ok(AUG20), day2 + timedelta(hours=5), cfg) == []  # no second digest


def test_digest_unchanged_with_best_seen_in_between():
    state, cfg = started()
    day2 = T0 + timedelta(days=1)
    process(state, ok(AUG1), day2 + timedelta(hours=1), cfg)  # 07:00 alert
    process(state, ok(SEP1), day2 + timedelta(hours=2), cfg)  # 08:00 gone
    msgs = process(state, ok(SEP1), day2 + timedelta(hours=3), cfg)  # 09:00 digest
    digest = msgs[-1]
    assert "Since the last summary: unchanged" in digest.text
    assert "Best seen in between: Aug 1, 2027 at Nov 6 07:00 (gone since)" in digest.text


def test_digest_later_date():
    state, cfg = started()
    msgs = process(state, ok("2027-09-11"), T0 + timedelta(days=1, hours=3), cfg)
    assert "Since the last summary: 10 days later (was Sep 1, 2027)" in msgs[-1].text


def test_digest_when_session_expired_uses_last_known_date():
    state, cfg = started()
    state["session_set_at"] = T0.isoformat()
    process(state, FetchResult("expired", detail="HTTP 302"), T0 + timedelta(hours=1), cfg)
    msgs = process(
        state, FetchResult("skipped", detail="session expired"), T0 + timedelta(days=1, hours=3), cfg
    )
    assert kinds(msgs) == ["digest"]
    assert "Last known earliest (as of Nov 5 06:00): Wed Sep 1, 2027" in msgs[0].text
    assert "Session: EXPIRED since Nov 5 07:00" in msgs[0].text


def test_digest_after_empty_answer_reports_backoff():
    state, cfg = started()
    msgs = process(state, empty(), T0 + timedelta(days=1, hours=3), cfg)  # 09:00 next day
    assert kinds(msgs) == ["digest"]
    assert "No dates offered at the last good check" in msgs[0].text
    assert "Since the last summary: no dates now (was Sep 1, 2027)" in msgs[0].text
    assert "pausing until Nov 6 12:00" in msgs[0].text


def test_digest_hour_respects_timezone():
    cfg = make_cfg(timezone="Asia/Almaty", digest_hour=9)  # UTC+5
    state, _ = started(cfg)
    # 04:00 UTC on Nov 6 = 09:00 in Almaty
    msgs = process(state, ok(SEP1), T0 + timedelta(hours=22), cfg)
    assert kinds(msgs) == ["digest"]


def test_rollback_restores_state_for_retry():
    state, cfg = started()
    msgs = process(state, ok(AUG1), T0 + timedelta(hours=1), cfg)
    state.update(msgs[0].rollback)
    assert state["last_seen"] == SEP1
    assert kinds(process(state, ok(AUG1), T0 + timedelta(hours=2), cfg)) == ["sooner"]
