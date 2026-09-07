"""Console daemon harness: collector orchestration, history, atomic writes.

Runs every enabled+probed plugin each poll (fixed order: system → nvidia →
ipmi → llm, so llm can read gpus/power from ctx), merges their health.json
fragments, stamps `updated`/`updated_iso` (source format: naive local
"%Y-%m-%d %H:%M:%S"), rolls the history snapshot file (reloaded from disk
every poll, like the source, so a manual file trim is respected), and writes
health.json / health.history.json / stats.json / usage-stats.json atomically
into data_dir (DESIGN §0: the bundle's own data dir, never /var/www).
"""
from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from daemon.config import ConsoleConfig, load as load_config
from daemon.plugins import ipmi as ipmi_mod
from daemon.plugins import llm as llm_mod
from daemon.plugins import nvidia as nvidia_mod
from daemon.plugins import system as system_mod
from daemon.plugins.base import Collector, Prober, write_json_atomic

HEALTH_FILE = "health.json"
HISTORY_FILE = "health.history.json"
USAGE_FILE = "usage-stats.json"
STATS_FILE = "stats.json"
PID_FILE = "console-daemon.pid"


def build_collectors() -> list[Collector]:
    """Ordered plugin roster; order is contractual (ipmi reads ctx['gpus'],
    llm reads ctx['gpus'] + ctx['power'])."""
    return [system_mod.SystemCollector(), nvidia_mod.NvidiaCollector(),
            ipmi_mod.IpmiCollector(), llm_mod.LlmCollector()]


class Harness:
    """One run_once() == one poll cycle of the source's main()."""

    def __init__(self, cfg: ConsoleConfig,
                 collectors: list[Collector] | None = None) -> None:
        self.cfg = cfg
        self.collectors = (build_collectors() if collectors is None
                           else collectors)
        self._probers = {c.name: Prober(lambda c=c: c.probe(cfg))
                         for c in self.collectors}
        self.paths = {
            "data_dir": cfg.data_dir,
            "health": cfg.data_dir / HEALTH_FILE,
            "history": cfg.data_dir / HISTORY_FILE,
            "usage_stats": cfg.data_dir / USAGE_FILE,
            "stats": cfg.data_dir / STATS_FILE,
        }

    def _enabled(self, name: str) -> bool:
        if name == "system":
            return self.cfg.plugins.system.enabled
        if name == "nvidia":
            return self.cfg.plugins.nvidia.auto
        if name == "ipmi":
            return self.cfg.plugins.ipmi.auto
        if name == "llm":
            return self.cfg.plugins.llm.auto
        return True

    def run_once(self) -> dict[str, Any]:
        """Collect all sections, merge into the health dict (DESIGN §3 key
        contract; undetected sections stay absent)."""
        health: dict[str, Any] = {}
        ctx: dict[str, Any] = {"config": self.cfg, "paths": self.paths}
        for col in self.collectors:
            if not self._enabled(col.name) or not self._probers[col.name].enabled():
                continue
            try:
                section = col.collect(self.cfg, ctx)
            except Exception as exc:  # one broken sensor must not kill the poll
                print(f"{col.name} collect failed: {exc}", file=sys.stderr)
                continue
            health.update(section)
            ctx.update(section)  # gpus/power visible to later plugins

        # cross-plugin merge: energy aggregates from the usage accumulator
        # land inside the ipmi-owned power section (golden schema).
        if "power" in health:
            agg = ctx.get("usage_aggregates") or {}
            health["power"]["consumption_24h_kwh"] = agg.get(
                "consumption_24h_kwh", 0.0)
            health["power"]["consumption_30d_kwh"] = agg.get(
                "consumption_30d_kwh", 0.0)

        now = time.time()
        health["updated"] = now
        health["updated_iso"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                              time.localtime(now))

        stats: dict[str, Any] = ctx.get("stats") or {}
        self._write_artifacts(health, stats)
        return health

    def _write_artifacts(self, health: dict[str, Any],
                         stats: dict[str, Any]) -> None:
        if stats:
            try:
                write_json_atomic(self.paths["stats"], stats)
            except OSError as exc:
                print(f"stats write failed: {exc}", file=sys.stderr)
        try:
            write_json_atomic(self.paths["health"], health)
        except OSError as exc:
            print(f"health write failed: {exc}", file=sys.stderr)
        try:
            self._append_history(health, stats)
        except OSError as exc:
            print(f"history append failed: {exc}", file=sys.stderr)

    def _load_history(self) -> dict[str, list[Any]]:
        """Reload from disk every poll (source parity: external trims stick)."""
        import json
        try:
            history = json.loads(
                Path(self.paths["history"]).read_text(encoding="utf-8"))
            if not isinstance(history, dict) or "samples" not in history:
                return {"samples": []}
            return history
        except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError):
            return {"samples": []}

    def _append_history(self, health: dict[str, Any],
                        stats: dict[str, Any]) -> None:
        """Rolling snapshot list capped at history.max_samples
        (port of source append_history_snapshot, lines 1511-1576)."""
        history = self._load_history()
        snapshot = build_snapshot(health, stats)
        history["samples"].append(snapshot)
        cap = self.cfg.history.max_samples
        if len(history["samples"]) > cap:
            history["samples"] = history["samples"][-cap:]
        write_json_atomic(self.paths["history"], history)


