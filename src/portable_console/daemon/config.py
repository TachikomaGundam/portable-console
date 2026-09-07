"""Typed configuration for the portable console bundle (DESIGN.md §2).

Loads `console.config.json`; every relative path is resolved against the
bundle root (the directory containing `console.py`). Zero third-party
dependencies; Python 3.10+ stdlib only.

v0.2.0 (packaging): this module moved into the portable_console package
(console/src/portable_console/daemon/config.py). The "bundle root" anchor is
now `root_dir` = the directory containing the config file (or cwd when there
is none); all resolution helpers live in portable_console.paths. Relative
`data_dir` semantics are unchanged (resolved against root_dir); when the key
is absent the XDG-aware portable_console.paths.default_data_dir applies.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from portable_console import paths

# bundle root = console/ (this file lives at console/daemon/config.py)
# v0.2.0: kept only as a deprecated alias for the package directory (this file
# now lives at .../portable_console/daemon/config.py). No real uses remain —
# use portable_console.paths / ConsoleConfig.root_dir instead.
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
    # v0.2.0: field `bundle_root` renamed to `root_dir` (the config file's
    # directory, or cwd). `bundle_root` survives as a deprecated alias below.
    root_dir: Path
    listen: ListenConfig
    data_dir: Path
    poll_interval_s: float
    history: HistoryConfig
    plugins: PluginsConfig
    # cards / links are consumed by the server agent (§2/§4); the daemon only
    # carries them through, as opaque validated-JSON objects.
    cards: tuple[dict[str, Any], ...] = ()
    links: tuple[dict[str, Any], ...] = ()

    @property
    def bundle_root(self) -> Path:
        """Deprecated v0.1.0 alias for root_dir (kept to minimize churn)."""
        return self.root_dir


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

    # v0.2.0: was `root = BUNDLE_ROOT` (the console.py directory). The anchor
    # is now the config file's own directory (portable_console.paths.root_dir).
    root = paths.root_dir(p)
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
    # v0.2.0: an explicit `data_dir` still wins, resolved against root (mirrors
    # the old `_resolve_path(root, ...)` semantics). When the key is absent we
    # no longer default to bundle-relative "data"; instead paths.default_data_dir
    # applies the legacy <config_dir>/data rule, then XDG_DATA_HOME, then
    # ~/.local/share/portable-console.
    if "data_dir" in raw:
        data_dir = _resolve_path(root, str(raw["data_dir"]))
    else:
        data_dir = paths.default_data_dir(p)
    return ConsoleConfig(
        root_dir=root,
        listen=listen,
        data_dir=data_dir,
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
