"""Configuration: a TOML file plus environment overrides.

``load_config()`` reads the path in ``I3B_CONFIG`` (default ``config.toml``), then applies
``I3B_<SECTION>__<KEY>`` environment variables, coercing to the type of the default. The
Discord token is only ever read from ``DISCORD_TOKEN``.
"""

from __future__ import annotations

import dataclasses
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Literal


class ConfigError(ValueError):
    pass


@dataclass(slots=True)
class MudConfig:
    name: str = "Discord"
    admin_email: str = "admin@example.com"
    mudlib: str = "i3bridge 0.1"
    base_mudlib: str = "i3bridge"
    driver: str = "Python/asyncio"
    mud_type: str = "Discord"
    open_status: str = "open for public"
    player_port: int = 0
    tcp_port: int = 0
    udp_port: int = 0


@dataclass(slots=True, frozen=True)
class RouterConfig:
    name: str
    host: str
    port: int


@dataclass(slots=True)
class I3Config:
    reconnect_min_seconds: float = 2.0
    reconnect_max_seconds: float = 300.0
    idle_reconnect_after_seconds: float = 1800.0
    keepalive_interval_seconds: float = 600.0
    max_packet_bytes: int = 2 * 1024 * 1024
    outbound_encoding: str = "utf-8"
    connect_timeout_seconds: float = 20.0
    startup_timeout_seconds: float = 30.0


@dataclass(slots=True)
class StateConfig:
    path: str = "state/bridge.db"


@dataclass(slots=True)
class DiscordConfig:
    guild_id: int = 0
    category_name: str = "InterMUD-3"
    admin_role_id: int = 0  # 0 = find or create admin_role_name
    tells_channel_id: int = 0  # 0 = find or create tells_channel_name
    admin_role_name: str = "i3-admin"
    tells_channel_name: str = "i3-tells"
    auto_provision: bool = True
    channel_prefix: str = "i3-"
    allow_member_join: bool = False


@dataclass(slots=True)
class FormatConfig:
    transliterate: str = "text"  # none | text | all (true/false mean all/none)
    max_outbound_chars: int = 900
    multiline: Literal["join", "split"] = "join"
    color_mode: Literal["strip", "ansi"] = "strip"
    emote_from_italics: bool = True


@dataclass(slots=True)
class PresenceConfig:
    scope: Literal["all", "bridged", "opt-in"] = "all"
    fallback_window_seconds: int = 7200


@dataclass(slots=True)
class LimitsConfig:
    tells_per_minute: int = 10
    messages_per_user_per_minute: int = 30
    channel_flood_messages: int = 20
    channel_flood_window_seconds: int = 10


@dataclass(slots=True)
class HealthConfig:
    bind: str = "127.0.0.1"
    port: int = 8080


@dataclass(slots=True)
class LogConfig:
    level: str = "INFO"
    json: bool = False
    packet_debug: bool = False


