"""LLM plugin tests — detection, metrics, usage accumulator, honest-None.

Everything external is monkeypatched (urllib, subprocess, /proc): the tests
never touch the network, docker, or a real inference server.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from daemon.config import ConsoleConfig
from daemon.plugins import llm

GOLDEN_LLM_KEYS = {
    "last_gen_tps", "last_prompt_tps", "kv_cache_fill_pct",
    "kv_cache_used_tokens", "kv_cache_max_tokens", "queue_depth",
    "queue_capacity", "server_uptime_s", "model", "llm_port",
    "llm_container", "backend", "running", "waiting", "ttft_ms", "tpot_ms",
    "e2e_ms", "queue_ms", "prefix_hit_pct", "num_preemptions",
    "spec_accept_pct", "total_requests", "error_count_5m", "last_error",
    "latency_p50_ms", "latency_p95_ms", "latency_p99_ms", "gen_total_24h",
    "prompt_total_24h", "gen_total_30d", "prompt_total_30d",
    "prompt_cached_total_24h", "prompt_cached_total_30d",
    "prompt_cached_pct_24h",
}


@pytest.fixture(autouse=True)
def _clean_llm_state() -> Any:
    llm.reset_state()
    yield
    llm.reset_state()


def _dead_subprocess(argv: list[str], timeout: float):
    raise FileNotFoundError(f"{argv[0]} absent")  # no docker/ss/ps/systemctl


def _seed_vllm(port: int = 8003, container: str | None = "vllm1") -> None:
    llm._llm_port_cache.update(port=port, container=container, backend="vllm",
                               pid=None, log=None, ts=time.time())


def _counters_body(gen: int, prompt: int) -> str:
    lab = '{model_name="m"}'
    return (f"# help\nvllm:generation_tokens_total{lab} {gen}\n"
            f"vllm:prompt_tokens_total{lab} {prompt}\n")


# ---------- (b) honest-None fallback, no ghosts ----------
def test_backend_none_honest_fallback(tmp_path: Path, make_config: ConsoleConfig,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm, "run_argv", _dead_subprocess)
    monkeypatch.setattr(llm.urllib.request, "urlopen",
                        lambda url, timeout=2.0, **k: (_ for _ in ()).throw(
                            OSError("connection refused")))
    ctx: dict[str, Any] = {"paths": {"usage_stats": str(tmp_path / "usage.json")}}
    section = llm.LlmCollector().collect(make_config, ctx)
    assert set(section["llm"]) == GOLDEN_LLM_KEYS
    l = section["llm"]
    assert l["backend"] is None and l["llm_port"] is None
    assert l["llm_container"] is None
    # honest zeros / -1 sentinels / None — never fabricated numbers
    assert l["last_gen_tps"] == 0 and l["last_prompt_tps"] == 0
    assert l["kv_cache_fill_pct"] == -1 and l["queue_depth"] == -1
    assert l["total_requests"] == 0 and l["error_count_5m"] == 0
    assert l["last_error"] is None
    assert l["latency_p50_ms"] is None and l["ttft_ms"] is None
    assert l["prompt_cached_total_24h"] is None      # honest None, not 0
    assert l["gen_total_24h"] == 0 and l["server_uptime_s"] == 0
    # usage file still initialised + written (power accrual starts clean)
    assert Path(ctx["paths"]["usage_stats"]).exists()
    assert ctx["stats"]["usage"]["gen_total_24h"] == 0


# ---------- (c) vLLM counter DECREASE re-baseline ----------
def test_vllm_counter_decrease_rebaselines(make_config: ConsoleConfig,
                                           monkeypatch: pytest.MonkeyPatch,
                                           clock: Any) -> None:
    _seed_vllm()
    bodies = [_counters_body(1000, 2000)]

    def fake_get(url: str, timeout: float = 3.0) -> str | None:
        return bodies[-1]
    monkeypatch.setattr(llm, "_http_get", fake_get)

    assert llm._update_vllm_tps(make_config) == (0.0, 0.0)   # seeding poll
    bodies[0] = _counters_body(1100, 2200)
    clock.advance(2.0)
    gen, prompt = llm._update_vllm_tps(make_config)
    assert gen > 0 and prompt > gen                          # EMA ramped up

    bodies[0] = _counters_body(500, 900)                     # restart/reset
    clock.advance(1.0)
    assert llm._update_vllm_tps(make_config) == (0.0, 0.0)   # re-baselined
    st = llm._vllm_tps_state
    assert st["gen"] == 500 and st["ema_gen"] == 0.0

    bodies[0] = _counters_body(550, 950)                     # grow from new base
    clock.advance(1.0)
    gen, _ = llm._update_vllm_tps(make_config)
    assert gen > 0                                           # no negative ghost


def test_windowed_ring_clears_on_counter_decrease(
        clock: Any) -> None:
    lab = {"prefix_cache_hits_total": 10.0, "prefix_cache_queries_total": 20.0,
           "time_to_first_token_seconds_sum": 5.0,
           "time_to_first_token_seconds_count": 10.0}
    up = {**lab, "prefix_cache_hits_total": 40.0,
          "prefix_cache_queries_total": 60.0,
          "time_to_first_token_seconds_sum": 9.0,
          "time_to_first_token_seconds_count": 18.0}
    llm._vllm_win["ring"].append((clock.t, dict(lab)))
    clock.advance(30)
    llm._vllm_win["ring"].append((clock.t, dict(up)))
    win = llm._vllm_windowed()
    assert win is not None
    assert win["prefix_hit_pct"] == 75.0                     # 30/40
    assert win["ttft_ms"] == 500.0                           # 4/8 s -> 500 ms

    # newest below oldest -> reset: ring collapses to 1, keeps last values
    down = {k: v * 0.1 for k, v in up.items()}
    clock.advance(30)
    llm._vllm_win["ring"].append((clock.t, dict(down)))
    win2 = llm._vllm_windowed()
    assert len(llm._vllm_win["ring"]) == 1
    assert win2 == win                                       # no flicker to None


# ---------- request stats: sum over reasons, error ring, carry-forward ------
def _req_body(stop: int, err: int) -> str:
    return ("vllm:request_success_total{finished_reason=\"stop\"} %d\n"
            "vllm:request_success_total{finished_reason=\"length\"} 3\n"
            "vllm:request_success_total{finished_reason=\"error\"} %d\n"
            % (stop, err))


def test_request_stats_totals_error_ring_and_carry_forward(
        make_config: ConsoleConfig, monkeypatch: pytest.MonkeyPatch,
        clock: Any) -> None:
    _seed_vllm()
    bodies: list[str | None] = [_req_body(10, 2)]
    monkeypatch.setattr(llm, "_metrics_body",
                        lambda cfg: bodies[-1])
    monkeypatch.setattr(llm, "run_argv", _dead_subprocess)  # no docker logs

    r = llm._get_vllm_request_stats(make_config)
    assert r["total_requests"] == 15          # 10 stop + 3 length + 2 error
    assert r["error_count_5m"] == 0           # first sample, no window yet
    assert r["last_error"] is None            # container unreachable -> honest

    clock.advance(10)
    bodies[0] = _req_body(18, 5)
    r = llm._get_vllm_request_stats(make_config)
    assert r["total_requests"] == 26          # 18 stop + 3 length + 5 error
    assert r["error_count_5m"] == 3           # 5-2 over the window

    bodies[0] = None                          # /metrics down -> carry
    r = llm._get_vllm_request_stats(make_config)
    assert r["total_requests"] == 26
    assert r["error_count_5m"] == 3


def test_last_error_via_docker_logs_only_when_container_known(
        make_config: ConsoleConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_vllm()
    monkeypatch.setattr(llm, "_metrics_body", lambda cfg: _req_body(1, 0))
    import subprocess as sp

    def fake_docker(argv: list[str], timeout: float) -> sp.CompletedProcess[str]:
        assert argv[:2] == ["docker", "logs"]
        return sp.CompletedProcess(argv, 0, "serving\n", "EngineCore ERROR bad\n")
    monkeypatch.setattr(llm, "run_argv", fake_docker)
    r = llm._get_vllm_request_stats(make_config)
    assert r["last_error"] == "EngineCore ERROR bad"


# ---------- (d) llama task-id reset layer 1 ----------
def _entry(task_id: int, gen: int = 100, prompt: int = 50) -> dict[str, Any]:
    return {"task_id": task_id, "gen_tokens": gen, "prompt_tokens": prompt,
            "gen_tps": 20.0, "prompt_tps": 40.0, "total_ms": 1200.0}


def test_task_id_reset_layer1_recounts_after_restart(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: Any) -> None:
    path = tmp_path / "usage-stats.json"
    usage = llm._ensure_usage_state(path)
    usage["last_seen_task_id"] = 20000
    llm._usage["last_sync"] = clock.t

    llm.update_usage_data([_entry(5000)], 0.0)   # 5000 << 20000 but > 1000

    bucket = usage["hourly"][-1]
    assert bucket["gen"] == 100 and bucket["prompt"] == 50  # counted, not ghost
    assert usage["last_seen_task_id"] == 5000
    assert json.loads(path.read_text())["last_seen_task_id"] == 5000


def test_task_id_below_threshold_is_not_a_reset(
        tmp_path: Path, clock: Any) -> None:
    path = tmp_path / "usage-stats.json"
    usage = llm._ensure_usage_state(path)
    usage["last_seen_task_id"] = 20000
    llm._usage["last_sync"] = clock.t

    llm.update_usage_data([_entry(500)], 0.0)    # small id, tiny -> not restart

    assert usage["hourly"] == [] or usage["hourly"][-1].get("gen", 0) == 0
    assert usage["last_seen_task_id"] == 20000   # state untouched


def test_power_kwh_uses_real_dt_capped(tmp_path: Path, clock: Any) -> None:
    path = tmp_path / "usage-stats.json"
    usage = llm._ensure_usage_state(path)
    llm.update_usage_data([], 1000.0)            # seeds last_power_ts
    clock.advance(600.0)                         # gap beyond the 120s cap
    llm.update_usage_data([], 1000.0)
    bucket = usage["hourly"][-1]
    # 1000 W * 120 s cap / 1000 / 3600 = 0.0333... kWh, never 600s-worth
    assert bucket["pwr"] == pytest.approx(1000 * 120 / 1000 / 3600, abs=1e-6)


# ---------- port candidates: docker-first + blacklist + config ----------
def test_candidate_ports_docker_first_blacklist_config(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from conftest import _make_config
    cfg = _make_config(tmp_path, non_llm_ports=(8002,))
    monkeypatch.setattr(llm, "_docker_host_ports",
                        lambda: {8003: "vllm1", 9000: "grafana"})
    monkeypatch.setattr(llm, "_localhost_listener_ports",
                        lambda: [8003, 8001, 9000, 22, 8002, 40000])
    assert llm._candidate_llm_ports(cfg) == [(8003, "vllm1"), (8001, None)]
    # 9000/22 base-blacklisted, 8002 config-blacklisted, 40000 ephemeral


# ---------- backend signature probing ----------
def test_probe_backend_signatures(monkeypatch: pytest.MonkeyPatch) -> None:
    def router(url: str, timeout: float = 3.0) -> str | None:
        if url.endswith("/metrics"):
            return "vllm:num_requests_running{model_name=\"m\"} 1\n"
        return "<html>Open WebUI</html>"
    monkeypatch.setattr(llm, "_http_get", router)
    monkeypatch.setattr(llm, "fetch_json",
                        lambda url, timeout=2.0: None)
    assert llm._probe_backend_on_port(8003) == "vllm"

    def slots(url: str, timeout: float = 3.0) -> str | None:
        return "not json"
    monkeypatch.setattr(llm, "_http_get", slots)   # /metrics no vllm: lines
    monkeypatch.setattr(llm, "fetch_json",
                        lambda url, timeout=2.0: [{"id": 0}]
                        if url.endswith("/slots") else None)
    assert llm._probe_backend_on_port(8080) == "llamacpp"

    monkeypatch.setattr(llm, "_http_get", lambda url, timeout=3.0: "<html/>")
    monkeypatch.setattr(llm, "fetch_json",
                        lambda url, timeout=2.0: {"openapi": "3"})
    assert llm._probe_backend_on_port(3000) is None  # decoy by signature


def test_detect_llm_port_caches_success_and_failure(
        make_config: ConsoleConfig, monkeypatch: pytest.MonkeyPatch,
        clock: Any) -> None:
    calls: list[int] = []

    def fake_probe(port: int, timeout: float = 1.5) -> str | None:
        calls.append(port)
        return "vllm" if port == 8003 else None
    monkeypatch.setattr(llm, "_probe_backend_on_port", fake_probe)
    monkeypatch.setattr(llm, "_candidate_llm_ports",
                        lambda cfg: [(8003, "vllm1")])
    monkeypatch.setattr(llm, "_discover_llama_server",
                        lambda: (None, None, None))

    assert llm._detect_llm_port(make_config) == (8003, "vllm1", "vllm", None)
    clock.advance(30)
    llm._detect_llm_port(make_config)
    assert len(calls) == 1                       # cached within 60s
    clock.advance(31)
    llm._detect_llm_port(make_config)
    assert len(calls) == 2                       # refreshed after TTL
