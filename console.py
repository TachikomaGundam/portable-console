#!/usr/bin/env python3
"""Portable console CLI: daemon / serve / card / token (DESIGN.md §1).

Subcommands:
  daemon run|once [--config PATH]   collector loop (default console.config.json)
  serve        [--config PATH]      static portal + /api/* (server/server.py,
                                    owned by the server agent; imported only
                                    when this subcommand runs)
  card status|start|stop <id>       run a control-card script action locally
  token        [--config PATH]      print (creating if missing) the 0600 token
"""
from __future__ import annotations

import argparse
import os
import secrets
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

os.umask(0o022)  # artifacts world-readable (644) — DESIGN §3 write discipline

# allow running as a bare script from anywhere: bundle root on sys.path
_BUNDLE_ROOT = Path(__file__).resolve().parent
if str(_BUNDLE_ROOT) not in sys.path:
    sys.path.insert(0, str(_BUNDLE_ROOT))

from daemon.config import ConsoleConfig, ConfigError, load as load_config  # noqa: E402
from daemon.harness import run_forever, run_once_cli  # noqa: E402

TOKEN_FILE = "console.token"
DEFAULT_CONFIG = "console.config.json"
_CARD_DEFAULT_TIMEOUT_S = 90.0
_CARD_ACTIONS = ("status", "start", "stop")


def _config_path(args: argparse.Namespace) -> Path:
    p = Path(args.config)
    return p if p.is_absolute() else (_BUNDLE_ROOT / p)


def _fail(msg: str, code: int = 2) -> "int":
    print(msg, file=sys.stderr)
    return code


# ---------- token ----------
def cmd_token(cfg: ConsoleConfig) -> int:
    """Print the control token, creating data/console.token (0600) once."""
    token_path = cfg.data_dir / TOKEN_FILE
    if not token_path.exists():
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(secrets.token_hex(16) + "\n")
        os.chmod(token_path, 0o600)
    print(token_path.read_text(encoding="utf-8").strip())
    return 0


# ---------- card ----------
def _find_card(cfg: ConsoleConfig, card_id: str) -> dict[str, Any] | None:
    for card in cfg.cards:
        if card.get("id") == card_id:
            return card
    return None


def cmd_card(cfg: ConsoleConfig, action: str, card_id: str) -> int:
    """CLI mirror of the server-side control-card executor (§4): runs the
    configured argv script, prints its stdout verbatim (JSON contract)."""
    card = _find_card(cfg, card_id)
    if card is None:
        return _fail(f"unknown card id: {card_id}")
    scripts = card.get("scripts") or {}
    script = scripts.get(action)
    if not isinstance(script, str) or not script.strip():
        return _fail(f"card {card_id} has no scripts.{action}")
    argv = shlex.split(script)
    prog = Path(argv[0])
    if not prog.is_absolute():
        prog = cfg.bundle_root / argv[0]
    timeout_s = float(card.get("timeout_s", _CARD_DEFAULT_TIMEOUT_S))
    try:
        r = subprocess.run([str(prog), *argv[1:]], capture_output=True,
                           text=True, timeout=timeout_s)
    except FileNotFoundError:
        return _fail(f"script not found: {prog}", 127)
    except subprocess.TimeoutExpired:
        return _fail(f"card {card_id} {action} timed out after {timeout_s:.0f}s",
                     2)
    if r.stdout:
        sys.stdout.write(r.stdout)
    if r.stderr:
        sys.stderr.write(r.stderr)
    return r.returncode


# ---------- main ----------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="console.py",
        description="Portable root-free server console (DESIGN.md).",
    )
    # --config lives on the LEAF subcommands (`daemon once --config PATH`),
    # not the root parser, per the CLI contract.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=DEFAULT_CONFIG,
                        help="config file (default: console.config.json "
                             "relative to the bundle root)")
    sub = parser.add_subparsers(dest="command", required=True)

    daemon = sub.add_parser("daemon", help="collector daemon")
    dsub = daemon.add_subparsers(dest="mode", required=True)
    dsub.add_parser("run", parents=[common], help="poll forever")
    dsub.add_parser("once", parents=[common],
                    help="one polling cycle, write artifacts, exit")

    sub.add_parser("serve", parents=[common],
                   help="static portal + /api/* (server agent)")

    card = sub.add_parser("card", help="run a control-card script action")
    csub = card.add_subparsers(dest="action", required=True)
    for act in _CARD_ACTIONS:
        cp = csub.add_parser(act, parents=[common])
        cp.add_argument("card_id")

    sub.add_parser("token", parents=[common],
                   help="print/create the control token")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(_config_path(args))
    except ConfigError as exc:
        return _fail(f"config error: {exc}")

    if args.command == "daemon":
        if args.mode == "once":
            return run_once_cli(_config_path(args))
        return run_forever(cfg)
    if args.command == "serve":
        # server module is owned by the parallel server agent — import only
        # when actually serving so the daemon works without it present.
        from server.server import main as serve_main  # type: ignore[import-not-found]
        return int(serve_main(cfg) or 0)
    if args.command == "card":
        return cmd_card(cfg, args.action, args.card_id)
    if args.command == "token":
        return cmd_token(cfg)
    return _fail(f"unknown command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
