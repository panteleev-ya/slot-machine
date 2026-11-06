"""The Mac sign-in probe decides pages and reads credentials without opening Chrome."""

from __future__ import annotations

import json
import subprocess

import pytest

import signin.mac as signin_mac


@pytest.fixture(scope="module")
def probe():
    return signin_mac


def test_sign_in_url_is_not_success(probe):
    url = "https://ais.usvisa-info.com/en-kz/niv/users/sign_in"
    assert probe.page_kind(url, False) == probe.KIND_SIGN_IN
    assert probe.page_kind(url, True) == probe.KIND_SIGN_IN


def test_schedule_redirect_on_the_sign_in_form_is_still_sign_in(probe):
    url = "https://ais.usvisa-info.com/en-kz/niv/users/sign_in?next=/niv/schedule/1/continue_actions"
    assert probe.page_kind(url, True) == probe.KIND_SIGN_IN


def test_continue_actions_is_the_appointment_page(probe):
    url = "https://ais.usvisa-info.com/en-kz/niv/schedule/99/continue_actions"
    assert probe.page_kind(url, False) == probe.KIND_APPOINTMENT


def test_account_page_is_signed_in_only(probe):
    assert probe.page_kind("https://ais.usvisa-info.com/en-kz/niv/account", False) == probe.KIND_SIGNED_IN


def test_sign_out_lands_on_the_locale_home(probe):
    home = {"url": "https://ais.usvisa-info.com/en-kz/niv", "hasSignInForm": False}
    assert probe._signed_out(home) is True
    assert probe._left_sign_in(home) is False
    assert probe._locale_home("https://ais.usvisa-info.com/en-kz/niv/") is True


def test_groups_page_waits_until_continue_is_drawn(probe):
    groups = {"url": "https://ais.usvisa-info.com/en-kz/niv/groups/1", "hasSignInForm": False}
    assert probe._left_sign_in(groups) is True
    assert probe._ready_after_submit(groups) is False
    assert probe._ready_after_submit({**groups, "hasContinue": True}) is True
    appointment = {
        "url": "https://ais.usvisa-info.com/en-kz/niv/schedule/99/continue_actions",
        "hasSignInForm": False,
    }
    assert probe._ready_after_submit(appointment) is True


def test_continue_control_is_the_continue_button_on_this_site(probe):
    assert "probeContinueControl" in probe.CONTINUE_JS
    assert "probeContinueControl" in probe.STATUS_JS
    assert "/^continue$/i" in probe.CONTINUE_JS
    assert "ais.usvisa-info.com" in probe.CONTINUE_JS
    assert "reschedule" in probe.CONTINUE_JS.lower()


def test_blank_document_has_not_left_sign_in(probe):
    assert probe._left_sign_in({"url": "", "hasSignInForm": False, "challenge": False}) is False
    assert probe._left_sign_in({"url": "about:blank", "hasSignInForm": False}) is False


def test_puzzle_stops_the_wait(probe):
    assert probe._left_sign_in(
        {"url": "https://ais.usvisa-info.com/en-kz/niv/users/sign_in", "challenge": True}
    )


def test_credentials_file_is_inside_the_repo(probe):
    assert probe.display_path(probe.CREDENTIALS) == "config/ais-login.env"
    assert probe.display_path(probe.SETTINGS) == "config/slot-machine.env"


