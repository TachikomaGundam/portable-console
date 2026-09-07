"""LLM backend auto-detection + metrics collector (plugin port of
parse-stats.py lines 8-1608 + the main()-loop stats build, 1611-1751).

Faithful refactor of the working source: same function-level behavior for
port detection (docker-first candidate ports, backend signature probing,
bare llama-server discovery), vLLM Prometheus parsing (5-minute windowed
deltas, EMA throughput, error ring, usage accumulator with persisted
vllm_base baseline), llama.cpp slot/log parsing (task-id reset layers,
idle-ghost prevention), and the honest-None fallback semantics documented on
the wiki llm-monitor page. When DESIGN.md and source conflict, DESIGN wins.

Deviations from source (each required by DESIGN §0 / the portability brief):
- NO hardcoded log path: the source's ``LOG`` default and the
  ``LLAMA_SERVER_LOGS`` candidate tuple are gone. A llama-server's live log
  is resolved ONLY via /proc/<pid>/fd/{1,2} readlinks (source:32-45 minus
  the fallback list). Unresolvable log => log-derived metrics stay empty
  (honest zeros), never a stale file.
- NO hardcoded ports/container/model names beyond the documented
  ``_BASE_NON_LLM_PORTS`` blacklist (plus config additions).
- ``sudo docker logs`` (source:636) became plain ``docker logs``: the bundle
  is root-free (DESIGN §0) and sudo would prompt/hang. If docker needs no
  root here it works; otherwise last_error stays None (honest).
- stats.json request history carries the last 60 entries (brief) instead of
  the source's 20; averages still use the newest 20 (source semantics).
- ``server_uptime_s`` / docker-StartedAt ISO parsing tolerates nanosecond
  fractions + 'Z' (docker emits both) instead of bare fromisoformat.
"""
from __future__ import annotations

import collections
import datetime
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from portable_console.daemon.plugins.base import run_argv, write_json_atomic

if False:  # pragma: no cover - type-only
    from portable_console.daemon.config import ConsoleConfig

# ---------- constants (source-parity) ----------
TAIL_LINES = 1500           # llama log history for error/progress detection
LLM_MODEL_CACHE_TTL = 60.0  # seconds between /v1/models fetches
SERVICE = "llama-server.service"
LLM_PORT_REFRESH = 60.0     # re-probe LLM location at most once a minute
LLM_FAIL_REFRESH = 30.0     # failure cached shorter
LLAMA_DISCOVER_REFRESH = 60.0
LLM_PORT_CANDIDATES_MAX = 12
_VLLM_TPS_TAU = 5.0         # EMA time constant (s)
_VLLM_TPS_MAX_GAP = 30.0    # re-baseline poll-gap cap (s)
_VLLM_WIN_SECONDS = 300.0   # windowed-delta ring span
TASK_ID_RESET_THRESHOLD = 1000
STALE_SECONDS = 3600
POWER_DT_CAP = 120.0        # kWh integration gap cap (s)
_ERR_WINDOW_S = 300.0
_DOCKER_LOGS_SINCE = "90s"

# Ports that are definitively NOT an LLM API on any standard box (SSH/PG/
# camera/Auth/wiki/Kafka...). 8000 (Open WebUI) deliberately absent: it is
# probed and rejected by signature, never excluded defensively (source:102).
# Machine-specific ports belong in plugins.llm.non_llm_ports, not here.
_BASE_NON_LLM_PORTS = frozenset({
    22, 53, 80, 389, 443, 631, 636, 666, 2019, 3000, 3001, 3306, 3389, 5000,
    5432, 6379, 8080, 8554, 8555, 9000, 9092, 9443,
})

PROM_NUM = r"([\d.eE+-]+)"           # 0.0 / 2.5e-05 / +Inf
_PROM_NONNUM = ("+Inf", "-Inf", "Inf", "NaN")
ERROR_RE = re.compile(
    r"\b(?:error|failed|failure|panic|exception|fatal|segfault|abort)\b",
    re.IGNORECASE,
)
TS_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)\.(\d+)")
_VLLM_WIN_NAMES = (
    "prefix_cache_hits_total", "prefix_cache_queries_total",
    "time_to_first_token_seconds_sum", "time_to_first_token_seconds_count",
    "inter_token_latency_seconds_sum", "inter_token_latency_seconds_count",
    "e2e_request_latency_seconds_sum", "e2e_request_latency_seconds_count",
    "request_queue_time_seconds_sum", "request_queue_time_seconds_count",
)

# ---------- mutable module state (per-process caches) ----------
# `container` holds the docker name (vLLM), `pid` the bare llama-server pid,
# `log` its resolved live log path. Single source of truth for the API loc.
_llm_port_cache: dict[str, Any] = {
    "port": None, "ts": 0.0, "container": None,
    "backend": None, "pid": None, "log": None,
}
_llama_server_cache: dict[str, Any] = {
    "pid": None, "log": None, "port": None, "ts": 0.0,
}
_vllm_tps_state: dict[str, Any] = {
    "ts": None, "gen": None, "prompt": None, "ema_gen": 0.0, "ema_prompt": 0.0,
}
_vllm_win: dict[str, Any] = {
    "ring": collections.deque(maxlen=128), "last": None,
}
_max_seqs_state: dict[str, Any] = {"container": None, "value": None, "ts": 0.0}
_vllm_model_cache: dict[str, Any] = {"name": None, "ts": 0.0}
_last_vllm_total_requests: int | None = None
_vllm_err_ring: list[tuple[float, int]] = []  # [(ts, err_counter)] oldest first
_last_kv_fill: float = 0.0

# usage accumulator state; loaded from data_dir/usage-stats.json on first poll
_usage: dict[str, Any] = {
    "data": None,            # dict-shaped like the source's UsageData TypedDict
    "path": None,            # Path of usage-stats.json
    "last_sync": 0.0,        # last time new tokens were counted
    "last_power_ts": None,   # last poll wall-clock for kWh integration
    "base": {"gen": None, "prompt": None, "cached": None, "synced_hour": None},
}


def reset_state() -> None:
    """Drop every in-process cache (test / config-change hook)."""
    _llm_port_cache.update(port=None, ts=0.0, container=None,
                           backend=None, pid=None, log=None)
    _llama_server_cache.update(pid=None, log=None, port=None, ts=0.0)
    _vllm_tps_state.update(ts=None, gen=None, prompt=None,
                           ema_gen=0.0, ema_prompt=0.0)
    _vllm_win["ring"].clear()
    _vllm_win["last"] = None
    _max_seqs_state.update(container=None, value=None, ts=0.0)
    _vllm_model_cache.update(name=None, ts=0.0)
    global _last_vllm_total_requests, _vllm_err_ring, _last_kv_fill
    _last_vllm_total_requests = None
    _vllm_err_ring = []
    _last_kv_fill = 0.0
    _usage.update(data=None, path=None, last_sync=0.0, last_power_ts=None,
                  base={"gen": None, "prompt": None, "cached": None,
                        "synced_hour": None})


