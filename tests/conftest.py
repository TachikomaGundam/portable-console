"""Shared fixtures for the console daemon tests (stdlib + pytest only).

Everything external is faked: no subprocess, no network, no root, no
reliance on this machine's nvidia-smi / ipmitool / LLM services.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

# v0.2.0: package moved to src/ layout — point sys.path at src/portable_console
_BUNDLE = Path(__file__).resolve().parents[1] / "src"
if str(_BUNDLE) not in sys.path:
    sys.path.insert(0, str(_BUNDLE))

from portable_console.daemon.config import (  # noqa: E402
    ConsoleConfig, HistoryConfig, ListenConfig,
    LlmPluginConfig, NvidiaPluginConfig,
    PluginsConfig, SystemPluginConfig)


def _make_config(tmp_path: Path, **plugin_over: Any) -> ConsoleConfig:
    """Minimal valid ConsoleConfig rooted at a tmp data dir."""
    return ConsoleConfig(
        root_dir=tmp_path,  # v0.2.0: was bundle_root (same role)
        listen=ListenConfig(),
        data_dir=tmp_path / "data",
        poll_interval_s=1,
        history=HistoryConfig(max_samples=plugin_over.get("max_samples", 720)),
        plugins=PluginsConfig(
            system=SystemPluginConfig(),
            nvidia=NvidiaPluginConfig(),
            llm=LlmPluginConfig(
                non_llm_ports=tuple(plugin_over.get("non_llm_ports", ()))),
        ),
    )


@pytest.fixture
def make_config(tmp_path: Path):
    """Fixture: make_config() -> ConsoleConfig rooted at the test tmp_path."""
    return _make_config(tmp_path)


def cp(stdout: str = "", returncode: int = 0, stderr: str = ""
       ) -> subprocess.CompletedProcess[str]:
    """CompletedProcess factory for run_argv fakes."""
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


class FakeClock:
    """Deterministic time.time() replacement (mutable via .t / .advance)."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start

    def time(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(time, "time", fake.time)
    monkeypatch.setattr(time, "monotonic", fake.time)
    return fake


class FakeUrlopen:
    """urllib.request.urlopen stand-in routing by URL substring.

    Responses map: {substring: body-str | list[body] (popped per call) |
    Exception instance (raised)}. Unknown URLs raise URLError-ish OSError.
    Records every requested URL in .calls.
    """

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url: str, timeout: float = 2.0, **kw: Any):
        del timeout, kw
        self.calls.append(url)
        for sub, resp in self.routes.items():
            if sub in url:
                body = resp.pop(0) if isinstance(resp, list) else resp
                if isinstance(body, Exception):
                    raise body
                return _FakeResp(body)
        raise OSError(f"no fake route for {url}")


class _FakeResp:
    def __init__(self, body: str) -> None:
        self._body = body.encode()

    def read(self, n: int = -1) -> bytes:
        return self._body if n < 0 else self._body[:n]

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *a: Any) -> bool:
        return False
