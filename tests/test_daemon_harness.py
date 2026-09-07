"""Harness tests — section merge, honest absence, atomic writes, history.

Stub collectors only; the real nvidia/ipmi probes are forced False so a
machine without GPU/BMC hardware (CI, laptops) exercises the degrade path.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from portable_console.daemon.config import ConsoleConfig
from portable_console.daemon.harness import (Harness, build_snapshot, write_json_atomic)
from portable_console.daemon.plugins.base import Prober


class Stub:
    def __init__(self, name: str, section: dict[str, Any],
                 ok: bool = True) -> None:
        self.name = name
        self.section = section
        self.ok = ok
        self.collects = 0

    def probe(self, cfg: ConsoleConfig) -> bool:
        return self.ok

    def collect(self, cfg: ConsoleConfig, ctx: dict[str, Any]) -> dict[str, Any]:
        self.collects += 1
        return dict(self.section)


class Boom:
    name = "boom"

    def probe(self, cfg: ConsoleConfig) -> bool:
        return True

    def collect(self, cfg: ConsoleConfig, ctx: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("sensor exploded")


def _system_section() -> dict[str, Any]:
    return {"system": {"cpu_cores": 8, "load_1m": 0.5, "mem_used_pct": 40.0,
                       "disks": [{"path": "/", "used_pct": 55.0},
                                 {"path": "/mnt/data", "used_pct": 10.0}]}}


# ---------- (a) no-nvidia / no-ipmi degrade: keys ABSENT, no crash ----------
def test_no_gpu_no_bmc_absent_correct(tmp_path: Path,
                                      make_config: ConsoleConfig) -> None:
    sys_stub = Stub("system", _system_section()["system"])
    h = Harness(make_config, collectors=[
        sys_stub,
        Stub("nvidia", {}, ok=False),
        Stub("ipmi", {}, ok=False),
        Stub("llm", {"llm": {"backend": None}}),
    ])
    health = h.run_once()
    assert health["cpu_cores"] == 8                    # system merged flat
    assert "gpus" not in health                        # absent, not []/None
    assert "fans" not in health and "power" not in health
    assert health["llm"] == {"backend": None}
    assert isinstance(health["updated"], float)
    assert "updated_iso" in health
    data = make_config.data_dir
    written = json.loads((data / "health.json").read_text())
    assert "gpus" not in written and written["cpu_cores"] == 8


def test_broken_plugin_skipped_others_survive(make_config: ConsoleConfig) -> None:
    llm_stub = Stub("llm", {"llm": {"backend": "vllm"}})
    h = Harness(make_config, collectors=[Boom(), Stub("system", {"cpu_cores": 1}),
                                         llm_stub])
    health = h.run_once()
    assert health["cpu_cores"] == 1 and health["llm"]["backend"] == "vllm"


# ---------- power cross-merge (ipmi owns key, llm owns kWh) ----------
def test_power_consumption_merged_from_llm_aggregates(
        make_config: ConsoleConfig) -> None:
    class LlmWithAgg(Stub):
        def collect(self, cfg, ctx):
            ctx["usage_aggregates"] = {"consumption_24h_kwh": 12.5,
                                       "consumption_30d_kwh": 300.25}
            return {"llm": {}}
    h = Harness(make_config, collectors=[
        Stub("ipmi", {"fans": [], "power": {"current_w": 800}}),
        LlmWithAgg("llm", {}),
    ])
    health = h.run_once()
    assert health["power"] == {"current_w": 800, "consumption_24h_kwh": 12.5,
                               "consumption_30d_kwh": 300.25}


# ---------- (e) atomic write is 0644 even under hostile umask ----------
def test_atomic_write_is_0644_under_umask_077(tmp_path: Path) -> None:
    old = os.umask(0o077)
    try:
        p = tmp_path / "sub" / "health.json"
        write_json_atomic(p, {"a": 1})
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode == 0o644
        assert json.loads(p.read_text()) == {"a": 1}
    finally:
        os.umask(old)


# ---------- history: rolling cap + reload from disk every poll ----------
def test_history_caps_and_reloads(make_config: ConsoleConfig) -> None:
    from portable_console.daemon.config import HistoryConfig
    from dataclasses import replace
    cfg = replace(make_config, history=HistoryConfig(max_samples=2))
    h = Harness(cfg, collectors=[Stub("system", {"load_1m": 1.0})])
    for _ in range(3):
        h.run_once()
    hist = json.loads((cfg.data_dir / "health.history.json").read_text())
    assert len(hist["samples"]) == 2
    # an external trim sticks because the file is reloaded each poll
    hist["samples"] = hist["samples"][:1]
    (cfg.data_dir / "health.history.json").write_text(json.dumps(hist))
    h.run_once()
    hist = json.loads((cfg.data_dir / "health.history.json").read_text())
    assert len(hist["samples"]) == 2


def test_stats_json_written_from_llm_ctx(make_config: ConsoleConfig) -> None:
    class StatsWriter(Stub):
        def collect(self, cfg, ctx):
            ctx["stats"] = {"updated": 1.0, "total_requests": 7}
            return {"llm": {"backend": "llamacpp"}}
    h = Harness(make_config, collectors=[StatsWriter("llm", {})])
    h.run_once()
    stats = json.loads((make_config.data_dir / "stats.json").read_text())
    assert stats["total_requests"] == 7
    # no stats.json written when llm disabled/failed
    (make_config.data_dir / "stats.json").unlink()
    h2 = Harness(make_config, collectors=[Stub("system", {"cpu_cores": 2})])
    h2.run_once()
    assert not (make_config.data_dir / "stats.json").exists()


# ---------- snapshot shape (golden history keys) ----------
def test_build_snapshot_dynamic_gpu_fan_keys() -> None:
    health = {
        "load_1m": 1.0, "load_5m": 0.9, "load_15m": 0.8,
        "mem_used_pct": 30.0, "swap_used_pct": 5.0,
        "disks": [{"path": "/", "used_pct": 55.0},
                  {"path": "/mnt/data", "used_pct": 10.0}],
        "gpus": [{"use_pct": 42, "temp_junction_c": 60.0, "temp_edge_c": 55.0,
                  "vram_alloc_pct": 80.0, "power_w": 120.0}],
        "fans": [{"name": "FAN1", "rpm": 4200}],
        "power": {"current_w": 800, "consumption_24h_kwh": 1.0,
                  "consumption_30d_kwh": 2.0},
    }
    stats = {"last_gen_tps": 30.0, "last_prompt_tps": 100.0,
             "kv_cache_fill_pct": 20.0, "queue_depth": 1,
             "server_uptime_s": 50.0, "ttft_ms": 120.0, "tpot_ms": None,
             "prefix_hit_pct": None, "waiting": 0, "prompt_cached_pct": None}
    snap = build_snapshot(health, stats)
    assert snap["gpu0_util"] == 42 and snap["gpu0_power"] == 120.0
    assert snap["fan0_rpm"] == 4200
    assert snap["disk_os_used_pct"] == 55.0
    assert snap["disk_data_used_pct"] == 10.0      # first non-'/' disk
    assert snap["system_power_w"] == 800
    assert snap["llm_ttft_ms"] == 120.0
    assert "llm_tpot_ms" not in snap               # None -> key omitted
    assert "llm_prefix_hit_pct" not in snap


# ---------- probe caching windows (60s ok / 15s fail) ----------
def test_prober_caches_success_60s_failure_15s(clock: Any) -> None:
    calls = {"n": 0}

    def probe() -> bool:
        calls["n"] += 1
        return True
    p = Prober(probe)
    assert p.enabled() and p.enabled()
    clock.advance(59)
    assert p.enabled()
    assert calls["n"] == 1                         # one probe per minute
    clock.advance(2)
    assert p.enabled()
    assert calls["n"] == 2

    calls["n"] = 0

    def failing() -> bool:
        calls["n"] += 1
        raise OSError("ipmitool exploded")
    pf = Prober(failing)
    assert not pf.enabled()
    clock.advance(14)
    assert not pf.enabled()
    assert calls["n"] == 1                         # failures retried sooner
    clock.advance(2)
    assert not pf.enabled()
    assert calls["n"] == 2