def build_snapshot(health: dict[str, Any],
                   stats: dict[str, Any]) -> dict[str, Any]:
    """One history sample: load/ram/disk + dynamic per-GPU / per-fan keys +
    llm scalars (source:1530-1562). Cards carry identical names now, so GPU
    fields are looked up by list position (= nvidia-smi index)."""
    gpus = health.get("gpus") or []
    fans = health.get("fans") or []
    disks = health.get("disks") or []

    def disk_pct(want_root: bool) -> float:
        for d in disks:
            is_root = d.get("path") == "/"
            if is_root == want_root:
                return d.get("used_pct", 0)
        return 0

    snap: dict[str, Any] = {
        "ts": time.time(),
        "iso": time.strftime("%H:%M:%S"),
        "cpu_load_1m": health.get("load_1m", 0),
        "cpu_load_5m": health.get("load_5m", 0),
        "cpu_load_15m": health.get("load_15m", 0),
        "ram_used_pct": health.get("mem_used_pct", 0),
        "swap_used_pct": health.get("swap_used_pct", 0),
        # DEVIATION: source hardcoded the data-disk path; portable bundle
        # picks the first non-'/' disk (key kept for frontend parity).
        "disk_os_used_pct": disk_pct(True),
        "disk_data_used_pct": disk_pct(False),
        **{f"gpu{i}_{field}": (gpus[i].get(col) if i < len(gpus) else None)
           for i in range(len(gpus))
           for field, col in (("util", "use_pct"),
                              ("temp_junction", "temp_junction_c"),
                              ("temp_core", "temp_edge_c"),
                              ("vram_pct", "vram_alloc_pct"),
                              ("power", "power_w"))},
        **{f"fan{i}_rpm": (fans[i]["rpm"] if i < len(fans) else None)
           for i in range(len(fans))},
        "llm_gen_tps": stats.get("last_gen_tps", 0),
        "llm_prompt_tps": stats.get("last_prompt_tps", 0),
        "llm_kv_fill_pct": stats.get("kv_cache_fill_pct", 0),
        "llm_queue_depth": stats.get("queue_depth", 0),
        "llm_uptime_s": stats.get("server_uptime_s", 0),
        # dynamic latency/queue keys only when the backend reports them
        **{f"llm_{k}": stats.get(k)
           for k in ("ttft_ms", "tpot_ms", "prefix_hit_pct", "waiting",
                     "prompt_cached_pct")
           if stats.get(k) is not None},
        "system_power_w": (health.get("power") or {}).get("current_w"),
        "consumption_24h_kwh": (health.get("power") or {}).get(
            "consumption_24h_kwh"),
        "consumption_30d_kwh": (health.get("power") or {}).get(
            "consumption_30d_kwh"),
    }
    return snap


_running = True


def _handle_shutdown(signum: int, frame: Any) -> None:
    del signum, frame
    global _running
    _running = False


def _write_pid_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()}\n", encoding="utf-8")


def run_forever(cfg: ConsoleConfig) -> int:
    """Signal-handled poll loop; PID file inside data_dir (DESIGN §0: never
    /run)."""
    global _running
    _running = True
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)
    _write_pid_file(cfg.data_dir / PID_FILE)
    harness = Harness(cfg)
    try:
        while _running:
            started = time.time()
            harness.run_once()
            elapsed = time.time() - started
            delay = max(0.0, cfg.poll_interval_s - elapsed)
            if delay:
                time.sleep(delay)
    finally:
        try:
            (cfg.data_dir / PID_FILE).unlink(missing_ok=True)
        except OSError:
            pass
    return 0


def run_once_cli(config_path: str | Path) -> int:
    """One-shot mode: collect + write artifacts, print health path."""
    cfg = load_config(config_path)
    health = Harness(cfg).run_once()
    print(f"health written: {cfg.data_dir / HEALTH_FILE} "
          f"(sections: {sorted(health.keys())})")
    return 0
