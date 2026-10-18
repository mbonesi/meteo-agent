"""Configuration loading and validation.

"Project" parameters live in the TOML file (version-controlled).
Secrets (tokens, API keys) live ONLY in the environment, never in the TOML.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Internal canonical units, valid for ANY source:
#   temperature   -> degrees C
#   precipitation -> mm
#   wind speed    -> km/h
#   wind direction-> degrees (0 = North, clockwise)
#   humidity      -> %
#   pressure      -> hPa
# Sources convert into these units on ingestion, so rules are written once
# and hold for both forecast and observation.


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Location:
    latitude: float
    longitude: float
    elevation_m: float
    timezone: str = "Europe/Rome"


@dataclass(frozen=True)
class OpenMeteoConfig:
    enabled: bool = True
    model: str = "best_match"
    forecast_days: int = 4
    endpoint: str = "https://api.open-meteo.com/v1/forecast"
    timeout_s: float = 30.0


@dataclass(frozen=True)
class ArpaeConfig:
    enabled: bool = True
    endpoint: str = "https://dati-simc.arpae.it/opendata/osservati/meteo/realtime/realtime.jsonl"
    timeout_s: float = 120.0
    # "Pinned" station: integer lon/lat *1e5, per the RMAP data model.
    # If station_lon/station_lat are None, the nearest usable station within
    # max_distance_km is used (handy on first run, not recommended long-term:
    # a station going offline would be silently swapped for another).
    station_lon: int | None = None
    station_lat: int | None = None
    station_network: str | None = None
    max_distance_km: float = 15.0
    # Confidence threshold below which a reading is dropped (B33007 attribute).
    min_confidence: float = 1.0
    # How often to download the feed (~8.6 MB). 0 = every cycle. The feed keeps
    # about 6 h of history, so downloading it less often loses no data: it only
    # delays observation rules. Must be a multiple of interval_minutes.
    interval_minutes: int = 0


@dataclass(frozen=True)
class StoreConfig:
    path: str = "/var/lib/meteo-agent/meteo.db"
    retention_h: int = 72


@dataclass(frozen=True)
class TelegramConfig:
    enabled: bool = True
    timeout_s: float = 20.0
    token: str = ""  # from env TELEGRAM_BOT_TOKEN
    chat_id: str = ""  # from env TELEGRAM_CHAT_ID


LLM_PROVIDERS = ("anthropic", "openai")

# Per-provider defaults. For "openai" there are no sensible ones (GitHub,
# OpenAI, a proxy...): the model and the key's variable must be given.
DEFAULT_MODEL = {"anthropic": "claude-haiku-4-5-20251001"}
DEFAULT_API_KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY"}

_ENV_NAME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


@dataclass(frozen=True)
class LLMConfig:
    """The prose writer. Section [llm]; [claude] is accepted as a legacy alias."""

    enabled: bool = True
    # "anthropic"  Anthropic API, via the SDK.
    # "openai"     any endpoint compatible with OpenAI chat/completions
    #              (GitHub Models with a PAT, OpenAI, a local proxy...).
    provider: str = "anthropic"
    model: str = ""  # empty = the provider's default
    endpoint: str = ""  # "openai" only: full URL of .../chat/completions
    api_key_env: str = ""  # NAME of the environment variable holding the key, not the key
    max_tokens: int = 400
    timeout_s: float = 30.0
    api_key: str = ""  # read from os.environ[api_key_env], never from the TOML

    def validate(self) -> None:
        if self.provider not in LLM_PROVIDERS:
            raise ConfigError(
                f"invalid [llm].provider: {self.provider!r} (valid: {', '.join(LLM_PROVIDERS)})"
            )
        if self.provider == "openai" and not self.endpoint:
            raise ConfigError("[llm].provider = 'openai' requires [llm].endpoint")
        if self.provider == "anthropic" and self.endpoint:
            raise ConfigError("[llm].endpoint is only used with provider = 'openai'")
        if not self.model_name:
            raise ConfigError(f"[llm].provider = {self.provider!r} requires [llm].model")
        if not self.key_env:
            raise ConfigError(
                f"[llm].provider = {self.provider!r} requires [llm].api_key_env "
                "(the name of the environment variable holding the key)"
            )
        # Guard against the key being pasted in place of the variable name.
        if self.key_env[0].isdigit() or not set(self.key_env) <= _ENV_NAME_CHARS:
            raise ConfigError(
                f"[llm].api_key_env must be the NAME of an environment variable "
                f"(e.g. ANTHROPIC_API_KEY), not the key: {self.key_env[:6]!r}..."
            )

    @property
    def model_name(self) -> str:
        """Effective model: the configured one or the provider's default."""
        return self.model or DEFAULT_MODEL.get(self.provider, "")

    @property
    def key_env(self) -> str:
        """Effective environment variable: the configured one or the provider's default."""
        return self.api_key_env or DEFAULT_API_KEY_ENV.get(self.provider, "")


@dataclass(frozen=True)
class Rule:
    name: str
    when: str
    title: str = ""
    enabled: bool = True
    source: str = "forecast"  # "forecast" | "observation"
    horizon_h: int = 48
    mode: str = "any"  # "any" | "all" | "consecutive" | "count"
    min_hours: int = 1
    cooldown_h: int = 6
    trigger: str = "edge"  # "edge" (only on false->true transition) | "level"

    def validate(self) -> None:
        if self.source not in ("forecast", "observation"):
            raise ConfigError(f"rule '{self.name}': invalid source: {self.source!r}")
        if self.mode not in ("any", "all", "consecutive", "count"):
            raise ConfigError(f"rule '{self.name}': invalid mode: {self.mode!r}")
        if self.trigger not in ("edge", "level"):
            raise ConfigError(f"rule '{self.name}': invalid trigger: {self.trigger!r}")
        if self.min_hours < 1:
            raise ConfigError(f"rule '{self.name}': min_hours must be >= 1")
        if self.horizon_h < 1:
            raise ConfigError(f"rule '{self.name}': horizon_h must be >= 1")