# ---------- small helpers ----------
def fetch_json(url: str, timeout: float = 2.0) -> Any:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def _http_get(url: str, timeout: float = 3.0) -> str | None:
    """GET text body, None on any failure (refused / timeout / non-HTTP)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read(1 << 20).decode("utf-8", "replace")
    except Exception:
        return None


# ---------- Prometheus text parsing (source:329-393) ----------
def _prom_gauge(body: str | None, name: str) -> float | None:
    if not body:
        return None
    m = re.search(
        r"^vllm:%s\{.*?\} %s$" % (re.escape(name), PROM_NUM), body, re.MULTILINE
    )
    if not m:
        return None
    raw = m.group(1)
    if raw in _PROM_NONNUM:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _prom_int(body: str | None, name: str) -> int | None:
    v = _prom_gauge(body, name)
    return int(v) if v is not None else None


def _prom_info_label(body: str | None, name: str, label: str) -> int | None:
    if not body:
        return None
    m = re.search(
        r"^vllm:%s\{(.*?)\} %s$" % (re.escape(name), PROM_NUM), body, re.MULTILINE
    )
    if not m:
        return None
    lm = re.search(r'%s="(\d+)"' % re.escape(label), m.group(1))
    if not lm:
        return None
    try:
        return int(lm.group(1))
    except ValueError:
        return None


def _prom_latency_ms(body: str | None, name: str) -> float | None:
    s = _prom_gauge(body, name + "_sum")
    c = _prom_gauge(body, name + "_count")
    if s is None or c is None:
        return None
    if c == 0:
        return 0.0
    return round(s / c * 1000, 1)


def _prom_ratio_pct(body: str | None, num_name: str,
                    den_names: tuple[str, ...]) -> float | None:
    num = _prom_gauge(body, num_name)
    if num is None:
        return None
    denom = 0.0
    for dn in den_names:
        d = _prom_gauge(body, dn)
        if d:
            denom += d
    if not denom:
        return 0.0
    return round(num / denom * 100, 1)


# ---------- bare llama-server discovery (source:32-100) ----------
def _resolve_llama_log(pid: int) -> str | None:
    """Live log path of a llama-server: its fd/1 / fd/2 targets (regular
    files only). DEVIATION: the source additionally probed a hardcoded
    LLAMA_SERVER_LOGS candidate list; a portable bundle must not guess other
    machines' deployment paths, so an unresolvable log stays None (honest)."""
    for scope in ("/proc/%d/fd/1" % pid, "/proc/%d/fd/2" % pid):
        try:
            target = os.readlink(scope)
            if os.path.isfile(target):
                return target
        except OSError:
            pass  # permission (daemon not root) or process gone
    return None


def _discover_llama_server() -> tuple[int | None, int | None, str | None]:
    """Find the running bare-process llama-server -> (port, pid, log_path).

    Scans `ps` for argv[0] basenames containing 'llama-server', parses
    --port <N>/--port=<N> (probes 8000-8010 /slots when absent) and VERIFIES
    the port by /slots parsing as a JSON list (llama-server-specific; an Open
    WebUI decoy returns HTML and is rejected). Cached 60s on success, 30s on
    failure. Several matches: prefer the newest (highest pid).
    """
    now = time.time()
    if _llama_server_cache["pid"] is not None:
        if now - _llama_server_cache["ts"] < LLAMA_DISCOVER_REFRESH:
            port = _llama_server_cache["port"]
            pid = _llama_server_cache["pid"]
            log = _llama_server_cache["log"]
            return (int(port) if port is not None else None,
                    int(pid), str(log) if log is not None else None)
    elif _llama_server_cache["ts"] and now - _llama_server_cache["ts"] < 30:
        return None, None, None
    found: tuple[int | None, int | None, str | None] = (None, None, None)
    matches: list[tuple[int, str]] = []
    try:
        r = run_argv(["ps", "-eo", "pid=,args="], timeout=5)
        for line in r.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) < 2:
                continue
            pid_str, args = parts
            arg0 = args.split(None, 1)[0]
            if "llama-server" not in os.path.basename(arg0):
                continue
            try:
                matches.append((int(pid_str), args))
            except ValueError:
                continue
    except (OSError, subprocess.TimeoutExpired):
        return None, None, None
    verified: list[tuple[int, int]] = []
    for pid, args in matches:
        m = re.search(r"--port(?:\s+|=)(\d+)", args)
        port = int(m.group(1)) if m else None
        if port is None:  # no --port flag: probe the usual range
            for cand in range(8000, 8011):
                if isinstance(fetch_json("http://127.0.0.1:%d/slots" % cand,
                                         timeout=2), list):
                    port = cand
                    break
        if port is None:
            continue
        if isinstance(fetch_json("http://127.0.0.1:%d/slots" % port,
                                 timeout=3), list):
            verified.append((pid, port))
    if verified:
        pid, port = max(verified)
        found = (port, pid, _resolve_llama_log(pid))
    _llama_server_cache.update(port=found[0], pid=found[1], log=found[2], ts=now)
    return found


# ---------- LLM port / backend detection (source:111-327) ----------
def _docker_host_ports() -> dict[int, str]:
    """{host_port: container_name} of ALL running containers (no image
    filter — deployments rename freely)."""
    mapping: dict[int, str] = {}
    try:
        r = run_argv(["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"],
                     timeout=5)
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            cname, ports = parts[0], parts[1]
            for m in re.finditer(r"(?:0\.0\.0\.0|\[::\]|127\.0\.0\.1):(\d+)->",
                                 ports):
                mapping.setdefault(int(m.group(1)), cname)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return mapping


def _localhost_listener_ports() -> list[int]:
    """TCP ports listening on loopback/all interfaces (`ss -tln`)."""
    ports: list[int] = []
    try:
        r = run_argv(["ss", "-tln"], timeout=5)
        for line in r.stdout.splitlines()[1:]:
            m = re.search(r"(?:\*|0\.0\.0\.0|\[::\]|127\.0\.0\.1):(\d+)\s", line)
            if m:
                ports.append(int(m.group(1)))
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ports


def _candidate_llm_ports(cfg: "ConsoleConfig") -> list[tuple[int, str | None]]:
    """Deterministic probe candidates: docker-published ports FIRST (inference
    services live in containers; bare processes hold plain listeners), then
    remaining localhost listeners. Blacklisted + ephemeral (>=30000) dropped
    from BOTH groups; capped. Container name rides along for docker deps."""
    blacklist = _BASE_NON_LLM_PORTS | set(cfg.plugins.llm.non_llm_ports)
    docker_map = _docker_host_ports()
    docker_ports = sorted(p for p in docker_map
                          if p not in blacklist and p < 30000)
    listeners = sorted(set(_localhost_listener_ports()) - set(docker_map))
    listeners = [p for p in listeners if p not in blacklist and p < 30000]
    ordered = docker_ports + listeners
    return [(p, docker_map.get(p)) for p in ordered[:LLM_PORT_CANDIDATES_MAX]]


def _probe_backend_on_port(port: int,
                           timeout: float = 1.5) -> str | None:
    """"vllm" when /metrics shows vllm:-prefixed lines, "llamacpp" when
    /slots parses as a JSON list, else None (signature check is what rejects
    the Open WebUI decoy — never a port exclusion)."""
    base = "http://127.0.0.1:%d" % port
    body = _http_get(base + "/metrics", timeout=timeout)
    if body and re.search(r"^vllm:", body, re.MULTILINE):
        return "vllm"
    if isinstance(fetch_json(base + "/slots", timeout=timeout), list):
        return "llamacpp"
    return None


def _detect_llm_port(
    cfg: "ConsoleConfig",
) -> tuple[int | None, str | None, str | None, int | None]:
    """(host_port, container_name, backend, pid) — deployment-agnostic, no
    hardcoded ports/names. Port probing first, bare-process fallback;
    port-probed llama.cpp also fills pid/log for uptime + live log. Cached
    60s on success, 30s on total failure (source:188-230)."""
    now = time.time()
    if _llm_port_cache["port"] is not None:
        ttl = (LLM_PORT_REFRESH
               if (_llm_port_cache["container"] or _llm_port_cache["pid"])
               else 30.0)
        if now - _llm_port_cache["ts"] < ttl:
            return (_llm_port_cache["port"], _llm_port_cache["container"],
                    _llm_port_cache["backend"], _llm_port_cache["pid"])
    elif _llm_port_cache["ts"] and now - _llm_port_cache["ts"] < 30:
        return None, None, None, None
    port: int | None = None
    container: str | None = None
    backend: str | None = None
    pid: int | None = None
    log: str | None = None
    for cand, cname in _candidate_llm_ports(cfg):
        b = _probe_backend_on_port(cand)
        if b:
            port, container, backend = cand, cname, b
            break
    if port is None:  # no port signature matched: bare-process scan
        port, pid, log = _discover_llama_server()
        if port:
            backend = "llamacpp"
    elif backend == "llamacpp":  # port-probed llama.cpp: fill pid/log
        dport, dpid, dlog = _discover_llama_server()
        if dport == port:
            pid, log = dpid, dlog
    _llm_port_cache.update(port=port, container=container, backend=backend,
                           pid=pid, log=log, ts=now)
    return port, container, backend, pid


