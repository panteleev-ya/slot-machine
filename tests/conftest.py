from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from slot_machine import timeutil as clock_module  # everything reads utcnow() from here
from slot_machine.config import ENV_KEYS, Config

from fakes import BOT_TOKEN, CHAT_ID, FakeAIS, FakeTelegram, Server
from helpers import T0, make_cfg


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in [*ENV_KEYS, "VISA_WATCH_CONFIG"]:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> datetime:
        self.now += timedelta(**kw)
        return self.now


@pytest.fixture
def clock(monkeypatch) -> Clock:
    c = Clock(T0)
    monkeypatch.setattr(clock_module, "utcnow", c)
    return c


@pytest.fixture
def ais():
    with Server(FakeAIS()) as server:
        yield server


@pytest.fixture
def tg():
    with Server(FakeTelegram()) as server:
        yield server


@pytest.fixture
def cfg(tmp_path, ais, tg) -> Config:
    return make_cfg(tmp_path / "state", ais, tg)


@pytest.fixture
def env_file(tmp_path, ais, tg) -> Path:
    """A complete settings file pointing at the fakes."""
    path = tmp_path / "test.env"
    path.write_text(
        "\n".join(
            [
                "AIS_SCHEDULE_ID=1234",
                "AIS_FACILITY_ID=134",
                "LOCATION_LABEL=Astana",
                f"TELEGRAM_BOT_TOKEN={BOT_TOKEN}",
                f"TELEGRAM_CHAT_ID={CHAT_ID}",
                "TIMEZONE=UTC",
                f"STATE_DIR={tmp_path / 'state'}",
                f"AIS_BASE_URL={ais.url}",
                f"TELEGRAM_API_BASE={tg.url}",
            ]
        )
        + "\n"
    )
    path.chmod(0o600)
    return path
