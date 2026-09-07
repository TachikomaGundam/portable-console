"""Unit tests for server.control.CardController (real tiny shell fixtures)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import control  # noqa: E402


def sh(root: Path, name: str, body: str) -> str:
    """Write an executable fixture script under ctl/, return its bundle-relative path."""
    (root / "ctl").mkdir(exist_ok=True)
    path = root / "ctl" / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return f"ctl/{name}"


@pytest.fixture()
def bundle(tmp_path: Path) -> dict[str, str]:
    return {
        "ok": sh(tmp_path, "ok.sh", "echo '{\"state\":\"running\",\"detail\":\"up\"}'"),
        "bad": sh(tmp_path, "bad.sh", "printf '%0.sx' $(seq 300)"),
        "empty": sh(tmp_path, "empty.sh", "true"),
        "start_ok": sh(tmp_path, "start_ok.sh", "echo '{\"started\":true}'"),
        "stop_fail": sh(tmp_path, "stop_fail.sh", "echo nope >&2; exit 3"),
        "slow": sh(tmp_path, "slow.sh", "sleep 5"),
    }


def make_controller(tmp_path: Path, s: dict[str, str]) -> control.CardController:
    return control.CardController(
        [
            {"id": "demo", "name": "Demo", "icon": "cpu", "timeout_s": 10,
             "scripts": {"status": s["ok"], "start": s["start_ok"], "stop": s["stop_fail"]}},
            {"id": "garbage", "scripts": {"status": s["bad"]}},
            {"id": "silent", "scripts": {"status": s["empty"]}},
            {"id": "slow", "timeout_s": 0.3, "scripts": {"status": s["slow"], "stop": s["slow"]}},
            {"id": "nostart", "scripts": {"status": s["ok"]}},
            {"id": "abs", "scripts": {"status": str(tmp_path / s["ok"])}},
        ],
        tmp_path,
    )


def test_list_cards_shape(tmp_path, bundle):
    cards = make_controller(tmp_path, bundle).list_cards()
    assert cards[0] == {"id": "demo", "name": "Demo", "icon": "cpu",
                        "actions": ["start", "stop"]}
    assert {c["id"] for c in cards} == {"demo", "garbage", "silent", "slow", "nostart", "abs"}


def test_status_parses_stdout_json(tmp_path, bundle):
    assert make_controller(tmp_path, bundle).status("demo") == {
        "state": "running", "detail": "up"}


def test_status_absolute_script_path(tmp_path, bundle):
    assert make_controller(tmp_path, bundle).status("abs")["state"] == "running"


def test_status_invalid_json_degrades_honestly(tmp_path, bundle):
    body = make_controller(tmp_path, bundle).status("garbage")
    assert body["state"] == "unknown"
    assert len(body["detail"]) == 200  # first 200 chars only


def test_status_empty_output(tmp_path, bundle):
    assert make_controller(tmp_path, bundle).status("silent") == {
        "state": "unknown", "detail": "no output"}


def test_action_ok_records_steps(tmp_path, bundle):
    result = make_controller(tmp_path, bundle).action("demo", "start")
    assert result["action"] == "start"
    assert result["result"] == "ok"
    assert result["steps"][0] == "start rc=0"
    assert '{"started":true}' in result["steps"][1]


def test_action_nonzero_rc_is_failed_not_exception(tmp_path, bundle):
    result = make_controller(tmp_path, bundle).action("demo", "stop")
    assert result["result"] == "failed"
    assert result["steps"][0] == "stop rc=3"
    assert "nope" in result["steps"][1]


def test_action_timeout_raises_card_timeout(tmp_path, bundle):
    with pytest.raises(control.CardTimeoutError, match="timed out after 0.3s"):
        make_controller(tmp_path, bundle).action("slow", "stop")


def test_status_timeout_raises_card_timeout(tmp_path, bundle):
    with pytest.raises(control.CardTimeoutError):
        make_controller(tmp_path, bundle).status("slow")


def test_unknown_card_raises_keyerror(tmp_path, bundle):
    ctl = make_controller(tmp_path, bundle)
    with pytest.raises(KeyError):
        ctl.status("ghost")
    with pytest.raises(KeyError):
        ctl.action("ghost", "start")


def test_missing_script_for_verb_raises_valueerror(tmp_path, bundle):
    with pytest.raises(ValueError, match="no 'start' script"):
        make_controller(tmp_path, bundle).action("nostart", "start")


def test_unknown_verb_raises_valueerror(tmp_path, bundle):
    with pytest.raises(ValueError, match="unknown action"):
        make_controller(tmp_path, bundle).action("demo", "recover")


def test_exec_failure_propagates_oserror(tmp_path):
    ctl = control.CardController(
        [{"id": "gone", "scripts": {"start": "ctl/definitely-missing.sh"}}], tmp_path)
    with pytest.raises(OSError):
        ctl.action("gone", "start")