def _llm_base_url(cfg: "ConsoleConfig") -> str | None:
    """http://127.0.0.1:<detected port> or None when nothing was detected."""
    p = _detect_llm_port(cfg)[0]
    return "http://127.0.0.1:%d" % p if p else None


def _detect_llm_backend(cfg: "ConsoleConfig") -> str | None:
    """Backend on the DETECTED port: 'vllm' / 'llamacpp' / None. Cached via
    the shared _llm_port_cache (source:295-327)."""
    now = time.time()
    if _llm_port_cache["backend"] is not None:
        if now - _llm_port_cache["ts"] < LLM_PORT_REFRESH:
            return str(_llm_port_cache["backend"])
    elif _llm_port_cache["port"] is None and _llm_port_cache["ts"]:
        if now - _llm_port_cache["ts"] < 30:
            return None
    base = _llm_base_url(cfg)
    if not base:
        _llm_port_cache["backend"] = None
        _llm_port_cache["ts"] = now
        return None
    backend: str | None = None
    body = _http_get(base + "/metrics", timeout=3)
    if body and re.search(r"^vllm:", body, re.MULTILINE):
        backend = "vllm"
    if backend is None and isinstance(fetch_json(base + "/slots", timeout=3),
                                      list):
        backend = "llamacpp"
    _llm_port_cache["backend"] = backend
    _llm_port_cache["ts"] = now
    return backend


# ---------- vLLM live throughput (source:237-291) ----------
def _update_vllm_tps(cfg: "ConsoleConfig") -> tuple[float, float]:
    """EMA-smoothed (tau=5s) per-poll rates of the cumulative vLLM token
    counters. First poll seeds; counter DECREASE or gap > 30s re-baselines to
    0.0 (honest idle-zero); fetch failure returns the current EMA untouched."""
    st = _vllm_tps_state
    now = time.time()
    base = _llm_base_url(cfg)
    body = _http_get(base + "/metrics", timeout=3) if base else None
    if body is None:
        return round(float(st["ema_gen"]), 1), round(float(st["ema_prompt"]), 1)
    gen = _prom_int(body, "generation_tokens_total")
    prompt = _prom_int(body, "prompt_tokens_total")
    if gen is None or prompt is None:
        return round(float(st["ema_gen"]), 1), round(float(st["ema_prompt"]), 1)
    if st["ts"] is None:
        st.update(ts=now, gen=gen, prompt=prompt)
        return 0.0, 0.0
    dt = now - float(st["ts"])
    if gen < st["gen"] or prompt < st["prompt"] or dt > _VLLM_TPS_MAX_GAP:
        print(f"vllm tps counter reset/gap detected: gen {st['gen']}->{gen}, "
              f"prompt {st['prompt']}->{prompt} dt={dt:.1f}s → re-baselining",
              file=sys.stderr)
        st.update(ts=now, gen=gen, prompt=prompt, ema_gen=0.0, ema_prompt=0.0)
        return 0.0, 0.0
    rate_gen = max(0.0, (gen - float(st["gen"])) / dt)
    rate_prompt = max(0.0, (prompt - float(st["prompt"])) / dt)
    alpha = 1.0 - math.exp(-dt / _VLLM_TPS_TAU)
    st["ema_gen"] = alpha * rate_gen + (1.0 - alpha) * float(st["ema_gen"])
    st["ema_prompt"] = (alpha * rate_prompt
                        + (1.0 - alpha) * float(st["ema_prompt"]))
    st.update(ts=now, gen=gen, prompt=prompt)
    return round(float(st["ema_gen"]), 1), round(float(st["ema_prompt"]), 1)


# ---------- vLLM 5-minute windowed deltas (source:395-471) ----------
def _vllm_windowed() -> dict[str, Any] | None:
    """Δ-based prefix_hit_pct / ttft_ms / tpot_ms / e2e_ms / queue_ms over a
    sliding 5-min ring. Counter DECREASE clears the ring; idle windows keep
    the LAST computed values so the card never flickers; None until the first
    window exists (caller falls back to lifetime values)."""
    ring: collections.deque = _vllm_win["ring"]
    last: dict[str, Any] | None = _vllm_win["last"]
    if len(ring) < 2:
        return last
    newest_ts, newest = ring[-1]
    oldest: tuple[float, dict[str, float | None]] | None = None
    for i, (ts, _) in enumerate(ring):
        if ts >= newest_ts - _VLLM_WIN_SECONDS:
            oldest = ring[i]
            break
    if oldest is None:
        return last
    _, oldest_raw = oldest
    for name in _VLLM_WIN_NAMES:
        a, b = newest.get(name), oldest_raw.get(name)
        if a is not None and b is not None and a < b:
            ring.clear()
            ring.append((newest_ts, newest))
            return last

    def _delta(name: str) -> float | None:
        a, b = newest.get(name), oldest_raw.get(name)
        if a is None or b is None:
            return None
        return a - b

    winvals: dict[str, float] = {}
    dh = _delta("prefix_cache_hits_total")
    dq = _delta("prefix_cache_queries_total")
    if dh is not None and dq is not None and dq > 0:
        winvals["prefix_hit_pct"] = round(dh / dq * 100, 1)
    for name, key in (("time_to_first_token_seconds", "ttft_ms"),
                      ("inter_token_latency_seconds", "tpot_ms"),
                      ("e2e_request_latency_seconds", "e2e_ms"),
                      ("request_queue_time_seconds", "queue_ms")):
        ds = _delta(name + "_sum")
        dc = _delta(name + "_count")
        if ds is not None and dc is not None and dc > 0:
            winvals[key] = round(ds / dc * 1000, 1)
    if winvals:
        if last is None:
            last = {}
        last.update(winvals)
        _vllm_win["last"] = last
    return last


# ---------- vLLM queue capacity (source:473-507) ----------
def _vllm_queue_capacity(container: str | None) -> int | None:
    """--max-num-seqs from `docker inspect` .Config.Cmd, cached 60s per
    container name; None when absent/unparsable (no capacity claim)."""
    now = time.time()
    if (container and container == _max_seqs_state["container"]
            and now - float(_max_seqs_state["ts"]) < 60):
        value = _max_seqs_state["value"]
        return int(value) if value is not None else None
    if not container:
        _max_seqs_state.update(container=None, value=None, ts=now)
        return None
    value: int | None = None
    try:
        cp = run_argv(["docker", "inspect", "--format",
                       "{{json .Config.Cmd}}", container], timeout=5)
        cmd = json.loads(cp.stdout)
        for i, tok in enumerate(cmd):
            if tok == "--max-num-seqs" and i + 1 < len(cmd):
                value = int(cmd[i + 1])
                break
            if isinstance(tok, str) and tok.startswith("--max-num-seqs="):
                value = int(tok.split("=", 1)[1])
                break
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError,
            KeyError, IndexError):
        value = None  # graceful: no inspect info -> no capacity claim
    _max_seqs_state.update(container=container, value=value, ts=now)
    return value


