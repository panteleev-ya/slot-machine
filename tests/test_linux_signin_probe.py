"""The Linux probe stays headed and off the visa site until a real run asks for it."""

from __future__ import annotations

import pytest

import signin.linux as signin_linux


@pytest.fixture(scope="module")
def linux():
    return signin_linux


def test_chrome_launch_is_a_normal_window(linux, monkeypatch, tmp_path):
    monkeypatch.delenv("CHROME_NO_SANDBOX", raising=False)
    command = linux.chrome_command(tmp_path, 9222)
    assert command[-1] == "about:blank"
    assert "--enable-automation" not in command
    assert "--remote-debugging-address=127.0.0.1" in command
    assert "--no-sandbox" not in command
    assert not any(part == "--headless" or part.startswith("--headless=") for part in command)


def test_sandbox_off_is_an_explicit_flag(linux, monkeypatch, tmp_path):
    monkeypatch.setenv("CHROME_NO_SANDBOX", "1")
    assert "--no-sandbox" in linux.chrome_command(tmp_path, 9222)


def test_browser_problem_rejects_automation_and_headless(linux):
    agent = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
    assert linux.browser_problem({"webdriver": False, "headless": False, "ua": agent}) is None
    assert "webdriver" in linux.browser_problem({"webdriver": True, "ua": agent})
    assert "headless" in linux.browser_problem(
        {"webdriver": False, "headless": True, "ua": "HeadlessChrome/131"}
    )


def test_only_the_visa_site_or_a_blank_tab_is_allowed(linux):
    assert linux.allowed_page("about:blank") is True
    assert linux.allowed_page("https://ais.usvisa-info.com/en-kz/niv/users/sign_in") is True
    assert linux.allowed_page("https://ais.usvisa-info.com.evil/sign_in") is False
    assert linux.allowed_page("http://ais.usvisa-info.com/en-kz/niv") is False
    assert linux.allowed_page("https://example.com/") is False


def test_navigate_does_not_open_other_sites(linux):
    class Page:
        def call(self, *_args, **_kwargs):
            raise AssertionError("navigated")

    with pytest.raises(linux.mac.ProbeError, match="outside"):
        linux.navigate(Page(), "https://example.com/")


def test_session_cookie_is_the_live_yatri_value(linux):
    class Page:
        def call(self, _method, _params=None, timeout=None):
            return {
                "cookies": [
                    {"name": "other", "value": "x" * 30},
                    {"name": "_yatri_session", "value": "short"},
                    {"name": "_yatri_session", "value": "a" * 24},
                ]
            }

    assert linux.session_cookie(Page()) == "a" * 24


def test_check_is_opt_in(linux):
    assert linux.parse_args(["--check"]).check is True
    assert linux.parse_args(["--once"]).check is False
    assert linux.parse_args(["--once"]).once is True
