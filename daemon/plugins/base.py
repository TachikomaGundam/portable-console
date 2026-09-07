"""Collector protocol, probe caching and atomic JSON writes (DESIGN.md §1).

Every hardware/metric source is a `Collector`:

- ``probe(cfg) -> bool``  cheap availability check; the harness caches it via
  ``Prober`` (60 s on success, 15 s on failure) so a missing device costs one
  check per minute, not one per poll.
- ``collect(cfg, ctx) -> dict``  returns the health.json key fragment the
  plugin owns (e.g. ``{"gpus": [...]}``); ``{}`` means "nothing this cycle"
  and the keys stay absent (honest degradation, DESIGN §0).

``ctx`` is the shared per-cycle dict: ``config``, ``paths`` (data_dir file
targets), plus every section collected so far (so ``llm`` can read
``ctx["gpus"]`` / ``ctx["power"]``; plugin order in harness.py guarantees
availability).
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Protocol

if False:  # pragma: no cover - type-only import, avoids runtime cycle
    from daemon.config import ConsoleConfig

PROBE_OK_TTL = 60.0
PROBE_FAIL_TTL = 15.0


class Collector(Protocol):
    """A data-collection plugin (see module docstring)."""

    name: str

    def probe(self, cfg: "ConsoleConfig") -> bool:
        """Return True when this collector's data source is available."""
        ...

    def collect(self, cfg: "ConsoleConfig", ctx: dict[str, Any]) -> dict[str, Any]:
        """Return this plugin's health.json key fragment ({} when unavailable)."""
        ...


class Prober:
    """60 s / 15 s TTL cache around a Collector.probe callable."""

    def __init__(self, probe: Callable[[], bool]) -> None:
        self._probe = probe
        self._result: bool | None = None
        self._ts: float = 0.0

    def enabled(self) -> bool:
        now = time.monotonic()
        if self._result is not None:
            ttl = PROBE_OK_TTL if self._result else PROBE_FAIL_TTL
            if now - self._ts < ttl:
                return self._result
        try:
            self._result = bool(self._probe())
        except Exception:  # a broken probe must never kill the daemon
            self._result = False
        self._ts = now
        return self._result

    def invalidate(self) -> None:
        self._result = None


def write_json_atomic(path: Path | str, payload: Any) -> None:
    """tempfile + fsync + chmod 0644 + os.replace (DESIGN §3 write discipline).

    The explicit chmod defeats the process umask so consumers (caddy /
    the portal server) can always read the artifacts.
    """
    p = Path(path)
    dir_name = p.parent
    dir_name.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=str(dir_name), delete=False, encoding="utf-8"
    ) as f:
        json.dump(payload, f, indent=None)
        f.flush()
        os.fsync(f.fileno())
        tmp_path = f.name
    os.chmod(tmp_path, 0o644)
    os.replace(tmp_path, str(p))


def run_argv(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """subprocess.run with argv list + text + timeout + capture, never shell."""
    return subprocess.run(  # argv lists only, never shell=True
        argv, capture_output=True, text=True, timeout=timeout
    )