# ---------- vLLM unified metrics (source:509-565) ----------
def _metrics_body(cfg: "ConsoleConfig") -> str | None:
    base = _llm_base_url(cfg)
    return _http_get(base + "/metrics", timeout=3) if base else None


def _get_vllm_metrics(cfg: "ConsoleConfig") -> dict[str, Any]:
    """Parse <base>/metrics into the unified LLM schema; every key present,
    None when the metric is absent (frontend renders '—'). Latency means and
    prefix hit rate are 5-min windowed deltas with lifetime fallback."""
    body = _metrics_body(cfg)

    fill = _prom_gauge(body, "kv_cache_usage_perc")     # 0..1 gauge
    size = _prom_info_label(body, "cache_config_info", "kv_cache_size_tokens")
    waiting = _prom_int(body, "num_requests_waiting")

    now = time.time()
    _vllm_win["ring"].append(
        (now, {name: _prom_gauge(body, name) for name in _VLLM_WIN_NAMES})
    )
    win = _vllm_windowed()

    def _winv(key: str, lifetime: float | None) -> float | None:
        if win and key in win:
            v: float = win[key]
            return v
        return lifetime

    prefix_n = _prom_gauge(body, "prefix_cache_hits_total")
    prefix_d = _prom_gauge(body, "prefix_cache_queries_total")
    # hits/queries (vLLM's own ratio), NOT hits/(hits+queries).
    prefix_life = (round(prefix_n / prefix_d * 100, 1)
                   if prefix_n is not None and prefix_d else 0.0)
    return {
        "kv_cache_fill_pct": round(fill * 100, 1) if fill is not None else None,
        "kv_cache_used_tokens": (int(round(fill * size))
                                 if fill is not None and size else None),
        "kv_cache_max_tokens": size,
        "queue_depth": waiting,
        "running": _prom_int(body, "num_requests_running"),
        "waiting": waiting,
        "queue_capacity": _vllm_queue_capacity(_detect_llm_port(cfg)[1]),
        "ttft_ms": _winv("ttft_ms",
                         _prom_latency_ms(body, "time_to_first_token_seconds")),
        "tpot_ms": _winv("tpot_ms",
                         _prom_latency_ms(body, "inter_token_latency_seconds")),
        "e2e_ms": _winv("e2e_ms",
                        _prom_latency_ms(body, "e2e_request_latency_seconds")),
        "queue_ms": _winv("queue_ms",
                          _prom_latency_ms(body, "request_queue_time_seconds")),
        "prefix_hit_pct": _winv("prefix_hit_pct", prefix_life),
        "num_preemptions": _prom_int(body, "num_preemptions_total"),
        "spec_accept_pct": _prom_ratio_pct(
            body, "spec_decode_num_accepted_tokens_total",
            ("spec_decode_num_drafts_total",
             "spec_decode_num_draft_tokens_total")),
        "slots": None,  # vLLM exposes no per-slot list
        "backend": "vllm",
    }


# ---------- vLLM request / error stats (source:567-645) ----------
def _get_vllm_request_stats(cfg: "ConsoleConfig") -> dict[str, Any]:
    """total_requests (sum over ALL finished_reason, carry-forward on fetch
    failure), error_count_5m (300s ring delta of the error counter),
    last_error (newest docker-logs ERROR_RE line, container-known only)."""
    global _last_vllm_total_requests, _vllm_err_ring
    body = _metrics_body(cfg)

    total: int | None = None
    err: int | None = None
    if body:
        reasons: dict[str, int] = {}
        for m in re.finditer(
            r'^vllm:request_success_total\{[^}]*finished_reason="([^"]+)"'
            r'[^}]*\} %s$' % PROM_NUM, body, re.MULTILINE,
        ):
            raw = m.group(2)
            if raw in _PROM_NONNUM:
                continue
            try:
                reasons.setdefault(m.group(1), int(float(raw)))
            except ValueError:
                continue
        if reasons:
            total = sum(reasons.values())
            _last_vllm_total_requests = total
            err = reasons.get("error")
    if total is None and _last_vllm_total_requests is not None:
        total = _last_vllm_total_requests  # /metrics down — carry last known

    now = time.time()
    if err is None and _vllm_err_ring:
        err = _vllm_err_ring[-1][1]  # reuse last known counter while down
    err_5m: int | None
    if err is not None:
        if _vllm_err_ring and err < _vllm_err_ring[-1][1]:
            _vllm_err_ring[:] = [(now, err)]  # counter reset -> restart window
        else:
            _vllm_err_ring.append((now, err))
        _vllm_err_ring[:] = [(ts, c) for ts, c in _vllm_err_ring
                             if now - ts <= _ERR_WINDOW_S]
        err_5m = (max(0, err - _vllm_err_ring[0][1])
                  if len(_vllm_err_ring) >= 2 else 0)
    else:
        err_5m = None

    last_error: str | None = None
    _, container, _, _ = _detect_llm_port(cfg)
    if container:
        try:
            cp = run_argv(["docker", "logs", "--since", _DOCKER_LOGS_SINCE,
                           container], timeout=5)
            text = ((cp.stdout or "") + (cp.stderr or ""))
            for line in text.splitlines():
                if ERROR_RE.search(line):
                    last_error = line.strip()
        except (OSError, subprocess.TimeoutExpired):
            last_error = None

    return {"total_requests": total, "error_count_5m": err_5m,
            "last_error": last_error}


# ---------- llama.cpp slots / log parsing (source:721-1070) ----------
def tail_log(n: int, path: str | None) -> list[str]:
    """Last n lines of the resolved llama-server log (None path -> [])."""
    if not path:
        return []
    try:
        r = run_argv(["tail", "-%d" % n, path], timeout=5)
        return r.stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired):
        return []


def parse_ts(line: str) -> float | None:
    """Log timestamp A.B.CCC.DDD -> monotonic seconds float."""
    m = TS_RE.match(line)
    if not m:
        return None
    a, b, c, d = m.groups()
    try:
        return float(f"{a}.{b}") + float(f"0.{c}") + float(f"0.000{d}")
    except ValueError:
        return None


def parse_entries(lines: list[str]) -> list[dict[str, Any]]:
    """Prompt/eval/total timing triplets grouped by task id (source:740-782)."""
    tasks: dict[str, dict[str, Any]] = {}
    for line in lines:
        task_match = re.search(r"id\s+\d+\s*\|\s*task\s+(\d+)", line)
        task_id = task_match.group(1) if task_match else None
        if task_id is None:
            continue
        data = tasks.setdefault(task_id, {})
        m_prompt = re.search(
            r"prompt eval time\s*=\s*([\d.]+)\s*ms\s*/\s*(\d+)\s*tokens"
            r"\s*\(\s*[\d.]+\s*ms per token\s*,\s*([\d.]+)\s*tokens per second",
            line,
        )
        m_eval = re.search(
            r"(?<!prompt )eval time\s*=\s*([\d.]+)\s*ms\s*/\s*(\d+)\s*tokens"
            r"\s*\(\s*[\d.]+\s*ms per token\s*,\s*([\d.]+)\s*tokens per second",
            line,
        )
        m_total = re.search(
            r"total time\s*=\s*([\d.]+)\s*ms\s*/\s*(\d+)\s*tokens", line
        )
        if m_prompt:
            data["prompt_ms"] = float(m_prompt.group(1))
            data["prompt_tokens"] = int(m_prompt.group(2))
            data["prompt_tps"] = float(m_prompt.group(3))
        if m_eval:
            data["gen_ms"] = float(m_eval.group(1))
            data["gen_tokens"] = int(m_eval.group(2))
            data["gen_tps"] = float(m_eval.group(3))
        if m_total:
            data["total_ms"] = float(m_total.group(1))
            data["total_tokens"] = int(m_total.group(2))

    entries = []
    for tid, data in sorted(tasks.items(), key=lambda x: int(x[0])):
        if data.get("gen_tokens") and data.get("total_ms"):
            data["task_id"] = int(tid)
            entries.append(data)
    return entries


