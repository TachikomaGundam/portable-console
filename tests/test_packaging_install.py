"""E2E tests for the v0.2.0 `install` / `uninstall` subcommands.

Everything lands under a fake HOME + XDG dirs (tmp_path). XDG_RUNTIME_DIR
points at a nonexistent dir and DBUS_SESSION_BUS_ADDRESS is removed so
`systemctl --user` can NEVER reach the real user manager — the code must
degrade to the install.sh-style warning path instead.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

import pytest

from portable_console import cli, paths

UNIT_NAMES = ("console-daemon.service", "console-server.service")


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    home = tmp_path / "home"
    xdg_config = home / ".config"
    xdg_data = tmp_path / "xdg-data"
    for d in (home, xdg_config, xdg_data):
        d.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "no-such-run"))
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setenv("USER", "tester")
    return {"home": home, "config": xdg_config, "data": xdg_data}


def test_install_full_flow(fake_home: dict[str, Path]) -> None:
    # When: portable-console install runs on a fresh machine ...
    rc = cli.main(["install"])
    # Then: config bootstrapped from the example, token 0600 in the XDG data
    # dir (the documented v0.2.0 fresh-install default), units rendered.
    assert rc == 0
    cfg_path = paths.default_config_path()
    assert cfg_path == fake_home["config"] / "portable-console" / "console.config.json"
    assert cfg_path.is_file()
    assert json.loads(cfg_path.read_text("utf-8"))["listen"]["port"] == 8090

    token = fake_home["data"] / "portable-console" / "console.token"
    assert token.is_file()
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert len(token.read_text("utf-8").strip()) == 32

    unit_dir = fake_home["config"] / "systemd" / "user"
    daemon_unit = (unit_dir / "console-daemon.service").read_text("utf-8")
    server_unit = (unit_dir / "console-server.service").read_text("utf-8")
    assert f"ExecStart=daemon run --config {cfg_path}" not in daemon_unit  # sanity: program prefix
    for unit, cmd in ((daemon_unit, "daemon run"), (server_unit, "serve")):
        assert re.search(rf"^ExecStart=.+ {re.escape(cmd)} --config {re.escape(str(cfg_path))}$",
                         unit, re.MULTILINE)
        assert f"WorkingDirectory={cfg_path.parent}" in unit
        assert "%RUN%" not in unit and "%DIR%" not in unit and "%ENV%" not in unit
        assert "Description=Portable Console" in unit
    # systemd syntax sanity (systemd-analyze unavailable under fake HOME):
    for unit in (daemon_unit, server_unit):
        assert re.search(r"^\[Service\]$", unit, re.MULTILINE)
        assert "Restart=always" in unit


def test_install_is_idempotent_and_never_touches_token(fake_home: dict[str, Path]) -> None:
    # Given: a first install ...
    assert cli.main(["install"]) == 0
    token = fake_home["data"] / "portable-console" / "console.token"
    before = token.read_bytes()
    cfg_text = paths.default_config_path().read_text("utf-8")
    # When: re-run with a port change ...
    assert cli.main(["install", "--port", "9191"]) == 0
    # Then: token untouched, config preserved (port edited in place).
    assert token.read_bytes() == before
    new_text = paths.default_config_path().read_text("utf-8")
    assert json.loads(new_text)["listen"]["port"] == 9191
    assert json.loads(cfg_text)["cards"] == json.loads(new_text)["cards"]


def test_install_no_systemd_skips_units(fake_home: dict[str, Path],
                                        capsys: pytest.CaptureFixture[str]) -> None:
    # When: --no-systemd ...
    rc = cli.main(["install", "--no-systemd"])
    # Then: no units written, manual hint printed.
    assert rc == 0
    assert not (fake_home["config"] / "systemd").exists()
    out = capsys.readouterr().out
    assert "--no-systemd" in out
    assert "daemon run --config" in out


def test_install_custom_config_and_data_dir_override_units(
        fake_home: dict[str, Path], tmp_path: Path) -> None:
    # Given: explicit --config inside a source-tree-like dir with legacy data/ ...
    root = tmp_path / "bundle"
    (root / "data").mkdir(parents=True)
    cfg = root / "console.config.json"
    # When: install with --data-dir override ...
    data = tmp_path / "elsewhere"
    rc = cli.main(["install", "--config", str(cfg), "--data-dir", str(data)])
    # Then: config created next to legacy data/ dir honored for runtime, but
    # install used the explicit dir for the token and passes --data to units.
    assert rc == 0
    assert (data / "console.token").is_file()
    daemon_unit = (paths.user_unit_dir() / "console-daemon.service").read_text("utf-8")
    assert f"daemon run --config {cfg} --data {data}" in daemon_unit


def test_install_renders_console_script_when_installed(
        fake_home: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a portable-console script on PATH (wheel/pipx layout) ...
    real_which = cli.shutil.which

    def which(name: str) -> Any:
        if name == "portable-console":
            return "/opt/pipx/bin/portable-console"
        return real_which(name)

    monkeypatch.setattr(cli.shutil, "which", which)
    # When: install ...
    assert cli.main(["install"]) == 0
    # Then: ExecStart uses the script, no PYTHONPATH env needed.
    daemon_unit = (paths.user_unit_dir() / "console-daemon.service").read_text("utf-8")
    assert "ExecStart=/opt/pipx/bin/portable-console daemon run --config " in daemon_unit
    # (the template header comment mentions PYTHONPATH; only the rendered
    # Environment directive must be absent in script mode)
    assert not re.search(r"^Environment=", daemon_unit, re.MULTILINE)


def test_uninstall_removes_units_keeps_state(fake_home: dict[str, Path]) -> None:
    # Given: an installed system ...
    assert cli.main(["install"]) == 0
    unit_dir = paths.user_unit_dir()
    assert all((unit_dir / n).is_file() for n in UNIT_NAMES)
    token = fake_home["data"] / "portable-console" / "console.token"
    cfg = paths.default_config_path()
    # When: uninstall ...
    assert cli.main(["uninstall"]) == 0
    # Then: both units gone; config + token preserved.
    assert not any((unit_dir / n).exists() for n in UNIT_NAMES)
    assert cfg.is_file()
    assert token.is_file()


def test_card_relative_script_resolves_against_root_dir(
        tmp_path: Path, fake_home: dict[str, Path],
        capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a config whose card script is a relative path, executable
    # relative to the CONFIG directory (v0.2.0 root_dir anchor) ...
    script = tmp_path / "ctl" / "status.sh"
    script.parent.mkdir()
    script.write_text('#!/bin/sh\necho \'{"state":"running"}\'\n', encoding="utf-8")
    script.chmod(0o755)
    cfg = tmp_path / "console.config.json"
    cfg.write_text(json.dumps({"cards": [{"id": "demo",
                                          "scripts": {"status": "ctl/status.sh"}}]}),
                   encoding="utf-8")
    # When: `card status demo` runs from an unrelated cwd ...
    (tmp_path / "nowhere").mkdir()
    monkeypatch.chdir(tmp_path / "nowhere")
    rc = cli.main(["card", "status", "demo", "--config", str(cfg)])
    # Then: it resolved against root_dir, not cwd or the package dir.
    assert rc == 0
    assert "running" in capsys.readouterr().out
