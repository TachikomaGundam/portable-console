"""Config-driven control-card executor (console/DESIGN.md §4).

One CardController replaces the hardcoded per-model control servers
(legacy per-model control servers): cards come from console.config.json,
and every request just execs the configured script. Script strings come from
TRUSTED local config only — no user input ever reaches argv (shlex.split here
is safe for exactly that reason). Relative script paths resolve against the
bundle root via subprocess cwd (§2 path rule).
"""
from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT_S = 90
ACTIONS: tuple[str, ...] = ("start", "stop")


class CardTimeoutError(RuntimeError):
    """A card script exceeded its configured timeout_s."""


class Card:
    """One control card: identity + start/stop/status scripts from config."""

    def __init__(self, spec: dict[str, Any], bundle_root: Path) -> None:
        self.id: str = str(spec["id"])
        self.name: str = str(spec.get("name", self.id))
        self.icon: str = str(spec.get("icon", "card"))
        self.timeout_s: float = float(spec.get("timeout_s", DEFAULT_TIMEOUT_S))
        self.bundle_root = bundle_root
        self._scripts: dict[str, str] = dict(spec.get("scripts", {}))

    def run(self, verb: str) -> tuple[int, str, str]:
        """Exec the configured script for *verb*; return (rc, stdout, stderr).

        Raises CardTimeoutError on timeout; OSError (e.g. missing binary) and
        ValueError (no script configured) propagate to the HTTP caller, which
        maps them to 504 / 500.
        """
        script = self._scripts.get(verb)
        if not script:
            raise ValueError(f"card '{self.id}' has no '{verb}' script in config")
        argv = shlex.split(str(script))
        if not argv:
            raise ValueError(f"card '{self.id}' has empty '{verb}' script")
        try:
            p = subprocess.run(
                argv, capture_output=True, text=True,
                timeout=self.timeout_s, cwd=self.bundle_root,
            )
        except subprocess.TimeoutExpired as exc:
            raise CardTimeoutError(
                f"card '{self.id}' '{verb}' timed out after {self.timeout_s:g}s"
            ) from exc
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


class CardController:
    """Registry of cards keyed by id; the whole control-plane API surface."""

    def __init__(self, cards: list[dict[str, Any]], bundle_root: Path) -> None:
        self.cards: dict[str, Card] = {
            str(spec["id"]): Card(spec, bundle_root) for spec in cards
        }

    def list_cards(self) -> list[dict[str, Any]]:
        return [
            {"id": c.id, "name": c.name, "icon": c.icon, "actions": list(ACTIONS)}
            for c in self.cards.values()
        ]

    def status(self, card_id: str) -> dict[str, Any]:
        """Passthrough of the status script's stdout JSON.

        Invalid / empty JSON degrades honestly to {"state":"unknown","detail":...}
        (first 200 chars). Unknown card -> KeyError.
        """
        _, out, err = self._card(card_id).run("status")
        raw = out or err
        if not raw:
            return {"state": "unknown", "detail": "no output"}
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("status payload must be a JSON object")
            return parsed
        except (json.JSONDecodeError, ValueError):
            return {"state": "unknown", "detail": raw[:200]}

    def action(self, card_id: str, verb: str) -> dict[str, Any]:
        """Run start/stop; returns {"action","result":"ok|failed","steps":[...]}.

        Raises KeyError (unknown card), ValueError (no such verb / no script),
        CardTimeoutError, OSError (exec failure) — mapped by the HTTP layer.
        """
        if verb not in ACTIONS:
            raise ValueError(f"unknown action '{verb}'")
        card = self._card(card_id)
        code, out, err = card.run(verb)
        detail = (out or err)[:300] or "(no output)"
        return {
            "action": verb,
            "result": "ok" if code == 0 else "failed",
            "steps": [f"{verb} rc={code}", detail],
        }

    def _card(self, card_id: str) -> Card:
        try:
            return self.cards[card_id]
        except KeyError:
            raise KeyError(card_id) from None
