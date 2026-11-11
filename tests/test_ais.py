"""Talking to the visa site: reply classification, cookies, page parsing."""

import json
from email.message import Message

import pytest

from slot_machine.ais import (
    classify,
    fetch_dates,
    merge_set_cookies,
    parse_cookie_input,
    parse_facility_options,
)

from fakes import APPOINTMENT_PAGE, SIGN_IN_PAGE, VALID_SESSION

JAR = {"_yatri_session": VALID_SESSION}


def headers(**kw) -> Message:
    m = Message()
    for name, value in kw.items():
        values = value if isinstance(value, list) else [value]
        for v in values:
            m[name.replace("_", "-")] = v
    return m


def days(*dates) -> bytes:
    return json.dumps([{"date": d, "business_day": True} for d in dates]).encode()


# --------------------------------------------------------------------------- classify


def test_dates_are_sorted_and_deduplicated():
    res = classify(200, headers(), days("2027-09-01", "2027-08-01", "2027-09-01"), JAR)
    assert res.kind == "ok"
    assert res.dates == ["2027-08-01", "2027-09-01"]
    assert res.cookies == JAR


def test_empty_list_is_empty_not_error():
    res = classify(200, headers(), b"[]", JAR)
    assert res.kind == "empty"
    assert res.cookies == JAR


def test_list_without_dates_means_site_changed():
    res = classify(200, headers(), b'[{"day": "tomorrow"}]', JAR)
    assert res.kind == "error"
    assert "did the site change" in res.detail


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_to_sign_in_is_expired(status):
    res = classify(status, headers(Location="https://x/en-kz/niv/users/sign_in"), b"", JAR)
    assert res.kind == "expired"


def test_redirect_elsewhere_is_error():
    res = classify(302, headers(Location="https://x/maintenance"), b"", JAR)
    assert res.kind == "error"
    assert "maintenance" in res.detail


def test_401_is_expired():
    assert classify(401, headers(), b"", JAR).kind == "expired"


@pytest.mark.parametrize("status", [403, 429])
def test_403_and_429_are_throttling(status):
    assert classify(status, headers(), b"", JAR).kind == "throttled"


def test_server_error_is_error():
    res = classify(500, headers(), b"", JAR)
    assert (res.kind, res.detail) == ("error", "HTTP 500")


@pytest.mark.parametrize("status", [502, 503, 504])
def test_gateway_errors_mean_the_site_is_down(status):
    res = classify(status, headers(), b"<html>502 Bad Gateway</html>", JAR)
    assert (res.kind, res.detail) == ("down", f"HTTP {status}")


def test_sign_in_page_instead_of_json_is_expired():
    assert classify(200, headers(), SIGN_IN_PAGE.encode(), JAR).kind == "expired"


def test_other_html_is_error():
    res = classify(200, headers(), b"<html>Service unavailable</html>", JAR)
    assert res.kind == "error"
    assert "non-JSON" in res.detail


def test_json_error_asking_to_sign_in_is_expired():
    body = b'{"error": "You need to sign in or sign up before continuing."}'
    assert classify(200, headers(), body, JAR).kind == "expired"


def test_other_json_object_is_error():
    assert classify(200, headers(), b'{"error": "boom"}', JAR).kind == "error"


def test_no_cookies_returned_on_failure():
    assert classify(500, headers(), b"", JAR).cookies is None


# --------------------------------------------------------------------------- cookies


def test_set_cookie_updates_session():
    h = headers(Set_Cookie=["_yatri_session=NEW; path=/; secure; HttpOnly", "other=1; path=/"])
    assert merge_set_cookies(JAR, h) == {"_yatri_session": "NEW", "other": "1"}


@pytest.mark.parametrize(
    "line",
    [
        "_yatri_session=; path=/",
        "_yatri_session=deleted; path=/",
        "_yatri_session=x; Max-Age=0",
        "_yatri_session=x; expires=Thu, 01 Jan 1970 00:00:00 GMT",
    ],
)
def test_set_cookie_deletion(line):
    assert merge_set_cookies(JAR, headers(Set_Cookie=line)) == {}


def test_merge_keeps_input_unchanged_and_tolerates_none():
    jar = dict(JAR)
    merge_set_cookies(jar, headers(Set_Cookie="_yatri_session=NEW"))
    assert jar == JAR
    assert merge_set_cookies(JAR, None) == JAR


@pytest.mark.parametrize(
    "raw",
    [
        VALID_SESSION,
        f"  '{VALID_SESSION}'\n",
        f"_yatri_session={VALID_SESSION}",
        f"Cookie: foo=bar; _yatri_session={VALID_SESSION}",
    ],
)
def test_cookie_input_formats(raw):
    assert parse_cookie_input(raw)["_yatri_session"] == VALID_SESSION


def test_cookie_header_keeps_other_cookies():
    assert parse_cookie_input(f"a=1; _yatri_session={VALID_SESSION}") == {
        "a": "1",
        "_yatri_session": VALID_SESSION,
    }


@pytest.mark.parametrize("raw", ["", "short", "has whitespace inside the value ok"])
def test_bad_cookie_input_rejected(raw):
    with pytest.raises(ValueError):
        parse_cookie_input(raw)


# --------------------------------------------------------------------------- pages


def test_parse_facility_options():
    found = parse_facility_options(APPOINTMENT_PAGE.format(facility_id="134"))
    assert found == [
        ("appointments_consulate_appointment_facility_id", [("134", "Astana"), ("135", "Almaty & Co")])
    ]


def test_parse_facility_options_none():
    assert parse_facility_options("<html></html>") == []


# --------------------------------------------------------------------------- against the fake site


def test_fetch_sends_browser_like_ajax_request(cfg, ais):
    ais.fake.dates = ["2027-09-01"]
    res = fetch_dates(cfg, JAR)
    assert (res.kind, res.dates) == ("ok", ["2027-09-01"])
    method, path, hdrs = ais.fake.requests[-1]
    assert path == "/en-kz/niv/schedule/1234/appointment/days/134.json"
    # the request that worked from curl on 2026-11-05, minus the version-specific client hints
    expected = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": f"{cfg.appointment_url}?confirmed_limit_message=1&amp;commit=Continue",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "User-Agent": cfg.user_agent,
        "X-Requested-With": "XMLHttpRequest",
        "Cookie": f"_yatri_session={VALID_SESSION}",
    }
    assert {k: hdrs.get(k) for k in expected} == expected
    assert not [k for k in hdrs if k.lower().startswith("sec-ch-ua")]
    assert "If-None-Match" not in hdrs  # a conditional request would get 304 and no dates


def test_fetch_sees_expiry_redirect(cfg, ais):
    ais.fake.sessions.clear()
    assert fetch_dates(cfg, JAR).kind == "expired"


def test_fetch_picks_up_rotated_cookie(cfg, ais):
    ais.fake.dates = ["2027-09-01"]
    ais.fake.rotate_cookie = "r" * 40
    assert fetch_dates(cfg, JAR).cookies == {"_yatri_session": "r" * 40}


def test_fetch_network_failure_means_the_site_is_down(cfg):
    cfg.ais_base = "http://127.0.0.1:9"  # nothing listens on the discard port
    res = fetch_dates(cfg, JAR)
    assert res.kind == "down"
    assert res.detail.startswith("network:")
