from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from visa_watch import Config, FetchResult

from fakes import BOT_TOKEN, CHAT_ID

T0 = datetime(2026, 11, 5, 6, 0, tzinfo=UTC)  # a Thursday, before the 09:00 digest


def make_cfg(state_dir: Path | None = None, ais=None, tg=None, **overrides) -> Config:
    cfg = Config(
        schedule_id="1234",
        facility_id="134",
        label="Astana",
        timezone="UTC",
        bot_token=BOT_TOKEN,
        chat_id=str(CHAT_ID),
    )
    if state_dir is not None:
        cfg.state_dir = state_dir
    if ais is not None:
        cfg.ais_base = ais.url
    if tg is not None:
        cfg.telegram_api = tg.url
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def ok(*dates: str) -> FetchResult:
    return FetchResult("ok", dates=sorted(dates), cookies={})


def empty() -> FetchResult:
    return FetchResult("empty", detail="no dates in reply", cookies={})


def kinds(msgs) -> list[str]:
    return [m.kind for m in msgs]
