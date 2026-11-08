"""Non-secret configuration loaded from a TOML file. Never holds passwords."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .safe_imap import DEFAULT_HOST, DEFAULT_PORT

DEFAULT_CONFIG_PATH = Path("~/.config/email-archiver/config.toml").expanduser()

_SCHEMA = {
    "imap": {"host", "port", "username", "timeout"},
    "search": {"senders", "folders", "exclude_folders"},
    "limits": {"max_messages_per_pdf", "max_mb_per_pdf"},
}
_FORBIDDEN_KEYS = {"password", "app_password", "passwd", "secret"}


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    username: str | None = None
    timeout: float = 60.0
    senders: list[str] = field(default_factory=list)
    folders: list[str] | None = None
    exclude_folders: list[str] = field(default_factory=list)
    max_messages_per_pdf: int = 1500
    max_mb_per_pdf: int = 200


def load_config(path: Path | None, required: bool = False) -> Config:
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        if required:
            raise ConfigError(f"Config file not found: {path}")
        return Config()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    cfg = Config()
    for section, values in data.items():
        if section not in _SCHEMA or not isinstance(values, dict):
            raise ConfigError(f"{path}: unknown section [{section}]")
        for key, value in values.items():
            if key.lower() in _FORBIDDEN_KEYS:
                raise ConfigError(f"{path}: passwords must not be stored in the config file")
            if key not in _SCHEMA[section]:
                raise ConfigError(f"{path}: unknown key '{key}' in [{section}]")
            setattr(cfg, key, value)
    return cfg
