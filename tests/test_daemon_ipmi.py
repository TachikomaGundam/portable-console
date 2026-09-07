"""IPMI collector tests — ipmitool fully faked; local-BMC denial and
remote-BMC credential handling both covered without root or hardware."""
from __future__ import annotations

import subprocess
from dataclasses import replace

import pytest

from portable_console.daemon.config import (BmcConfig, ConsoleConfig, IpmiPluginConfig)
from portable_console.daemon.plugins import ipmi
from portable_console.daemon.plugins.ipmi import IpmiCollector

# Supermicro `ipmitool sdr type Fan` emits five pipe columns:
# name | sensor-hex | status | entity | "N RPM" (source:691-696; sample
# verified live: "FAN1 | 41h | ok | 29.1 | 4200 RPM").
SDR_OUT = (
    "FAN1  | 41h | ok | 29.1 | 3300 RPM\n"
    "FAN2  | 42h | ok | 29.2 | 3270 RPM\n"
    "FAN3  | 43h | ns | 29.3 | No Reading\n"
    "FAN4  | 44h | ok | 29.4 | 4200 RPM\n"
)
DCMI_OUT = """DCMI Power Reading Test
    Instantaneous power reading:                   849 Watts
    Minimum during sampling period:                800 Watts
    Maximum during sampling period:                901 Watts
"""
POWER_OK = "Chassis Power is on\n"


def make_ipmi_cfg(tmp_path, bmc: BmcConfig | None = None) -> ConsoleConfig:
    from conftest import _make_config
    cfg = _make_config(tmp_path)
    return replace(cfg, plugins=replace(cfg.plugins,
                                        ipmi=IpmiPluginConfig(bmc=bmc)))


def fake_ipmitool(sdr=SDR_OUT, dcmi=DCMI_OUT, power=POWER_OK, power_rc=0):
    def fake(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        if argv[-3:] == ["sdr", "type", "Fan"]:
            return subprocess.CompletedProcess(argv, 0, sdr, "")
        if argv[-3:] == ["dcmi", "power", "reading"]:
            return subprocess.CompletedProcess(argv, 0, dcmi, "")
        if argv[-2:] == ["power", "status"]:
            return subprocess.CompletedProcess(argv, power_rc, power, "")
        raise AssertionError(f"unexpected argv {argv}")
    return fake


def test_probe_local_ok(tmp_path,
                        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ipmi.shutil, "which", lambda x: "/usr/bin/ipmitool")
    monkeypatch.setattr(ipmi, "run_argv", fake_ipmitool())
    assert IpmiCollector().probe(make_ipmi_cfg(tmp_path)) is True


def test_probe_denied_for_nonroot_local(tmp_path,
                                        monkeypatch: pytest.MonkeyPatch
                                        ) -> None:
    monkeypatch.setattr(ipmi.shutil, "which", lambda x: "/usr/bin/ipmitool")

    def denied(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "",
                                           "Could not open device")
    monkeypatch.setattr(ipmi, "run_argv", denied)
    assert IpmiCollector().probe(make_ipmi_cfg(tmp_path)) is False


def test_collect_fans_and_dcmi_power(tmp_path,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ipmi, "run_argv", fake_ipmitool())
    section = IpmiCollector().collect(make_ipmi_cfg(tmp_path), {})
    assert section["fans"] == [{"name": "FAN1", "rpm": 3300},
                               {"name": "FAN2", "rpm": 3270},
                               {"name": "FAN4", "rpm": 4200}]  # ns row skipped
    assert section["power"] == {"current_w": 849}


def test_bmc_credentials_argv_and_env_password(tmp_path, monkeypatch,
                                               ) -> None:
    bmc = BmcConfig(host="10.0.0.5", user="admin", password_env="BMC_PW")
    monkeypatch.setenv("BMC_PW", "secret")
    seen: list[list[str]] = []

    def spy(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        seen.append(list(argv))
        return fake_ipmitool()(argv, timeout)
    monkeypatch.setattr(ipmi, "run_argv", spy)
    monkeypatch.setattr(ipmi.shutil, "which", lambda x: "/usr/bin/ipmitool")
    cfg = make_ipmi_cfg(tmp_path, bmc=bmc)
    assert IpmiCollector().probe(cfg) is True
    assert seen[0] == ["ipmitool", "-I", "lanplus", "-H", "10.0.0.5",
                       "-U", "admin", "-P", "secret", "power", "status"]

    monkeypatch.delenv("BMC_PW")                 # unset pw -> disabled
    assert IpmiCollector().probe(cfg) is False
    assert IpmiCollector().collect(cfg, {}) == {}


def test_power_gpu_sum_fallback_when_dcmi_silent(tmp_path,
                                                 monkeypatch) -> None:
    monkeypatch.setattr(ipmi, "run_argv", fake_ipmitool(dcmi=""))
    ctx = {"gpus": [{"power_w": 100.5}, {"power_w": 200.4}]}
    section = IpmiCollector().collect(make_ipmi_cfg(tmp_path), ctx)
    assert section["power"] == {"current_w": 301}  # 300.9 rounded


def test_power_key_omitted_without_dcmi_or_gpus(tmp_path,
                                                monkeypatch) -> None:
    monkeypatch.setattr(ipmi, "run_argv", fake_ipmitool(dcmi=""))
    section = IpmiCollector().collect(make_ipmi_cfg(tmp_path), {})
    assert "power" not in section
    assert section["fans"]                        # fans still reported