def get_slots_metrics(cfg: "ConsoleConfig",
                      log_path: str | None) -> dict[str, Any]:
    """KV fill / queue / per-slot view from GET <base>/slots (source:901-953).

    fill is latched via _last_kv_fill: a 0 fill while queue depth is 0 keeps
    the last non-zero value (idle servers report 0 slots tokens transiently —
    the ghost-prevention latch from the wiki page)."""
    global _last_kv_fill
    base = _llm_base_url(cfg)
    slots = fetch_json(base + "/slots", timeout=5) if base else None
    if not isinstance(slots, list) or not slots:
        return _slots_fallback(log_path)

    capacity = len(slots)
    depth = 0
    used_tokens = 0
    first = slots[0] if isinstance(slots[0], dict) else {}
    max_tokens = int(first.get("n_ctx") or 65536)
    per_slot: list[dict[str, Any]] = []

    for i, s in enumerate(slots):
        if not isinstance(s, dict):
            continue
        is_proc = bool(s.get("is_processing"))
        if is_proc:
            depth += 1
        prompt_tokens = int(s.get("n_prompt_tokens") or 0)
        prompt_tokens_cache = int(s.get("n_prompt_tokens_cache") or 0)
        decoded = 0
        nt = s.get("next_token")
        if isinstance(nt, list) and nt and isinstance(nt[0], dict):
            decoded = int(nt[0].get("n_decoded") or 0)
        slot_used = max(prompt_tokens, prompt_tokens_cache) + decoded
        used_tokens += slot_used

        slot_max = int(s.get("n_ctx") or max_tokens)
        slot_pct = round(slot_used / slot_max, 4) if slot_max else 0
        per_slot.append({
            "id": int(s.get("id", i)),
            "state": "processing" if is_proc else "idle",
            "current_tokens": slot_used,
            "n_prompt_tokens_cache": prompt_tokens_cache,
            "max_tokens": slot_max,
            "percentage": slot_pct,
        })

    used_clamped = min(used_tokens, max_tokens)
    fill = round(used_clamped / max_tokens, 4) if max_tokens else 0
    if fill > 0:
        _last_kv_fill = fill
    elif depth == 0 and _last_kv_fill > 0:
        fill = _last_kv_fill
    return {
        "kv_cache_fill_pct": fill,
        "kv_cache_used_tokens": used_clamped,
        "kv_cache_max_tokens": max_tokens,
        "queue_depth": depth,
        "queue_capacity": capacity,
        "slots": per_slot,
        "running": sum(1 for s in per_slot if s["state"] == "processing"),
        "backend": "llamacpp",
    }


def _slots_fallback(log_path: str | None) -> dict[str, Any]:
    """n_ctx / n_slots scraped from the log when /slots is unreachable;
    -1 fill/used/depth keep the frontend's '—' rendering (source:955-979)."""
    lines = tail_log(2000, log_path)
    n_ctx = 65536
    n_slots = 4
    for line in reversed(lines):
        m = re.search(r"new slot[^|]*\|\s*task\s+-?\d+\s*\|\s*new slot,"
                      r"\s*n_ctx\s*=\s*(\d+)", line)
        if m:
            n_ctx = int(m.group(1))
            break
    for line in lines:
        m = re.search(r"initializing slots,\s*n_slots\s*=\s*(\d+)", line)
        if m:
            n_slots = int(m.group(1))
            break
    return {
        "kv_cache_fill_pct": -1,
        "kv_cache_used_tokens": -1,
        "kv_cache_max_tokens": n_ctx,
        "queue_depth": -1,
        "queue_capacity": n_slots,
        "slots": [],
        "running": 0,
        "backend": "llamacpp",
    }


def get_latency_percentiles(history: list[dict[str, Any]]) -> dict[str, Any]:
    """P50/P95/P99 (ms, rounded ints) from total_ms; None below 5 samples."""
    if not history or len(history) < 5:
        return {"latency_p50_ms": None, "latency_p95_ms": None,
                "latency_p99_ms": None}
    vals = sorted(float(e["total_ms"]) for e in history if e.get("total_ms"))
    if len(vals) < 5:
        return {"latency_p50_ms": None, "latency_p95_ms": None,
                "latency_p99_ms": None}

    def pct(p: float) -> float:
        k = (len(vals) - 1) * p
        f = int(k)
        c = f + 1
        if c >= len(vals):
            return vals[f]
        return vals[f] + (k - f) * (vals[c] - vals[f])

    return {
        "latency_p50_ms": int(round(pct(0.50))),
        "latency_p95_ms": int(round(pct(0.95))),
        "latency_p99_ms": int(round(pct(0.99))),
    }


def _parse_iso(value: str) -> Any:
    """ISO-8601 from docker StartedAt, tolerating 'Z' + >6-digit fractions
    (DEVIATION: source used bare fromisoformat, 3.11-only niceties)."""
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    v = re.sub(r"(\.\d{6})\d+", r"\1", v)  # nanoseconds -> microseconds
    return datetime.datetime.fromisoformat(v)


def get_server_uptime_s(cfg: "ConsoleConfig") -> int:
    """systemd llama-server.service ActiveEnterTimestamp, then the detected
    container's docker StartedAt, then ps etimes of the bare pid (source:1001)."""
    try:
        r = run_argv(["systemctl", "show", SERVICE,
                      "-p", "ActiveState,ActiveEnterTimestamp", "--value"],
                     timeout=3)
        out = r.stdout.strip().splitlines()
        if len(out) >= 2:
            state, ts_str = out[0], out[1]
            if state == "active" and ts_str:
                m = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", ts_str)
                if m:
                    local_dt = datetime.datetime.strptime(
                        m.group(1), "%Y-%m-%d %H:%M:%S")
                    return max(0, int((datetime.datetime.now()
                                       - local_dt).total_seconds()))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    _, container, _, pid = _detect_llm_port(cfg)
    if container:
        try:
            r = run_argv(["docker", "inspect", "--format",
                          "{{.State.StartedAt}}", container], timeout=5)
            started = r.stdout.strip()
            if started:
                started_dt = _parse_iso(started)
                if started_dt.tzinfo is not None:
                    return max(0, int((datetime.datetime.now(
                        datetime.timezone.utc) - started_dt).total_seconds()))
                return max(0, int((datetime.datetime.now()
                                   - started_dt).total_seconds()))
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
    if pid:
        try:
            r = run_argv(["ps", "-o", "etimes=", "-p", str(pid)], timeout=3)
            uptime = int(r.stdout.strip())
            if uptime > 0:
                return uptime
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
    return 0


def get_model_loading_progress(cfg: "ConsoleConfig",
                               lines: list[str]) -> float:
    """0..1 during model load, 1.0 ready, -1 indeterminate (source:1049-1070)."""
    seen_loading = any("loading model" in line for line in lines)
    seen_loaded = any(("server is listening" in line or "model loaded" in line)
                      for line in lines)
    if seen_loaded:
        return 1.0
    if not seen_loading:
        base = _llm_base_url(cfg)
        h = fetch_json(base + "/health") if base else None
        if isinstance(h, dict) and h.get("status") == "ok":
            return 1.0
        return -1
    if any("initializing slots" in l for l in lines):
        return 0.9
    if any("warming up the model" in l for l in lines):
        return 0.7
    return 0.4


