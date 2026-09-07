"""IPMI (BMC) collector: fans + whole-system power (DESIGN §3 fans/power).

Port of parse-stats.py get_fan_speeds()/get_system_power_w() (lines 663-704)
with a root-free probe gate:

- If ``plugins.ipmi.bmc`` is configured, probe via
  ``ipmitool -I lanplus -H <host> -U <user> -P <env password> power status``.
  The password lives ONLY in the environment variable named by
  ``password_env`` — never in the config file, never logged.
- Otherwise try the LOCAL interface (``ipmitool power status``), which needs
  root/ipmi-perms on most machines; on failure the whole plugin is disabled
  and the ``fans``/``power`` keys stay absent from health.json (a rootless
  laptop therefore degrades cleanly, DESIGN §0).
- Fan names are auto-discovered from ``ipmitool sdr type Fan`` — no
  hardcoded FAN1..FAN10 list (source never hardcoded either; it parsed only
  'ok' status rows).
- power.current_w comes from ``ipmitool dcmi power reading``; when DCMI has
  no answer but GPUs were collected this cycle, fall back to the GPU
  power_w sum (source:1454). If neither is known the power key is omitted.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

from portable_console.daemon.plugins.base import run_argv

if False:  # pragma: no cover - type-only
    from portable_console.daemon.config import BmcConfig, ConsoleConfig

_SDR_TIMEOUT_S = 5.0
_POWER_TIMEOUT_S = 3.0


def _bmc_password(bmc: "BmcConfig") -> str:
    """Resolve the BMC password from the named env var (empty when unset)."""
    return os.environ.get(bmc.password_env, "")


def _base_argv(cfg: "ConsoleConfig") -> list[str] | None:
    """ipmitool argv prefix: lanplus+credentials when a BMC is configured and
    its password env var is set; plain local ipmitool otherwise; None when no
    BMC is configured AND the local interface is unavailable (probe would
    have failed, this guards collect)."""
    bmc = cfg.plugins.ipmi.bmc
    if bmc is not None:
        password = _bmc_password(bmc)
        if not password:
            return None
        return ["ipmitool", "-I", "lanplus", "-H", bmc.host,
                "-U", bmc.user, "-P", password]
    return ["ipmitool"]


def get_fan_speeds(prefix: list[str]) -> list[dict[str, Any]]:
    """[{name, rpm}] for fans reporting 'ok' with an RPM reading (source:681)."""
    try:
        r = run_argv(prefix + ["sdr", "type", "Fan"], timeout=_SDR_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return []
    fans: list[dict[str, Any]] = []
    for line in r.stdout.splitlines():
        fields = [f.strip() for f in line.split("|")]
        if len(fields) < 5:
            continue
        if fields[2] != "ok":  # skip disconnected (ns) / non-ok rows
            continue
        name = fields[0]
        reading = fields[4]  # e.g. "3300 RPM"
        parts = reading.split()
        if len(parts) < 2 or parts[1] != "RPM":
            continue
        try:
            rpm = int(float(parts[0]))
        except ValueError:
            continue
        fans.append({"name": name, "rpm": rpm})
    return fans


def get_system_power_w(prefix: list[str]) -> float | None:
    """Instantaneous whole-machine watts via DCMI (source:663)."""
    try:
        r = run_argv(prefix + ["dcmi", "power", "reading"],
                     timeout=_POWER_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in r.stdout.splitlines():
        if "Instantaneous" in line:
            parts = line.split()
            for i, w in enumerate(parts):
                if "Watts" in w and i > 0:
                    try:
                        return float(parts[i - 1])
                    except ValueError:
                        return None
    return None


class IpmiCollector:
    """fans[] + power{current_w} — or nothing when BMC access is unavailable."""

    name = "ipmi"

    def probe(self, cfg: "ConsoleConfig") -> bool:
        if shutil.which("ipmitool") is None:
            return False
        prefix = _base_argv(cfg)
        if prefix is None:
            return False
        try:
            r = run_argv(prefix + ["power", "status"], timeout=_POWER_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return r.returncode == 0 and "Chassis Power is" in r.stdout

    def collect(self, cfg: "ConsoleConfig", ctx: dict[str, Any]) -> dict[str, Any]:
        prefix = _base_argv(cfg)
        if prefix is None:
            return {}
        section: dict[str, Any] = {"fans": get_fan_speeds(prefix)}

        power_w = get_system_power_w(prefix)
        if power_w is None:
            gpus = ctx.get("gpus") or []
            total = sum(float(g.get("power_w", 0.0)) for g in gpus)
            power_w = total if total > 0 else None
        if power_w is not None:
            # consumption_*_kwh are merged in by the harness from the usage
            # accumulator (cross-plugin concern, DESIGN §3).
            section["power"] = {"current_w": int(round(power_w))}
        return section