@dataclass(slots=True)
class Config:
    mud: MudConfig = field(default_factory=MudConfig)
    routers: list[RouterConfig] = field(default_factory=list)
    i3: I3Config = field(default_factory=I3Config)
    state: StateConfig = field(default_factory=StateConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    format: FormatConfig = field(default_factory=FormatConfig)
    presence: PresenceConfig = field(default_factory=PresenceConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    log: LogConfig = field(default_factory=LogConfig)
    discord_token: str = field(default="", repr=False)

    def validate(self) -> None:
        if not self.mud.name.strip():
            raise ConfigError("mud.name must not be empty")
        if any(ch.isspace() for ch in self.mud.name):
            raise ConfigError("mud.name must not contain whitespace")
        if not self.routers:
            raise ConfigError("at least one [[routers]] entry is required")
        for r in self.routers:
            if not r.name.startswith("*"):
                raise ConfigError(f"router name {r.name!r} should start with '*'")
            if not (0 < r.port < 65536):
                raise ConfigError(f"router {r.name}: bad port {r.port}")
        self.format.transliterate = _normalise_transliterate(self.format.transliterate)
        if self.format.transliterate not in ("none", "text", "all"):
            raise ConfigError("format.transliterate must be none, text or all")
        if self.format.multiline not in ("join", "split"):
            raise ConfigError("format.multiline must be 'join' or 'split'")
        if self.format.color_mode not in ("strip", "ansi"):
            raise ConfigError("format.color_mode must be 'strip' or 'ansi'")
        if self.presence.scope not in ("all", "bridged", "opt-in"):
            raise ConfigError("presence.scope must be all, bridged or opt-in")


def _normalise_transliterate(value: object) -> str:
    """Accept the legacy booleans (true = fold everything, false = send UTF-8 as-is)."""
    text = str(value).strip().lower()
    if text in ("true", "yes", "on", "1"):
        return "all"
    if text in ("false", "no", "off", "0"):
        return "none"
    return text


DEFAULT_ROUTERS = [
    RouterConfig("*dalet", "97.107.133.86", 8787),
    RouterConfig("*i4", "204.209.44.3", 8080),
]

ENV_PREFIX = "I3B_"


def _coerce(default: Any, raw: str, name: str) -> Any:
    if isinstance(default, bool):
        low = raw.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
        raise ConfigError(f"{name}: expected boolean, got {raw!r}")
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigError(f"{name}: expected integer, got {raw!r}") from exc
    if isinstance(default, float):
        try:
            return float(raw)
        except ValueError as exc:
            raise ConfigError(f"{name}: expected number, got {raw!r}") from exc
    return raw


def _section(cls: type[Any], data: dict[str, Any], name: str) -> Any:
    known = {f.name: f for f in fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        raise ConfigError(f"[{name}]: unknown keys {sorted(unknown)}")
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        f = known[key]
        default = f.default if f.default is not dataclasses.MISSING else None
        if isinstance(default, bool) and not isinstance(value, bool):
            raise ConfigError(f"[{name}] {key}: expected boolean")
        if isinstance(default, int) and not isinstance(default, bool) and isinstance(value, bool):
            raise ConfigError(f"[{name}] {key}: expected integer")
        if isinstance(default, float) and isinstance(value, int):
            value = float(value)
        kwargs[key] = value
    return cls(**kwargs)


def _apply_env(cfg: Config, environ: dict[str, str]) -> None:
    for key, raw in environ.items():
        if not key.startswith(ENV_PREFIX) or "__" not in key:
            continue
        section_name, _, field_name = key[len(ENV_PREFIX) :].lower().partition("__")
        if not hasattr(cfg, section_name) or section_name in ("routers", "discord_token"):
            raise ConfigError(f"{key}: unknown config section {section_name!r}")
        section = getattr(cfg, section_name)
        if not hasattr(section, field_name):
            raise ConfigError(f"{key}: unknown key {field_name!r} in [{section_name}]")
        current = getattr(section, field_name)
        setattr(section, field_name, _coerce(current, raw, key))


def load_config(
    path: str | os.PathLike[str] | None = None, environ: dict[str, str] | None = None
) -> Config:
    environ = dict(os.environ if environ is None else environ)
    path = Path(path or environ.get("I3B_CONFIG", "config.toml"))
    data: dict[str, Any] = {}
    if path.exists():
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    cfg = config_from_dict(data)
    _apply_env(cfg, environ)
    cfg.discord_token = environ.get("DISCORD_TOKEN", "")
    cfg.validate()
    return cfg


def config_from_dict(data: dict[str, Any]) -> Config:
    sections = {
        "mud": MudConfig,
        "i3": I3Config,
        "state": StateConfig,
        "discord": DiscordConfig,
        "format": FormatConfig,
        "presence": PresenceConfig,
        "limits": LimitsConfig,
        "health": HealthConfig,
        "log": LogConfig,
    }
    unknown = set(data) - set(sections) - {"routers"}
    if unknown:
        raise ConfigError(f"unknown top-level sections {sorted(unknown)}")
    kwargs: dict[str, Any] = {
        name: _section(cls, data.get(name, {}), name) for name, cls in sections.items()
    }
    # normalise before env overrides so a TOML boolean does not make the field look boolean
    kwargs["format"].transliterate = _normalise_transliterate(kwargs["format"].transliterate)
    routers_raw = data.get("routers")
    if routers_raw is None:
        kwargs["routers"] = list(DEFAULT_ROUTERS)
    else:
        if not isinstance(routers_raw, list):
            raise ConfigError("[[routers]] must be an array of tables")
        kwargs["routers"] = [_section(RouterConfig, r, "routers") for r in routers_raw]
    return Config(**kwargs)
