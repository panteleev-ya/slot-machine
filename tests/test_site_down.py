"""The visa site not answering. A failed check (or sign-in) starts pings of the home page;
pings without an answer for a few minutes mean it's down, and then only pings go out until one
is answered. The Pi tells the owner both ways; the sign-in loop follows the same rules."""

import threading
from datetime import timedelta

import pytest

import signin.linux as signin_linux
import signin.mac as signin_mac
from slot_machine import bot, renew, sitewatch
from slot_machine.ais import FetchResult
from slot_machine.runner import run_check, run_ping
from slot_machine.watcher import process, process_ping

from fakes import VALID_SESSION
from helpers import T0, kinds, make_cfg, ok

JAR = {"_yatri_session": VALID_SESSION}
OWNER, FRIEND = "42", "77"
SEP1 = "2027-09-01"
TOKEN = "T" * 43
TIMEOUT = FetchResult("down", detail="network: URLError: <urlopen error timed out>")
NO_ANSWER = "network: URLError: <urlopen error [Errno 113] No route to host>"
DEAD = "http://127.0.0.1:9"  # nothing listens on the discard port


def at(minutes: float):
    return T0 + timedelta(minutes=minutes)  # T0 is 06:00 UTC; the digest is due at 09:00


# --------------------------------------------------------------------------- the state machine


def test_a_failure_starts_pings_right_away():
    site = sitewatch.failed(None, "HTTP 503", at(0))
    assert site == {
        "state": "checking",
        "since": at(0).isoformat(),
        "pings": 0,
        "next_ping": at(0).isoformat(),
        "last_error": "HTTP 503",
    }
    assert sitewatch.ping_due(site, at(0))
    assert not sitewatch.ping_due(None, at(0))


def test_three_unanswered_pings_mean_down_and_one_answer_means_up():
    site = sitewatch.failed(None, "HTTP 503", at(0))
    for n, minute in enumerate((0, 3), start=1):
        site = sitewatch.pinged(site, False, "HTTP 503", at(minute))
        assert (site["state"], site["pings"]) == ("checking", n)
    assert site["next_ping"] == at(6).isoformat()
    assert not sitewatch.ping_due(site, at(5))
    site = sitewatch.pinged(site, False, "HTTP 503", at(6))
    assert (site["state"], site["down_at"], site["next_ping"]) == (
        "down",
        at(6).isoformat(),
        at(9).isoformat(),
    )
    site = sitewatch.pinged(site, False, "HTTP 503", at(9))
    assert site["state"] == "down"
    assert sitewatch.pinged(site, True, "HTTP 200", at(12)) is None


@pytest.mark.parametrize(
    ("status", "answered"), [(200, True), (302, True), (403, True), (500, True), (502, False), (503, False)]
)
def test_any_answer_but_a_gateway_error_counts(ais, status, answered):
    ais.fake.home_status = status
    url = sitewatch.home_url(ais.url, "en-kz")
    assert sitewatch.ping(url, "Mozilla/5.0 test") == (answered, f"HTTP {status}")
    _, path, headers = ais.fake.requests[-1]
    assert path == "/en-kz/niv" and "Cookie" not in headers


def test_no_connection_is_no_answer():
    answered, detail = sitewatch.ping(sitewatch.home_url(DEAD, "en-kz"), "Mozilla/5.0 test")
    assert not answered and detail.startswith("network: URLError:")


# --------------------------------------------------------------------------- the Pi's decisions


def started():
    cfg = make_cfg(chat_id=f"{OWNER},{FRIEND}")
    state = {}
    assert kinds(process(state, ok(SEP1), T0, cfg)) == ["baseline"]
    return state, cfg


def went_down(state, cfg):
    """A check at 07:00 gets no answer, and neither do pings at 07:00, 07:03 and 07:06."""
    assert process(state, TIMEOUT, at(60), cfg) == []
    msgs = []
    for minute in (60, 63, 66):
        msgs += process_ping(state, False, NO_ANSWER, at(minute), cfg)
    assert kinds(msgs) == ["down"]
    return msgs[0]


def test_a_failed_check_only_starts_pings():
    state, cfg = started()
    assert process(state, TIMEOUT, at(60), cfg) == []
    assert state["site"]["state"] == "checking"
    assert state["counts"]["failed"] == 1
    assert not state.get("consecutive_errors")  # that's for "checks failing"