def get_recent_errors(lines: list[str]) -> tuple[int, str | None]:
    """ERROR_RE hits within 300 log-seconds of the newest line + last msg."""
    last_ts: float | None = None
    for line in reversed(lines):
        last_ts = parse_ts(line)
        if last_ts is not None:
            break
    if last_ts is None:
        return 0, None
    count = 0
    last_msg: str | None = None
    for line in lines:
        ts = parse_ts(line)
        if ts is None:
            continue
        if last_ts - ts > 300:
            continue
        if ERROR_RE.search(line):
            count += 1
            msg = line.strip()
            body = TS_RE.sub("", msg, count=1).strip()
            if body and body[0] in "EWI" and len(body) > 1 and body[1] == " ":
                body = body[2:].strip()
            last_msg = body or msg
    return count, last_msg


# ---------- usage accumulator (source:1099-1376) ----------
def _new_usage_data(now: float) -> dict[str, Any]:
    return {"hourly": [], "last_seen_task_id": 0, "started": int(now)}


def _load_usage_data(path: Path) -> dict[str, Any]:
    """Parse usage-stats.json defensively (source:1108-1139); corrupted or
    missing -> fresh state. vllm_base baseline survives daemon restarts."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("usage data must be an object")
        hourly_raw = raw.get("hourly", [])
        if not isinstance(hourly_raw, list):
            raise ValueError("usage hourly must be a list")
        hourly: list[dict[str, Any]] = []
        for item in hourly_raw:
            if not isinstance(item, dict):
                continue
            bucket = {
                "hour": int(item.get("hour", 0)),
                "gen": int(item.get("gen", 0)),
                "prompt": int(item.get("prompt", 0)),
                "pwr": float(item.get("pwr", 0.0)),
            }
            if item.get("cached") is not None:
                # HONEST: old buckets have no cached key at all
                bucket["cached"] = int(item["cached"])
            hourly.append(bucket)
        data: dict[str, Any] = {
            "hourly": hourly,
            "last_seen_task_id": int(raw.get("last_seen_task_id", 0)),
            "started": int(raw.get("started", int(time.time()))),
        }
        raw_base = raw.get("vllm_base")
        if isinstance(raw_base, dict):
            data["vllm_base"] = raw_base
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError,
            ValueError):
        return _new_usage_data(time.time())


def _ensure_usage_state(path: Path) -> dict[str, Any]:
    if _usage["data"] is None or _usage["path"] != path:
        _usage["data"] = _load_usage_data(path)
        _usage["path"] = path
        if _usage["last_sync"] == 0.0:
            _usage["last_sync"] = time.time()
    usage: dict[str, Any] = _usage["data"]
    return usage


def _current_hour(now: float) -> int:
    return int(now // 3600) * 3600


def _trim_usage_data(usage_data: dict[str, Any], now: float) -> None:
    cutoff = int(now - 31 * 86400)
    usage_data["hourly"] = [
        b for b in usage_data.get("hourly", [])
        if int(b.get("hour", 0)) >= cutoff
    ]


def _get_hour_bucket(usage_data: dict[str, Any],
                     hour: int) -> dict[str, Any]:
    for bucket in usage_data.get("hourly", []):
        if int(bucket.get("hour", 0)) == hour:
            return bucket
    bucket = {"hour": hour, "gen": 0, "prompt": 0, "pwr": 0.0}
    usage_data["hourly"].append(bucket)
    usage_data["hourly"].sort(key=lambda item: int(item.get("hour", 0)))
    return bucket


def _advance_cached_base(base: dict[str, Any],
                         cached: int | None) -> int | None:
    """Per-poll cached-token delta with HONEST-NONE + decrease re-baseline
    (source:1176-1195): None family absent -> bucket untouched; 0 first
    sighting/reset; else positive increment."""
    if cached is None:
        return None
    if base["cached"] is None:
        base["cached"] = cached
        return 0
    if cached < base["cached"]:
        print(f"vllm cached token counter reset detected: "
              f"cached {base['cached']}->{cached} → re-baselining",
              file=sys.stderr)
        base["cached"] = cached
        return 0
    delta = cached - base["cached"]
    base["cached"] = cached
    return delta


def _get_vllm_usage_delta(cfg: "ConsoleConfig") -> tuple[int, int, int | None]:
    """Per-poll (gen, prompt, cached) deltas of the cumulative vLLM counters,
    baselined in _usage['base'] and persisted under USAGE['vllm_base'] so a
    daemon restart resumes without double-count (source:1198-1259)."""
    base = _usage["base"]
    usage = _usage["data"] or {}
    now_hour = _current_hour(time.time())
    if base["gen"] is None:
        persisted = usage.get("vllm_base")
        if isinstance(persisted, dict) and persisted.get("gen_base") is not None:
            base["gen"] = int(persisted["gen_base"])
            base["prompt"] = int(persisted["prompt_base"])
            base["synced_hour"] = now_hour
            if persisted.get("cached_base") is not None:
                base["cached"] = int(persisted["cached_base"])
    body = _metrics_body(cfg)
    if body is None:
        return (0, 0, None)
    gen = _prom_int(body, "generation_tokens_total")
    prompt = _prom_int(body, "prompt_tokens_total")
    cached = _prom_int(body, "prompt_tokens_cached_total")
    if gen is None or prompt is None:
        return (0, 0, None)
    if base["gen"] is None:
        base.update({"gen": gen, "prompt": prompt, "synced_hour": now_hour})
        cached_delta = _advance_cached_base(base, cached)
        usage["vllm_base"] = {"gen_base": gen, "prompt_base": prompt,
                              "cached_base": base["cached"],
                              "base_hour": now_hour}
        return (0, 0, cached_delta)
    if gen < base["gen"] or prompt < base["prompt"]:
        print(f"vllm token counter reset detected: gen {base['gen']}->{gen}, "
              f"prompt {base['prompt']}->{prompt} → re-baselining",
              file=sys.stderr)
        base.update({"gen": gen, "prompt": prompt, "synced_hour": now_hour})
        usage["vllm_base"] = {"gen_base": gen, "prompt_base": prompt,
                              "base_hour": now_hour}
        if cached is not None:
            base["cached"] = cached  # full reset — re-seed cached too
            usage["vllm_base"]["cached_base"] = cached
        return (0, 0, 0 if cached is not None else None)
    delta = (gen - int(base["gen"]), prompt - int(base["prompt"]))
    cached_delta = _advance_cached_base(base, cached)
    base.update({"gen": gen, "prompt": prompt, "synced_hour": now_hour})
    usage["vllm_base"] = {"gen_base": gen, "prompt_base": prompt,
                          "cached_base": base["cached"], "base_hour": now_hour}
    return (delta[0], delta[1], cached_delta)


def update_usage_data(
    entries: list[dict[str, Any]], power_w: float,
    vllm_delta: tuple[int, int, int | None] | None = None,
) -> None:
    """Hourly token/power accumulator (source:1262-1338), incl. the two
    llama task-id reset-defense layers and the stale self-heal; writes
    usage-stats.json atomically every poll.

    power_w: whole-machine watts resolved by the caller (IPMI DCMI ->
    ipmi plugin, GPU sum fallback). Energy integrates REAL wall-clock dt
    between polls, capped at 120s so daemon downtime fabricates nothing."""
    usage = _usage["data"]
    path: Path | None = _usage["path"]
    if usage is None or path is None:
        return
    now = time.time()
    bucket = _get_hour_bucket(usage, _current_hour(now))
    last_seen_task_id = int(usage.get("last_seen_task_id", 0))

    if vllm_delta is not None:
        # vLLM: tokens accrue from cumulative counter deltas only; the llama
        # log is dead input here — stale-task self-heal must NOT re-count a
        # frozen batch into every hour (skip both llama layers entirely).
        bucket["gen"] = int(bucket.get("gen", 0)) + vllm_delta[0]
        bucket["prompt"] = int(bucket.get("prompt", 0)) + vllm_delta[1]
        if vllm_delta[2] is not None:
            bucket["cached"] = int(bucket.get("cached", 0)) + vllm_delta[2]
        _usage["last_sync"] = now
    elif entries:
        max_current = max(int(e.get("task_id", 0)) for e in entries)
        # --- reset-defense layer 1: counter reset / server restart ---
        # highest visible id far below last_seen yet non-trivial -> restart.
        if (max_current < last_seen_task_id
                and max_current > TASK_ID_RESET_THRESHOLD):
            print(f"task_id counter reset detected: max_current={max_current} "
                  f"< last_seen={last_seen_task_id} → resetting "
                  f"last_seen_task_id so entries start counting from ~0",
                  file=sys.stderr)
            last_seen_task_id = 0
            usage["last_seen_task_id"] = 0

    new_entries = [e for e in entries
                   if int(e.get("task_id", 0)) > last_seen_task_id]

    # --- reset-defense layer 2: stale-state self-heal ---
    if not new_entries and entries:
        max_current = max(int(e.get("task_id", 0)) for e in entries)
        if max_current > 0 and (now - float(_usage["last_sync"])) > STALE_SECONDS:
            print(f"stale usage detected: no new entries for "
                  f"{int(now - float(_usage['last_sync']))}s despite "
                  f"{len(entries)} parsed entries (max_id={max_current}) "
                  f"→ resetting last_seen_task_id from {last_seen_task_id} to 0",
                  file=sys.stderr)
            last_seen_task_id = 0
            usage["last_seen_task_id"] = 0
            new_entries = [e for e in entries if int(e.get("task_id", 0)) > 0]

    if new_entries:
        bucket["gen"] = int(bucket.get("gen", 0)) + sum(
            int(e.get("gen_tokens", 0)) for e in new_entries)
        bucket["prompt"] = int(bucket.get("prompt", 0)) + sum(
            int(e.get("prompt_tokens", 0)) for e in new_entries)
        usage["last_seen_task_id"] = max(
            int(e.get("task_id", 0)) for e in new_entries)
        _usage["last_sync"] = now

    # Energy: kWh = W * dt / 1000 / 3600 with real dt (2026-09-02 lesson:
    # the old fixed-1s assumption undercounted ~5.4x at ~5.4s poll cycles).
    last_ts = _usage["last_power_ts"]
    if last_ts is not None:
        dt = min(now - float(last_ts), POWER_DT_CAP)
        if dt > 0:
            bucket["pwr"] = (float(bucket.get("pwr", 0.0))
                             + power_w * dt / 1000 / 3600)
    _usage["last_power_ts"] = now
    _trim_usage_data(usage, now)
    write_json_atomic(path, usage)


def get_usage_aggregates(usage_data: dict[str, Any]) -> dict[str, Any]:
    """24h / 30d sums of the hourly buckets incl. kWh (source:1341-1376)."""
    now = time.time()
    cutoff_24h = now - 86400
    cutoff_30d = now - 30 * 86400
    acc = {"gen24": 0, "prompt24": 0, "cached24": 0, "pwr24": 0.0,
           "gen30": 0, "prompt30": 0, "cached30": 0, "pwr30": 0.0}
    for h in usage_data.get("hourly", []):
        hour = int(h.get("hour", 0))
        if hour >= cutoff_24h:
            acc["gen24"] += int(h.get("gen", 0))
            acc["prompt24"] += int(h.get("prompt", 0))
            acc["cached24"] += int(h.get("cached", 0))
            acc["pwr24"] += float(h.get("pwr", 0.0))
        if hour >= cutoff_30d:
            acc["gen30"] += int(h.get("gen", 0))
            acc["prompt30"] += int(h.get("prompt", 0))
            acc["cached30"] += int(h.get("cached", 0))
            acc["pwr30"] += float(h.get("pwr", 0.0))
    return {
        "gen_total_24h": acc["gen24"],
        "prompt_total_24h": acc["prompt24"],
        "gen_total_30d": acc["gen30"],
        "prompt_total_30d": acc["prompt30"],
        "prompt_cached_total_24h": acc["cached24"],
        "prompt_cached_total_30d": acc["cached30"],
        "prompt_cached_pct_24h": (round(acc["cached24"] / acc["prompt24"] * 100, 1)
                                  if acc["prompt24"] else None),
        "consumption_24h_kwh": round(acc["pwr24"], 3),
        "consumption_30d_kwh": round(acc["pwr30"], 3),
    }


def _get_llm_model_name(cfg: "ConsoleConfig") -> str:
    """OpenAI/llama.cpp/Ollama-shaped /v1/models basename minus .gguf,
    cached 60s; last-known or 'Unknown' on failure (source:1578-1608)."""
    now = time.time()
    if _vllm_model_cache["name"] is not None and (
            now - float(_vllm_model_cache["ts"])) < LLM_MODEL_CACHE_TTL:
        return str(_vllm_model_cache["name"])
    name: str | None = None
    base = _llm_base_url(cfg)
    if base:
        payload = fetch_json(base + "/v1/models", timeout=3)
        data: Any = None
        if isinstance(payload, dict):
            data = payload.get("data")
            if not isinstance(data, list) or not data:
                data = payload.get("models")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            mid = data[0].get("id") or data[0].get("model") or data[0].get("name")
            if isinstance(mid, str) and mid.strip():
                name = os.path.basename(mid.strip())
                if name.endswith(".gguf"):
                    name = name[:-5]
    if name:
        _vllm_model_cache["name"] = name
        _vllm_model_cache["ts"] = now
        return name
    return str(_vllm_model_cache["name"] or "Unknown")


# ---------- collector ----------
def _stats_prompt_cached_pct(aggregates: dict[str, Any]) -> float | None:
    if _usage["base"]["cached"] is None:
        return None  # HONEST-NONE until vLLM exposes the cached family
    pct: float | None = aggregates["prompt_cached_pct_24h"]
    return pct


def _health_llm(stats: dict[str, Any],
                aggregates: dict[str, Any]) -> dict[str, Any]:
    """Map the stats dict onto the golden health.json llm{} key set
    (source get_system_health:1462-1504)."""
    cached_known = _usage["base"]["cached"] is not None
    return {
        "last_gen_tps": stats.get("last_gen_tps", 0),
        "last_prompt_tps": stats.get("last_prompt_tps", 0),
        "kv_cache_fill_pct": stats.get("kv_cache_fill_pct", 0),
        "kv_cache_used_tokens": stats.get("kv_cache_used_tokens", 0),
        "kv_cache_max_tokens": stats.get("kv_cache_max_tokens", 0),
        "queue_depth": stats.get("queue_depth", 0),
        "queue_capacity": stats.get("queue_capacity"),
        "server_uptime_s": stats.get("server_uptime_s", 0),
        "model": stats.get("model", ""),
        "llm_port": stats.get("llm_port"),
        "llm_container": stats.get("llm_container"),
        "backend": stats.get("backend"),
        "running": stats.get("running"),
        "waiting": stats.get("waiting"),
        "ttft_ms": stats.get("ttft_ms"),
        "tpot_ms": stats.get("tpot_ms"),
        "e2e_ms": stats.get("e2e_ms"),
        "queue_ms": stats.get("queue_ms"),
        "prefix_hit_pct": stats.get("prefix_hit_pct"),
        "num_preemptions": stats.get("num_preemptions"),
        "spec_accept_pct": stats.get("spec_accept_pct"),
        "total_requests": stats.get("total_requests", 0),
        "error_count_5m": stats.get("error_count_5m", 0),
        "last_error": stats.get("last_error"),
        "latency_p50_ms": stats.get("latency_p50_ms"),
        "latency_p95_ms": stats.get("latency_p95_ms"),
        "latency_p99_ms": stats.get("latency_p99_ms"),
        "gen_total_24h": aggregates["gen_total_24h"],
        "prompt_total_24h": aggregates["prompt_total_24h"],
        "gen_total_30d": aggregates["gen_total_30d"],
        "prompt_total_30d": aggregates["prompt_total_30d"],
        # HONEST-NONE: cached totals stay None until vLLM actually exposed
        # prompt_tokens_cached_total — never a fabricated 0.
        "prompt_cached_total_24h": (aggregates["prompt_cached_total_24h"]
                                    if cached_known else None),
        "prompt_cached_total_30d": (aggregates["prompt_cached_total_30d"]
                                    if cached_known else None),
        "prompt_cached_pct_24h": (aggregates["prompt_cached_pct_24h"]
                                  if cached_known else None),
    }


class LlmCollector:
    """Detection + metrics + usage accumulator for the LLM serving stack."""

    name = "llm"

    def probe(self, cfg: "ConsoleConfig") -> bool:
        # Always "available": backend=None is an honest detectable state
        # (llm.backend null, numbers degraded) per DESIGN §3, so there is
        # nothing a probe could gate beyond the config switch.
        return cfg.plugins.llm.auto

    def collect(self, cfg: "ConsoleConfig",
                ctx: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        _ensure_usage_state(Path(ctx["paths"]["usage_stats"]))

        llm_port, llm_container, _, _ = _detect_llm_port(cfg)
        backend = _detect_llm_backend(cfg)
        # Idle-ghost prevention: log-derived values are llamacpp-ONLY. For
        # vLLM and for the None (deployment-flip / loading) window the stale
        # llama log must never be parsed — entries=[] / None everywhere.
        gpus: list[dict[str, Any]] = ctx.get("gpus") or []
        uptime_s = get_server_uptime_s(cfg)
        log_path: str | None = None
        entries: list[dict[str, Any]] = []
        if backend == "llamacpp":
            log_path = _llm_port_cache["log"]
            lines = tail_log(TAIL_LINES, log_path)
            entries = parse_entries(lines)
            latencies = get_latency_percentiles(entries)
            loading = get_model_loading_progress(cfg, lines)
            err_count, last_err = get_recent_errors(lines)
        else:
            lines = []
            latencies = {"latency_p50_ms": None, "latency_p95_ms": None,
                         "latency_p99_ms": None}
            loading = None
            err_count, last_err = 0, None

        # Usage accounting: llama parses the server log; vLLM counts from
        # cumulative /metrics counter deltas; None feeds nothing (power still
        # accrues, no recount can fire).
        power_w = (ctx.get("power") or {}).get("current_w")
        if power_w is None:
            power_w = sum(float(g.get("power_w", 0.0)) for g in gpus)
        if backend == "vllm":
            update_usage_data([], float(power_w),
                              vllm_delta=_get_vllm_usage_delta(cfg))
        elif backend == "llamacpp":
            update_usage_data(entries, float(power_w))
        else:
            update_usage_data([], float(power_w))

        # Provider layer: unified schema from the detected backend.
        if backend == "vllm":
            provider: dict[str, Any] = _get_vllm_metrics(cfg)
        elif backend == "llamacpp":
            provider = get_slots_metrics(cfg, log_path)
        else:
            provider = {}  # keeps the -1/'—' fallbacks, never crashes on None

        recent = entries[-20:] if len(entries) > 20 else entries
        avg_gen = (sum(e.get("gen_tps", 0) for e in recent) / len(recent)
                   if recent else 0)
        avg_prompt = (sum(e.get("prompt_tps", 0) for e in recent) / len(recent)
                      if recent else 0)

        stats: dict[str, Any] = {
            "updated": now,
            "updated_iso": time.strftime("%Y-%m-%d %H:%M:%S",
                                         time.localtime(now)),
            "model": _get_llm_model_name(cfg),
            "last_gen_tps": entries[-1]["gen_tps"] if entries else 0,
            "last_prompt_tps": entries[-1]["prompt_tps"] if entries else 0,
            "last_gen_tokens": entries[-1]["gen_tokens"] if entries else 0,
            "last_prompt_tokens": entries[-1]["prompt_tokens"] if entries else 0,
            "last_total_ms": entries[-1]["total_ms"] if entries else 0,
            "avg_gen_tps": round(avg_gen, 2),
            "avg_prompt_tps": round(avg_prompt, 2),
            "total_requests": len(entries),
            "history": entries[-60:],  # 60 per brief (source kept 20)
            "gpu": gpus,
            "kv_cache_fill_pct": provider.get("kv_cache_fill_pct", -1),
            "kv_cache_used_tokens": provider.get("kv_cache_used_tokens", -1),
            "kv_cache_max_tokens": provider.get("kv_cache_max_tokens", -1),
            "queue_depth": provider.get("queue_depth", -1),
            "queue_capacity": provider.get("queue_capacity", -1),
            "slots": provider.get("slots", []),
            "backend": backend,
            "running": provider.get("running"),
            "waiting": provider.get("waiting"),
            "ttft_ms": provider.get("ttft_ms"),
            "tpot_ms": provider.get("tpot_ms"),
            "e2e_ms": provider.get("e2e_ms"),
            "queue_ms": provider.get("queue_ms"),
            "prefix_hit_pct": provider.get("prefix_hit_pct"),
            "num_preemptions": provider.get("num_preemptions"),
            "spec_accept_pct": provider.get("spec_accept_pct"),
            "latency_p50_ms": latencies["latency_p50_ms"],
            "latency_p95_ms": latencies["latency_p95_ms"],
            "latency_p99_ms": latencies["latency_p99_ms"],
            "model_loading_progress": loading,
            "server_uptime_s": uptime_s,
            "llm_port": llm_port,
            "llm_container": llm_container,
            "error_count_5m": err_count,
            "last_error": last_err,
        }
        if backend == "vllm":
            vreq = _get_vllm_request_stats(cfg)
            if vreq["total_requests"] is not None:
                stats["total_requests"] = vreq["total_requests"]
            if vreq["error_count_5m"] is not None:
                stats["error_count_5m"] = vreq["error_count_5m"]
            stats["last_error"] = vreq["last_error"]
            # vLLM has no log-derived percentiles — honest None, never the
            # frozen llama-log numbers (card shows live TTFT/TPOT/E2E means).
            stats["latency_p50_ms"] = None
            stats["latency_p95_ms"] = None
            stats["latency_p99_ms"] = None
        elif backend is None and _last_vllm_total_requests is not None:
            # Flip/loading window: carry the last known vLLM count rather
            # than the frozen llama-log total.
            stats["total_requests"] = _last_vllm_total_requests
        if backend == "vllm":
            (stats["last_gen_tps"],
             stats["last_prompt_tps"]) = _update_vllm_tps(cfg)
        if backend == "llamacpp":
            # llama idle: newest log entry minutes old must read 0, never a
            # stale per-request number (idle-ghost prevention).
            try:
                log_idle = (time.time()
                            - os.path.getmtime(log_path or "")) > 120
            except OSError:
                log_idle = True
            if log_idle:
                stats["last_gen_tps"] = 0.0
                stats["last_prompt_tps"] = 0.0

        aggregates = get_usage_aggregates(_usage["data"] or _new_usage_data(now))
        stats["usage"] = aggregates
        # llm_prompt_cached_pct carrier for the history snapshot's None filter
        stats["prompt_cached_pct"] = _stats_prompt_cached_pct(aggregates)

        ctx["stats"] = stats
        ctx["usage_aggregates"] = aggregates
        return {"llm": _health_llm(stats, aggregates)}
