"""Settings: defaults, the KEY=value settings file, environment overrides, validation."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .timeutil import DATE_RE, log

APP = "slot-machine"
CONFIG_ENV_VAR = "SLOT_MACHINE_CONFIG"
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var, "")
    return Path(value) if os.path.isabs(value) else Path.home() / fallback


def default_config_path() -> Path:
    """$SLOT_MACHINE_CONFIG, else ~/.config/slot-machine/slot-machine.env (XDG)."""
    if os.environ.get(CONFIG_ENV_VAR):
        return Path(os.environ[CONFIG_ENV_VAR])
    return _xdg("XDG_CONFIG_HOME", ".config") / APP / f"{APP}.env"


def default_state_dir() -> Path:
    """~/.local/state/slot-machine (XDG); STATE_DIR in the settings overrides it."""
    return _xdg("XDG_STATE_HOME", ".local/state") / APP


@dataclass
class Config:
    locale: str = "en-kz"
    schedule_id: str = ""
    facility_id: str = ""
    label: str = "Astana"
    bot_token: str = ""
    chat_id: str = ""
    digest_hour: int = 9
    timezone: str = "America/Los_Angeles"
    digest_silent: bool = True
    alert_only_before: str = ""
    min_interval_minutes: float = 30
    backoff_hours: float = 3
    error_alert_after: int = 3
    user_agent: str = DEFAULT_UA
    state_dir: Path = field(default_factory=default_state_dir)
    ais_base: str = "https://ais.usvisa-info.com"
    telegram_api: str = "https://api.telegram.org"

    @property
    def appointment_url(self) -> str:
        return f"{self.ais_base}/{self.locale}/niv/schedule/{self.schedule_id}/appointment"

    @property
    def sign_in_url(self) -> str:
        return f"{self.ais_base}/{self.locale}/niv/users/sign_in"


ENV_KEYS = {
    "AIS_LOCALE": ("locale", str),
    "AIS_SCHEDULE_ID": ("schedule_id", str),
    "AIS_FACILITY_ID": ("facility_id", str),
    "LOCATION_LABEL": ("label", str),
    "TELEGRAM_BOT_TOKEN": ("bot_token", str),
    "TELEGRAM_CHAT_ID": ("chat_id", str),
    "DIGEST_HOUR": ("digest_hour", int),
    "TIMEZONE": ("timezone", str),
    "DIGEST_SILENT": ("digest_silent", "bool"),
    "ALERT_ONLY_BEFORE": ("alert_only_before", str),
    "MIN_INTERVAL_MINUTES": ("min_interval_minutes", float),
    "BACKOFF_HOURS": ("backoff_hours", float),
    "ERROR_ALERT_AFTER": ("error_alert_after", int),
    "USER_AGENT": ("user_agent", str),
    "STATE_DIR": ("state_dir", Path),
    "AIS_BASE_URL": ("ais_base", str),  # for testing against a fake server
    "TELEGRAM_API_BASE": ("telegram_api", str),  # for testing against a fake server
}


def read_env_file(path: Path) -> dict:
    values = {}
    if not path.exists():
        return values
    if os.name == "posix" and path.stat().st_mode & 0o077:
        log(f"warning: {path} is readable by other users; run: chmod 600 {path}")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        values[key.strip()] = val
    return values


def load_config(path: Path) -> Config:
    values = read_env_file(path)
    values.update({k: v for k, v in os.environ.items() if k in ENV_KEYS})
    cfg = Config()
    for key, (attr, kind) in ENV_KEYS.items():
        val = values.get(key, "").strip()
        if not val:
            continue
        if kind == "bool":
            setattr(cfg, attr, val.lower() in ("1", "true", "yes", "on"))
            continue
        try:
            setattr(cfg, attr, kind(val))
        except ValueError:
            raise SystemExit(f"Config error: {key}={val!r} is not valid") from None
    cfg.ais_base = cfg.ais_base.rstrip("/")
    cfg.telegram_api = cfg.telegram_api.rstrip("/")
    if not 0 <= cfg.digest_hour <= 23:
        raise SystemExit("Config error: DIGEST_HOUR must be 0-23")
    if cfg.alert_only_before and not DATE_RE.match(cfg.alert_only_before):
        raise SystemExit("Config error: ALERT_ONLY_BEFORE must look like 2027-04-30")
    for name, value in (("AIS_SCHEDULE_ID", cfg.schedule_id), ("AIS_FACILITY_ID", cfg.facility_id)):
        if value and not value.isdigit():
            raise SystemExit(f"Config error: {name} should be a number")
    return cfg