def test_down_tells_the_owner_once():
    state, cfg = started()
    down = went_down(state, cfg)
    assert down.to == [OWNER] and down.silent is False
    assert down.text.split("\n") == [
        "<b>🔴 Visa site down - Astana</b>",
        "No answer since Nov 5 07:00: a check failed, then 3 pings of the home page over 6m got none.",
        "Last error: network: URLError: &lt;urlopen error [Errno 113] No route to host&gt;",
        "Date checks are paused. The Pi pings the home page every 3m and tells you when it answers; "
        "the sign-in loop does the same and doesn't sign in meanwhile.",
    ]
    for minute in range(69, 120, 3):
        assert process_ping(state, False, NO_ANSWER, at(minute), cfg) == []


def test_back_up_tells_the_owner():
    state, cfg = started()
    went_down(state, cfg)
    (up,) = process_ping(state, True, "HTTP 200", at(150), cfg)
    assert up.kind == "up" and up.to == [OWNER]
    assert up.text.split("\n") == [
        "<b>✅ Visa site back up - Astana</b>",
        "It didn't answer for 1h 30m (since Nov 5 07:00).",
        "Date checks resume on schedule.",
    ]
    assert "site" not in state


def test_a_blip_is_quiet():
    state, cfg = started()
    process(state, TIMEOUT, at(60), cfg)
    assert process_ping(state, False, NO_ANSWER, at(60), cfg) == []
    assert process_ping(state, True, "HTTP 200", at(63), cfg) == []
    assert "site" not in state


def test_a_check_that_gets_an_answer_also_ends_it():
    state, cfg = started()
    went_down(state, cfg)
    assert kinds(process(state, ok("2027-08-01"), at(90), cfg)) == ["up", "sooner"]  # e.g. --force


def test_a_forced_check_that_fails_while_down_changes_nothing_else():
    state, cfg = started()
    went_down(state, cfg)
    before = dict(state["site"])
    assert process(state, FetchResult("down", detail="HTTP 502"), at(70), cfg) == []
    assert state["site"] == {**before, "last_error": "HTTP 502"}


def test_back_up_message_that_reached_nobody_goes_with_the_next_answer():
    state, cfg = started()
    went_down(state, cfg)
    (up,) = process_ping(state, True, "HTTP 200", at(90), cfg)
    state.update(up.rollback)  # what the runner does when a message reached nobody
    assert "site" not in state  # checks go on meanwhile
    assert kinds(process(state, ok(SEP1), at(120), cfg)) == ["up"]
    assert process(state, ok(SEP1), at(140), cfg) == []


def test_down_message_that_reached_nobody_goes_with_the_next_ping():
    state, cfg = started()
    down = went_down(state, cfg)
    state.update(down.rollback)
    assert kinds(process_ping(state, False, NO_ANSWER, at(69), cfg)) == ["down"]


def test_digest_says_the_site_is_down():
    state, cfg = started()
    went_down(state, cfg)
    msgs = process(state, FetchResult("skipped", detail="pinging"), at(24 * 60 + 210), cfg)  # day 2, 09:30
    assert kinds(msgs) == ["digest"]
    assert "Visa site: down since Nov 5 07:00" in msgs[0].text


# --------------------------------------------------------------------------- the Pi


def sent(tg) -> list[tuple[str, str]]:
    return [(m["chat_id"], m["text"].split("\n")[0]) for m in tg.fake.messages]


@pytest.fixture
def live(cfg, ais, tg, clock):
    """Two chats, a working session, baseline sent at 06:00; no minimum interval between checks."""
    cfg.chat_id = f"{OWNER},{FRIEND}"
    cfg.min_interval_minutes = 0
    cfg.renew_url, cfg.renew_token = "http://yarikos.local:8080", TOKEN
    ais.fake.dates = [SEP1]
    run_check(cfg, force=True, new_cookies=JAR)
    tg.fake.messages.clear()
    return cfg


def minute_by_minute(cfg, clock, minutes: int) -> None:
    """What cron's every-minute `slot-machine --ping` line does."""
    for _ in range(minutes):
        clock.advance(minutes=1)
        run_ping(cfg)


def test_ping_line_does_nothing_while_the_site_is_up(live, ais, capsys):
    capsys.readouterr()
    assert run_ping(live) == 0
    assert ais.fake.home_requests() == 0 and capsys.readouterr().out == ""