def test_credentials_require_mode_600(probe, tmp_path):
    path = tmp_path / "ais-login.env"
    path.write_text("AIS_EMAIL=a@b.c\nAIS_PASSWORD=secret\n", encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(probe.ProbeError, match="chmod 600"):
        probe.load_credentials(path)
    path.chmod(0o600)
    assert probe.load_credentials(path) == ("a@b.c", "secret")


def test_init_creates_a_private_empty_file(probe, tmp_path):
    path = tmp_path / "slot-machine" / "ais-login.env"
    probe.init_credentials(path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert probe.parse_env(path.read_text(encoding="utf-8")) == {"AIS_EMAIL": "", "AIS_PASSWORD": ""}
    with pytest.raises(probe.ProbeError, match="already exists"):
        probe.init_credentials(path)


def test_fill_js_embeds_credentials_as_json(probe):
    email, password = 'a"b@c.com', "p\\ass'word"
    source = probe.build_fill_js(email, password)
    assert json.dumps(email) in source
    assert json.dumps(password) in source
    assert "continue_actions" not in source
    assert "reschedule" not in source.lower()


def test_refuses_urls_outside_the_visa_site(probe):
    with pytest.raises(probe.ProbeError, match="outside"):
        probe._applescript_url("https://example.com/")


def test_assistive_access_error_is_about_the_alert(probe):
    message = probe._chrome_error("osascript is not allowed assistive access.")
    assert "Accessibility" in message
    assert "alert" in message


def test_dialog_hook_is_installed_before_sign_in(probe):
    assert "probeArmDialogs" in probe.SUBMIT_JS
    assert "probeClickPageOk" in probe.STATUS_JS


def test_quoted_window_id_round_trips(probe):
    assert probe.applescript_string('win"1') == '"win\\"1"'


def test_step_delay_stays_near_one_to_three_seconds(probe):
    samples = iter([1.0, -0.3, 3.0, 0.3, 2.0, 0.0])

    def draw(low, high):
        return next(samples)

    assert probe.step_delay(draw) == pytest.approx(0.7)
    assert probe.step_delay(draw) == pytest.approx(3.3)
    assert probe.step_delay(draw) == pytest.approx(2.0)


def test_waits_announce_themselves(probe, monkeypatch, capsys):
    monkeypatch.setattr(probe.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(probe, "step_delay", lambda rng=None: 1.4)
    probe.pause_step("before clicking Sign In")

    def ready(_window_id):
        return {"url": probe.SIGN_IN_URL, "hasSignInForm": True}

    monkeypatch.setattr(probe, "dismiss_native_ok", lambda: "")
    monkeypatch.setattr(probe, "read_status", ready)
    probe.wait_until("1", lambda page: page.get("hasSignInForm"), 30, what="for the sign-in form")
    monkeypatch.setattr(probe, "cookie_or_none", lambda: "x" * 24)
    assert probe.wait_for_signed_in_cookie("anon-cookie") == "x" * 24
    text = capsys.readouterr().out
    assert "Waiting 1.4s before clicking Sign In." in text
    assert "Waiting up to 30s for the sign-in form." in text
    assert "Waiting up to 30s for Chrome to store the signed-in session." in text
    assert "reading that session once more" in text


def test_cycle_seconds_is_exact_without_jitter(probe):
    assert probe.cycle_seconds(12, 0) == 720


def test_cycle_seconds_applies_jitter(probe):
    assert probe.cycle_seconds(50, 3, rng=lambda _low, _high: -3) == 47 * 60
    assert probe.cycle_seconds(50, 3, rng=lambda _low, _high: 3) == 53 * 60


def test_cycle_seconds_rejects_a_jitter_as_large_as_the_interval(probe):
    with pytest.raises(probe.ProbeError, match="jitter"):
        probe.cycle_seconds(12, 12)


def test_renew_settings_need_a_private_file(probe, tmp_path):
    path = tmp_path / "slot-machine.env"
    path.write_text("RENEW_URL=http://yarikos.local:8080\nRENEW_TOKEN=sekret\n", encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(probe.ProbeError, match="chmod 600"):
        probe.load_renew_settings(path)
    path.chmod(0o600)
    assert probe.load_renew_settings(path) == ("http://yarikos.local:8080", "sekret")


def test_session_body_is_the_extension_payload(probe):
    body = json.loads(probe.session_body("a" * 24, "Mozilla/5.0 Chrome"))
    assert body == {"cookie": "a" * 24, "user_agent": "Mozilla/5.0 Chrome"}
    with pytest.raises(probe.ProbeError, match="cookie"):
        probe.session_body("short", "Mozilla/5.0 Chrome")


def test_chrome_cookie_round_trip(probe):
    key = probe.chrome_key_from_password("peanuts")
    secret = b"session-value-0123456789"
    encrypted = subprocess_encrypt(secret, key)
    assert probe.decrypt_chrome_cookie(b"v10" + encrypted, key) == secret.decode()
    prefixed = subprocess_encrypt(b"\x01" * 32 + secret, key)
    assert probe.decrypt_chrome_cookie(b"v10" + prefixed, key) == secret.decode()


def subprocess_encrypt(plain: bytes, key: bytes) -> bytes:
    proc = subprocess.run(
        ["openssl", "enc", "-aes-128-cbc", "-K", key.hex(), "-iv", (b" " * 16).hex()],
        input=plain,
        capture_output=True,
        check=True,
    )
    return proc.stdout


def test_temp_js_file_is_private_and_removed_by_callers(probe, monkeypatch, tmp_path):
    monkeypatch.setattr(probe, "CREDENTIALS", tmp_path / "ais-login.env")
    path = probe._js_file("1+1")
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_text(encoding="utf-8") == "1+1"
    path.unlink()
