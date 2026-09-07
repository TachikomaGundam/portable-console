"""Typed configuration for the portable console bundle (DESIGN.md §2).

Loads `console.config.json`; every relative path is resolved against the
bundle root (the directory containing `console.py`). Zero third-party
dependencies; Python 3.10+ stdlib only.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# bundle root = console/ (this file lives at console/daemon/config.py)
BUNDLE_ROOT: Path = Path(__file__).resolve().parents[1]


class ConfigError(ValueError):
    """Raised when the config file is unreadable or malformed."""


def _as_dict(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{what} must be an object")
    return value


def _as_int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{what} must be a number")
    return int(value)


def _as_float(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{what} must be a number")
    return float(value)


@dataclass(frozen=True, slots=True)
class ListenConfig:
    host: str = "127.0.0.1"
    port: int = 8090


@dataclass(frozen=True, slots=True)
class HistoryConfig:
    max_samples: int = 720


@dataclass(frozen=True, slots=True)
class SystemPluginConfig:
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class NvidiaPluginConfig:
    auto: bool = True


@dataclass(frozen=True, slots=True)
class BmcConfig:
    """Out-of-band BMC endpoint. The password is NEVER stored in the config
    file: `password_env` names an environment variable read at probe time."""

    host: str
    user: str
    password_env: str


@dataclass(frozen=True, slots=True)
class IpmiPluginConfig:
    auto: bool = True
    bmc: BmcConfig | None = None


@dataclass(frozen=True, slots=True)
class LlmPluginConfig:
    auto: bool = True
    non_llm_ports: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class PluginsConfig:
    system: SystemPluginConfig = field(default_factory=SystemPluginConfig)
    nvidia: NvidiaPluginConfig = field(default_factory=NvidiaPluginConfig)
    ipmi: IpmiPluginConfig = field(default_factory=IpmiPluginConfig)
    llm: LlmPluginConfig = field(default_factory=LlmPluginConfig)


@dataclass(frozen=True, slots=True)
class ConsoleConfig:
    bundle_root: Path
    listen: ListenConfig
    data_dir: Path
    poll_interval_s: float
    history: HistoryConfig
    plugins: PluginsConfig
    # cards / links are consumed by the server agent (§2/§4); the daemon only
    # carries them through, as opaque validated-JSON objects.
    cards: tuple[dict[str, Any], ...] = ()
    links: tuple[dict[str, Any], ...] = ()


def _parse_bmc(raw: Any) -> BmcConfig | None:
    if raw is None:
        return None
    d = _as_dict(raw, "plugins.ipmi.bmc")
    host = d.get("host")
    user = d.get("user")
    password_env = d.get("password_env")
    if not (isinstance(host, str) and host):
        raise ConfigError("plugins.ipmi.bmc.host must be a non-empty string")
    if not (isinstance(user, str) and user):
        raise ConfigError("plugins.ipmi.bmc.user must be a non-empty string")
    if not (isinstance(password_env, str) and password_env):
        raise ConfigError(
            "plugins.ipmi.bmc.password_env must name an environment variable"
        )
    return BmcConfig(host=host, user=user, password_env=password_env)


def _parse_plugins(raw: Any) -> PluginsConfig:
    d = _as_dict(raw, "plugins") if raw is not None else {}
    system_d = _as_dict(d.get("system", {}), "plugins.system")
    nvidia_d = _as_dict(d.get("nvidia", {}), "plugins.nvidia")
    ipmi_d = _as_dict(d.get("ipmi", {}), "plugins.ipmi")
    llm_d = _as_dict(d.get("llm", {}), "plugins.llm")
    ports = llm_d.get("non_llm_ports", [])
    if not isinstance(ports, list) or any(
        isinstance(p, bool) or not isinstance(p, int) for p in ports
    ):
        raise ConfigError("plugins.llm.non_llm_ports must be a list of ints")
    return PluginsConfig(
        system=SystemPluginConfig(enabled=bool(system_d.get("enabled", True))),
        nvidia=NvidiaPluginConfig(auto=bool(nvidia_d.get("auto", True))),
        ipmi=IpmiPluginConfig(
            auto=bool(ipmi_d.get("auto", True)), bmc=_parse_bmc(ipmi_d.get("bmc"))
        ),
        llm=LlmPluginConfig(auto=bool(llm_d.get("auto", True)),
                            non_llm_ports=tuple(ports)),
    )


def _resolve_path(root: Path, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else (root / p)


def load(path: str | Path) -> ConsoleConfig:
    """Parse a console config file into a typed, frozen ConsoleConfig.

    Defaults follow DESIGN.md §2; unknown keys are ignored so the file may
    carry server-side sections (cards/links) this module does not consume.
    """
    p = Path(path)
    try:
        raw = _as_dict(json.loads(p.read_text(encoding="utf-8")), "config")
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {p}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config file is not valid JSON: {p}: {exc}") from exc

    root = BUNDLE_ROOT
    listen_raw = _as_dict(raw.get("listen", {}), "listen")
    listen = ListenConfig(
        host=str(listen_raw.get("host", "127.0.0.1")),
        port=_as_int(listen_raw.get("port", 8090), "listen.port"),
    )
    history_raw = _as_dict(raw.get("history", {}), "history")
    cards = raw.get("cards", [])
    links = raw.get("links", [])
    if not isinstance(cards, list) or any(not isinstance(c, dict) for c in cards):
        raise ConfigError("cards must be a list of objects")
    if not isinstance(links, list) or any(not isinstance(l, dict) for l in links):
        raise ConfigError("links must be a list of objects")
    return ConsoleConfig(
        bundle_root=root,
        listen=listen,
        data_dir=_resolve_path(root, str(raw.get("data_dir", "data"))),
        poll_interval_s=_as_float(
            raw.get("poll_interval_s", 1), "poll_interval_s"
        ),
        history=HistoryConfig(
            max_samples=_as_int(
                history_raw.get("max_samples", 720), "history.max_samples"
            )
        ),
        plugins=_parse_plugins(raw.get("plugins")),
        cards=tuple(cards),
        links=tuple(links),
    )