def test_outage_on_the_pi(live, ais, tg, clock, capsys):
    url, live.ais_base = live.ais_base, DEAD
    clock.advance(minutes=60)  # 07:00
    assert run_check(live)[1].kind == "down"
    assert run_ping(live) == 0  # 07:00: ping 1
    minute_by_minute(live, clock, 6)  # pings 2 and 3 at 07:03 and 07:06: down
    assert sent(tg) == [(OWNER, "<b>🔴 Visa site down - Astana</b>")]

    clock.advance(minutes=4)  # 07:10: the hourly check pings instead (one was due at 07:09)
    code, res = run_check(live)
    assert (code, res.kind, res.detail) == (0, "skipped", "visa site down; pinging instead")

    live.ais_base = url  # back; the next ping is at 07:13
    minute_by_minute(live, clock, 2)
    assert ais.fake.home_requests() == 0
    minute_by_minute(live, clock, 1)
    assert ais.fake.home_requests() == 1
    assert sent(tg)[-1] == (OWNER, "<b>✅ Visa site back up - Astana</b>")
    assert ais.fake.days_requests() == 1  # no date check while it was down: still only the baseline
    minute_by_minute(live, clock, 10)
    assert ais.fake.home_requests() == 1  # up again: no more pings

    log = capsys.readouterr().out
    assert "ping 1/3: network: URLError:" in log and "ping 2/3: network: URLError:" in log
    assert "the visa site is down, pinging every 3m until it answers [cron]" in log
    assert "ping: HTTP 200; the visa site answers, checks resume [cron]" in log
    pings = [line for line in log.splitlines() if line[20:].startswith("ping")]
    assert len(pings) == 4  # nothing for the ping at 07:10, while it stayed down
    assert run_check(live)[1].kind == "ok"


def test_a_blip_on_the_pi(live, ais, tg, clock):
    ais.fake.status = 503  # the dates request fails; the home page answers
    clock.advance(minutes=60)
    assert run_check(live)[1].kind == "down"
    ais.fake.status = 200
    run_ping(live)
    assert ais.fake.home_requests() == 1 and tg.fake.messages == []
    assert run_check(live)[1].kind == "ok"


def test_force_still_checks(live, clock):
    live.ais_base = DEAD
    clock.advance(minutes=60)
    run_check(live)
    clock.advance(minutes=1)
    assert run_check(live, force=True)[1].kind == "down"


def test_check_command_sends_nothing_while_the_site_looks_down(live, ais, tg, clock):
    live.ais_base = DEAD
    clock.advance(minutes=60)
    run_check(live)
    clock.advance(minutes=1)
    tg.fake.say(FRIEND, "/check")
    bot.poll(live, 0)
    (reply,) = [m["text"] for m in tg.fake.messages if m["chat_id"] == FRIEND]
    assert reply.startswith("The visa site hasn't answered since Nov 5 07:00 (network: URLError: &lt;urlopen")
    assert reply.endswith(
        "so nothing was checked. The Pi pings its home page every 3m and checks again once it answers."
    )
    assert ais.fake.days_requests() == 1


# --------------------------------------------------------------------------- the sign-in loop


class Loop:
    """Drives signin.mac.renew_loop with a scripted `cycle` and a fake clock: every wait
    just moves the clock, and `on_wait` can change the fake site meanwhile."""

    def __init__(self, ais, clock, monkeypatch, outcomes):
        self.ais, self.clock, self.outcomes = ais, clock, list(outcomes)
        self.fresh, self.waits = [], []
        self.on_wait = lambda: None
        monkeypatch.setattr(signin_mac, "_now", clock)
        monkeypatch.setattr(signin_mac, "_wait", self.wait)
        monkeypatch.setattr(signin_mac, "pi_status", lambda url, token: {"ok": True})

    def wait(self, seconds):
        self.waits.append(seconds)
        self.clock.advance(seconds=seconds)
        self.on_wait()

    def cycle(self, fresh):
        self.fresh.append(fresh)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def run(self, **kw):
        return signin_mac.renew_loop(
            self.cycle,
            every=50,
            jitter=0,
            renew_url="http://pi:8080",
            token=TOKEN,
            user_agent=lambda: "Mozilla/5.0 Chrome/142",
            home=sitewatch.home_url(self.ais.url, "en-kz"),
            **kw,
        )


GONE = signin_mac.SiteUnreachable("Chrome did not open the page (net::ERR_CONNECTION_TIMED_OUT)")


def test_sign_in_loop_pings_through_an_outage(ais, clock, monkeypatch, capsys):
    loop = Loop(ais, clock, monkeypatch, [0, GONE, 1])
    ais.fake.home_status = 503
    start = []

    def on_wait():
        if loop.fresh == [False, True] and not start:
            start.append(clock.now)  # the failed cycle; pings start right after it
        if start and clock.now - start[0] >= timedelta(minutes=12):
            ais.fake.home_status = 200

    loop.on_wait = on_wait
    assert loop.run() == 1  # the third cycle stops the loop
    assert loop.fresh == [False, True, True]
    assert ais.fake.home_requests() == 5  # +0, +3, +6 (down), +9, +12 (back)
    assert ais.fake.days_requests() == 0
    assert loop.waits == [50 * 60, 0, 180, 180, 180, 180]  # no sign-in pause after the outage
    out = capsys.readouterr().out
    assert "error: Chrome did not open the page (net::ERR_CONNECTION_TIMED_OUT)" in out
    assert "ping 1/3: HTTP 503" in out and "ping 2/3: HTTP 503" in out
    assert "ping: HTTP 503. The visa site is down. Pinging its home page every 3m" in out
    assert "ping: HTTP 200. The visa site answers again after 12m. Signing in now." in out
    assert len([line for line in out.splitlines() if line.startswith("ping")]) == 4  # +9 said nothing