@dataclass(frozen=True)
class Config:
    location: Location
    interval_minutes: int = 15
    log_level: str = "INFO"
    openmeteo: OpenMeteoConfig = field(default_factory=OpenMeteoConfig)
    arpae: ArpaeConfig = field(default_factory=ArpaeConfig)
    store: StoreConfig = field(default_factory=StoreConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    rules: tuple[Rule, ...] = ()


def _sub(raw: dict, key: str) -> dict:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"section [{key}] must be a table")
    return value


def _build(cls, data: dict, section: str):
    """Builds a dataclass from the TOML dict, rejecting unknown keys.

    A typo in a config file is a silent bug: better to fail immediately.
    """
    known = {f.name for f in cls.__dataclass_fields__.values()}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"[{section}]: unknown keys: {sorted(unknown)}")
    return cls(**data)


def load_env_file(path: str | Path) -> None:
    """Loads a KEY=VALUE file into the environment without overwriting variables already set.

    Useful in development. In production, use systemd's EnvironmentFile=.
    """
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def load(path: str | Path) -> Config:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"config file not found: {p}")
    with p.open("rb") as fh:
        raw = tomllib.load(fh)

    if "location" not in raw:
        raise ConfigError("[location] section is required")
    location = _build(Location, _sub(raw, "location"), "location")

    rules: list[Rule] = []
    for i, item in enumerate(raw.get("rules", [])):
        if not isinstance(item, dict) or "name" not in item:
            raise ConfigError(f"[[rules]] #{i}: missing 'name' field")
        rule = _build(Rule, item, f"rules.{item['name']}")
        rule.validate()
        rules.append(rule)

    names = [r.name for r in rules]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ConfigError(f"duplicate rule names: {sorted(dupes)}")

    # [claude] is the legacy name of [llm]: still accepted, but not both.
    if "llm" in raw and "claude" in raw:
        raise ConfigError("use [llm] or [claude] (legacy name), not both")
    llm_section = "claude" if "claude" in raw else "llm"

    for section in ("telegram", llm_section):
        for secret in ("token", "chat_id", "api_key"):
            if secret in _sub(raw, section):
                raise ConfigError(
                    f"[{section}].{secret} does not belong in the TOML: use an environment variable"
                )

    telegram = _build(TelegramConfig, _sub(raw, "telegram"), "telegram")
    llm = _build(LLMConfig, _sub(raw, llm_section), llm_section)
    llm.validate()

    # Secrets come only from the environment.
    telegram = replace_secrets(
        telegram,
        token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
    )
    llm = LLMConfig(
        enabled=llm.enabled,
        provider=llm.provider,
        model=llm.model,
        endpoint=llm.endpoint,
        api_key_env=llm.api_key_env,
        max_tokens=llm.max_tokens,
        timeout_s=llm.timeout_s,
        api_key=os.environ.get(llm.key_env, ""),
    )

    cfg = Config(
        location=location,
        interval_minutes=int(raw.get("interval_minutes", 15)),
        log_level=str(raw.get("log_level", "INFO")),
        openmeteo=_build(OpenMeteoConfig, _sub(raw, "openmeteo"), "openmeteo"),
        arpae=_build(ArpaeConfig, _sub(raw, "arpae"), "arpae"),
        store=_build(StoreConfig, _sub(raw, "store"), "store"),
        telegram=telegram,
        llm=llm,
        rules=tuple(rules),
    )

    if cfg.interval_minutes < 1:
        raise ConfigError("interval_minutes must be >= 1")
    if cfg.store.retention_h < 1:
        raise ConfigError("[store].retention_h must be >= 1")
    if cfg.arpae.interval_minutes < 0:
        raise ConfigError("[arpae].interval_minutes must be >= 0 (0 = every cycle)")
    if cfg.arpae.interval_minutes % cfg.interval_minutes != 0:
        raise ConfigError(
            f"[arpae].interval_minutes ({cfg.arpae.interval_minutes}) must be a "
            f"multiple of interval_minutes ({cfg.interval_minutes})"
        )
    if not (cfg.openmeteo.enabled or cfg.arpae.enabled):
        raise ConfigError("no data source enabled")
    if cfg.telegram.enabled and not (cfg.telegram.token and cfg.telegram.chat_id):
        raise ConfigError(
            "[telegram].enabled = true but TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not in the environment"
        )
    if cfg.llm.enabled and not cfg.llm.api_key:
        raise ConfigError(
            f"[{llm_section}].enabled = true but {cfg.llm.key_env} is not in the environment"
        )

    for rule in cfg.rules:
        if rule.source == "observation" and not cfg.arpae.enabled:
            raise ConfigError(
                f"rule '{rule.name}' uses source='observation' but [arpae].enabled = false"
            )
        if rule.source == "forecast" and not cfg.openmeteo.enabled:
            raise ConfigError(
                f"rule '{rule.name}' uses source='forecast' but [openmeteo].enabled = false"
            )

    return cfg


def replace_secrets(tg: TelegramConfig, token: str, chat_id: str) -> TelegramConfig:
    return TelegramConfig(
        enabled=tg.enabled, timeout_s=tg.timeout_s, token=token, chat_id=chat_id
    )
