"""Unit tests for portable_console.paths + config anchor semantics (v0.2.0).

Given/When/Then throughout; HOME / XDG_* fully monkeypatched — nothing here
reads or writes the real user directories.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from portable_console import paths
from portable_console.daemon import config as daemon_config


@pytest.fixture()
def clean_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fake HOME, XDG_DATA_HOME unset by default; returns the fake home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    return home


def test_package_dir_exposes_shipped_resources() -> None:
    # Given the installed/source package ...
    pkg = paths.package_dir()
    # Then its data files are inside the package (not next to console.py).
    assert paths.web_root() == pkg / "web"
    assert (paths.web_root() / "index.html").is_file()
    assert paths.example_config().is_file()
    assert (paths.systemd_dir() / "console-daemon.service").is_file()
    assert (paths.systemd_dir() / "console-server.service").is_file()


def test_root_dir_is_config_parent_or_cwd(tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a config in some directory, and separately no config at all ...
    cfg = tmp_path / "console.config.json"
    monkeypatch.chdir(tmp_path)
    # Then root_dir anchors on the config's directory, else on cwd.
    assert paths.root_dir(cfg) == tmp_path
    assert paths.root_dir(None) == Path.cwd()


def test_default_data_dir_legacy_sidecar_data_wins(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch,
                                                   clean_env: Path) -> None:
    # Given a config whose directory already contains a data/ dir (v0.1.0
    # bundle layout) AND XDG_DATA_HOME set ...
    (tmp_path / "data").mkdir()
    cfg = tmp_path / "console.config.json"
    monkeypatch.setenv("XDG_DATA_HOME", str(clean_env / "xdg"))
    # Then the legacy sidecar directory wins (backward compat).
    assert paths.default_data_dir(cfg) == tmp_path / "data"


def test_default_data_dir_uses_xdg_data_home(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch,
                                             clean_env: Path) -> None:
    # Given a config with NO sidecar data/ dir and XDG_DATA_HOME set ...
    cfg = tmp_path / "console.config.json"
    monkeypatch.setenv("XDG_DATA_HOME", str(clean_env / "xdg"))
    # Then the XDG application dir is used.
    assert paths.default_data_dir(cfg) == clean_env / "xdg" / "portable-console"


def test_default_data_dir_falls_back_to_home(tmp_path: Path,
                                             clean_env: Path) -> None:
    # Given no config and no XDG_DATA_HOME ...
    # Then the XDG spec default under HOME applies.
    assert paths.default_data_dir(None) == clean_env / ".local" / "share" / "portable-console"


def test_config_load_explicit_data_dir_resolves_against_root(tmp_path: Path,
                                                             clean_env: Path) -> None:
    # Given a config explicitly setting a relative data_dir ...
    cfg = tmp_path / "console.config.json"
    cfg.write_text(json.dumps({"data_dir": "stuff"}), encoding="utf-8")
    # When loaded ...
    loaded = daemon_config.load(cfg)
    # Then it resolves against the config dir (old _resolve_path semantics)
    # and root_dir == config parent; deprecated alias still agrees.
    assert loaded.data_dir == tmp_path / "stuff"
    assert loaded.root_dir == tmp_path
    assert loaded.bundle_root == loaded.root_dir


def test_config_load_without_data_dir_key_uses_default_rule(tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch,
                                                            clean_env: Path) -> None:
    # Given a config JSON WITHOUT the data_dir key and no sidecar data/ ...
    cfg = tmp_path / "console.config.json"
    cfg.write_text("{}", encoding="utf-8")
    xdg = clean_env / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    # When loaded, data_dir follows default_data_dir (XDG branch here).
    assert daemon_config.load(cfg).data_dir == xdg / "portable-console"
    # Given a sidecar data/ dir appears (legacy bundle) ...
    (tmp_path / "data").mkdir()
    # Then it wins — the fresh XDG default only applies to truly fresh setups.
    assert daemon_config.load(cfg).data_dir == tmp_path / "data"