def test_sign_in_loop_after_a_blip_waits_like_any_failed_cycle(ais, clock, monkeypatch, capsys):
    loop = Loop(ais, clock, monkeypatch, [GONE, 1])
    assert loop.run() == 1
    assert ais.fake.home_requests() == 1
    assert loop.waits == [0, 120]
    assert "The home page answers, so the site isn't down." in capsys.readouterr().out


def test_other_failures_wait_longer_each_time_without_pings(ais, clock, monkeypatch):
    loop = Loop(ais, clock, monkeypatch, [-1, signin_mac.ProbeError("no Continue link"), -1, 0, -1, 1])
    assert loop.run() == 1
    assert loop.waits == [120, 240, 480, 50 * 60, 120]
    assert ais.fake.home_requests() == 0


def test_once_does_not_ping(ais, clock, monkeypatch):
    loop = Loop(ais, clock, monkeypatch, [GONE])
    assert loop.run(once=True) == 1
    assert ais.fake.home_requests() == 0


def test_no_sign_in_while_the_pi_does_not_answer(ais, clock, monkeypatch, capsys):
    loop = Loop(ais, clock, monkeypatch, [1])
    answers = [signin_mac.PiUnreachable("Can't reach the Pi at http://pi:8080."), {"ok": True}]

    def status(_url, _token):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(signin_mac, "pi_status", status)
    assert loop.run() == 1
    assert loop.waits == [signin_mac.PI_POLL_MINUTES * 60]
    out = capsys.readouterr().out
    assert (
        "Can't reach the Pi at http://pi:8080. Not signing in: the session would have nowhere to go." in out
    )
    assert "The Pi answers. Going on." in out


@pytest.fixture
def pi(live):
    """The real renewal page, which the sign-in loop asks before every sign-in."""
    httpd = renew.make_server(live, 0, host="127.0.0.1")
    httpd.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def test_pi_status_tells_a_wrong_token_from_an_unreachable_pi(pi):
    assert signin_mac.pi_status(pi.url, TOKEN)["ok"] is True
    with pytest.raises(signin_mac.ProbeError, match="refused RENEW_TOKEN") as refused:
        signin_mac.pi_status(pi.url, "W" * 43)
    assert not isinstance(refused.value, signin_mac.PiUnreachable)  # stops the loop instead of waiting
    with pytest.raises(signin_mac.PiUnreachable):
        signin_mac.pi_status(DEAD, TOKEN)


def test_what_counts_as_a_page_that_did_not_load():
    error_page = {"url": "chrome-error://chromewebdata/", "netError": "ERR_CONNECTION_TIMED_OUT"}
    assert signin_mac.page_problem(error_page) == "Chrome couldn't load it: ERR_CONNECTION_TIMED_OUT"
    assert (
        signin_mac.page_problem({"url": "https://x/en-kz/niv/users/sign_in", "httpStatus": 503}) == "HTTP 503"
    )
    assert signin_mac.page_problem({"url": "https://x/en-kz/niv/users/sign_in", "httpStatus": 200}) == ""
    err = signin_mac.not_loaded("the sign-in form did not appear", {"httpStatus": 502})
    assert (
        isinstance(err, signin_mac.SiteUnreachable)
        and str(err) == "the sign-in form did not appear (HTTP 502)"
    )


@pytest.mark.parametrize(
    "reply",
    [
        {"errorText": "net::ERR_ADDRESS_UNREACHABLE"},
        signin_linux.cdp_client.CDPError("Chrome did not answer Page.navigate"),
    ],
)
def test_linux_navigation_that_cannot_reach_the_site(reply):
    class Page:
        def call(self, *_args, **_kwargs):
            if isinstance(reply, Exception):
                raise reply
            return reply

    with pytest.raises(signin_mac.SiteUnreachable):
        signin_linux.navigate(Page(), signin_mac.SIGN_IN_URL)


def test_failed_cycles_wait_longer_each_time():
    minutes = [signin_mac.retry_seconds(n) / 60 for n in range(1, 8)]
    assert minutes == [2, 4, 8, 16, 32, 60, 60]
