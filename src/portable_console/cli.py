#!/usr/bin/env python3
"""Portable console CLI: daemon / serve / card / token (DESIGN.md §1).

Subcommands:
  daemon run|once [--config PATH]   collector loop (default: ./console.config.json
                                    if it exists, else the XDG user config written
                                    by `install` — see _config_path())
  serve        [--config PATH]      static portal + /api/* (server/server.py,
                                    owned by the server agent; imported only
                                    when this subcommand runs)
  card status|start|stop <id>       run a control-card script action locally
  token        [--config PATH]      print (creating if missing) the 0600 token
  install      [--port N] [--no-systemd] [--config PATH] [--data-dir PATH]
                                    config + token + systemd user units
                                    (v0.2.0: python port of install.sh)
  uninstall                         disable + remove the installed user units
                                    (keeps data dir and config)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

# allow: SIZE_OK — the task spec pins the install/uninstall implementation
# into cli.py (single CLI owner); splitting installer logic elsewhere was the
# rejected alternative.

os.umask(0o022)  # artifacts world-readable (644) — DESIGN §3 write discipline

from portable_console import paths  # noqa: E402
from portable_console.daemon.config import ConsoleConfig, ConfigError, load as load_config  # noqa: E402
from portable_console.daemon.harness import run_forever, run_once_cli  # noqa: E402

# allow running as a bare script from anywhere: bundle root on sys.path
# v0.2.0: dropped — cli.py is a package module now; the bare-script entry point
# is the source-tree shim console/console.py which puts src/ on sys.path.

TOKEN_FILE = "console.token"
DEFAULT_CONFIG = "console.config.json"
_CARD_DEFAULT_TIMEOUT_S = 90.0
_CARD_ACTIONS = ("status", "start", "stop")

# v0.2.0 install (port of install.sh): units rendered from the packaged
# templates; %RUN%/%DIR%/%ENV% placeholders replaced by _render_unit().
_UNIT_NAMES = ("console-daemon.service", "console-server.service")
_UNIT_COMMANDS = {"console-daemon.service": "daemon run",
                  "console-server.service": "serve"}
_PORT_RE = re.compile(r'("listen".*"port"\s*:\s*)[0-9]+')


def _config_path(args: argparse.Namespace, *, install_target: bool = False) -> Path:
    raw = getattr(args, "config", None)
    if raw:
        p = Path(raw).expanduser()
        # v0.2.0: relative --config resolves against CWD (was the bundle root —
        # the package has no bundle). Running from the source-tree dir keeps the
        # v0.1.0 experience identical.
        return p if p.is_absolute() else (Path.cwd() / p)
    if install_target:
        # `install` alone picks its WRITE target: XDG user config.
        return paths.default_config_path()
    # Runtime discovery cascade (behavior contract): ./console.config.json wins
    # for v0.1 bundle parity; else the XDG file `install` wrote; else fall
    # through to (1) so a fresh machine keeps the exact v0.1 ConfigError text.
    cwd_default = Path.cwd() / DEFAULT_CONFIG
    xdg_default = paths.default_config_path()
    if cwd_default.exists() or not xdg_default.exists():
        return cwd_default
    return xdg_default


def _fail(msg: str, code: int = 2) -> "int":
    print(msg, file=sys.stderr)
    return code


# ---------- token ----------
def _ensure_token(data_dir: Path) -> Path:
    """Create data/console.token (0600, O_EXCL) if missing; never overwrite."""
    token_path = data_dir / TOKEN_FILE
    if not token_path.exists():
        data_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(secrets.token_hex(16) + "\n")
        os.chmod(token_path, 0o600)
    return token_path


def cmd_token(cfg: ConsoleConfig) -> int:
    """Print the control token, creating data/console.token (0600) once."""
    token_path = _ensure_token(cfg.data_dir)
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
        # v0.2.0: root_dir anchor (config file's directory; v0.1.0 bundle_root)
        prog = cfg.root_dir / argv[0]
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


# ---------- install / uninstall (v0.2.0 port of install.sh) ----------
def _log(msg: str) -> None:
    print(f"[install] {msg}")


def _warn(msg: str) -> None:
    print(f"[install][警告] {msg}", file=sys.stderr)


def _die(msg: str) -> int:
    print(f"[install][错误] {msg}", file=sys.stderr)
    return 1


def _systemctl_user(*unit_args: str) -> bool:
    """Best-effort `systemctl --user …`; False when systemctl is missing or
    there is no user systemd session (install.sh's degradation parity)."""
    try:
        r = subprocess.run(["systemctl", "--user", *unit_args],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def _apply_port(config_path: Path, port: int) -> None:
    """Set listen.port in the config (mirrors install.sh apply_port): first try
    the single-line in-place regex edit that preserves formatting; fall back to
    a semantic JSON rewrite for multi-line layouts."""
    text = config_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    changed = False
    for i, line in enumerate(lines):
        new, n = _PORT_RE.subn(rf"\g<1>{port}", line, count=1)
        if n:
            lines[i] = new
            changed = True
    if changed:
        config_path.write_text("".join(lines), encoding="utf-8")
    else:
        cfg = json.loads(text)
        cfg.setdefault("listen", {})["port"] = port
        config_path.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    back = json.loads(config_path.read_text(encoding="utf-8"))
    if (back.get("listen") or {}).get("port") != port:
        raise ConfigError(f"端口写入失败，请检查 {config_path} 格式")


def _current_port(config_path: Path) -> int:
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        return int((cfg.get("listen") or {}).get("port", 8090))
    except (OSError, ValueError, json.JSONDecodeError):
        return 8090


def _exec_argv() -> tuple[str, ...]:
    """ExecStart program: the installed console script, or `-m portable_console`
    against this very interpreter for source-tree runs (v0.2.0)."""
    script = shutil.which("portable-console")
    if script:
        return (script,)
    return (sys.executable, "-m", "portable_console")


def _render_unit(name: str, config_path: Path, root: Path, extra: str) -> str:
    """Render a packaged unit template: %RUN% -> ExecStart line, %DIR% ->
    WorkingDirectory, %ENV% -> PYTHONPATH when running via -m.

    `extra` is appended to the ExecStart line only (e.g. " --data DIR" when
    install was given --data-dir, so the runtime sees the same override).
    v0.2.0 orchestrator follow-up: every interpolated path goes through
    shlex.quote — a $HOME containing spaces must not split into multiple
    systemd ExecStart words (unit argv follows shell-like quoting)."""
    text = (paths.systemd_dir() / name).read_text(encoding="utf-8")
    argv = list(_exec_argv())
    argv += _UNIT_COMMANDS[name].split()
    argv += ["--config", str(config_path)]
    if extra:
        argv += shlex.split(extra)
    run = shlex.join(argv)
    if argv[0] == sys.executable:  # -m fallback: make src/ importable
        # Environment= with spaces needs the whole assignment double-quoted.
        env = f'Environment="PYTHONPATH={paths.package_dir().parent}"'
    else:
        env = ""
    # substitute placeholders on DIRECTIVE lines only — the templates' header
    # comments name the placeholders literally and must survive rendering.
    out = []
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("#"):
            out.append(line)
            continue
        out.append(line.replace("%RUN%", run).replace("%DIR%", str(root))
                   .replace("%ENV%", env))
    return "".join(out)


def _install_units(config_path: Path, root: Path, data_dir: Path,
                   data_override: bool) -> None:
    unit_dir = paths.user_unit_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    extra = f" --data {data_dir}" if data_override else ""
    for name in _UNIT_NAMES:
        src = paths.systemd_dir() / name
        if not src.is_file():
            raise ConfigError(f"缺少单元模板 {src}")
        dst = unit_dir / name
        dst.write_text(_render_unit(name, config_path, root, extra),
                       encoding="utf-8")
        _log(f"已渲染安装用户单元：{dst}")
    if shutil.which("systemctl"):
        if _systemctl_user("daemon-reload"):
            _log("systemd --user 已重载单元")
        else:
            _warn("systemctl --user daemon-reload 失败（可能无用户 systemd 会话），请手动执行。")
    else:
        _warn("未找到 systemctl，跳过 daemon-reload；请以命令行方式运行（参考 --no-systemd 提示）。")


def _next_steps(config_path: Path) -> None:
    port = _current_port(config_path)
    user = os.environ.get("USER") or "your-user"
    print()
    _log("本次安装未自动启动服务。启动命令：")
    print("    systemctl --user enable --now console-daemon console-server")
    print(f"    # 浏览器打开 http://127.0.0.1:{port}")
    print()
    _log("开机常驻（无需登录会话）需一次性开启 linger：")
    print(f"    loginctl enable-linger {user}")
    print("    # 说明：linger 让 user unit 在开机/未登录时也被拉起；该操作走 polkit，")
    print("    # 可能需要一次管理员授权，属可选步骤。不开启则服务仅在你登录期间运行。")


def _manual_hint(config_path: Path) -> None:
    port = _current_port(config_path)
    print()
    _log("已跳过 systemd 安装（--no-systemd）。手动运行（建议分别放后台/独立终端）：")
    print(f"    cd {config_path.parent}")
    print(f"    portable-console daemon run --config {config_path}   # 采集守护")
    print(f"    portable-console serve    --config {config_path}   # Web 门户")
    print(f"    # 浏览器打开 http://127.0.0.1:{port}")


def cmd_install(args: argparse.Namespace) -> int:
    """Full port of install.sh: config bootstrap, --port edit, 0600 token,
    systemd user units (honoring --no-systemd). Idempotent; never overwrites
    an existing config or token."""
    if sys.version_info < (3, 10):
        return _die(f"Python 版本过低：当前 {sys.version.split()[0]}，需要 >= 3.10。")
    config_path = _config_path(args, install_target=True)
    # 1. config bootstrap (XDG-aware default when --config absent)
    if not config_path.is_file():
        example = paths.example_config()
        if not example.is_file():
            return _die(f"缺少示例配置 {example}")
        config_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(example, config_path)
        _log(f"已生成 {config_path}（复制自示例；cards/links 请按需编辑）")
    else:
        _log(f"{config_path} 已存在（保留现有配置，不覆盖）")
    # 2. optional port override
    if args.port is not None:
        if not 1 <= args.port <= 65535:
            return _die(f"端口越界: {args.port}")
        try:
            _apply_port(config_path, args.port)
        except (ConfigError, json.JSONDecodeError) as exc:
            return _die(f"{exc}；请检查 {config_path} 格式")
        _log(f"监听端口已设为 {args.port}")
    # 3. data dir + token
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        return _die(f"配置无效: {exc}")
    data_dir = cfg.data_dir if not args.data_dir else Path(args.data_dir).expanduser()
    if not data_dir.is_absolute():
        data_dir = paths.root_dir(config_path) / data_dir
    token_existed = (data_dir / TOKEN_FILE).is_file()
    _ensure_token(data_dir)
    if token_existed:
        _log(f"{data_dir / TOKEN_FILE} 已存在（保留现有令牌）")
    else:
        _log(f"令牌就绪：{data_dir / TOKEN_FILE}（权限 0600）")
    # 4. units
    root = paths.root_dir(config_path)
    if args.no_systemd:
        _manual_hint(config_path)
    else:
        try:
            _install_units(config_path, root, data_dir, bool(args.data_dir))
        except ConfigError as exc:
            return _die(str(exc))
        _next_steps(config_path)
    _log(f"安装完成。root: {root}")
    return 0


def cmd_uninstall() -> int:
    """Mirror install.sh --uninstall: disable + remove the two user units.
    Keeps the data dir and the config file."""
    _log("卸载用户单元（保留数据目录与配置文件）")
    unit_dir = paths.user_unit_dir()
    has_systemctl = shutil.which("systemctl") is not None
    if has_systemctl:
        for name in _UNIT_NAMES:
            _systemctl_user("disable", "--now", name)  # best-effort, like `|| true`
    changed = False
    for name in _UNIT_NAMES:
        dst = unit_dir / name
        if dst.exists():
            dst.unlink()
            _log(f"已移除 {dst}")
            changed = True
    if changed and has_systemctl:
        _systemctl_user("daemon-reload")
    _log("完成。health.json、console.token 等数据与 console.config.json 均已保留。")
    return 0


# ---------- main ----------
def build_parser() -> argparse.ArgumentParser:
    # v0.2.0: no fixed prog anymore — argparse derives it from the entry point
    # (portable-console) or the script name (console.py via the source shim).
    parser = argparse.ArgumentParser(
        description="Portable root-free server console (DESIGN.md).",
    )
    # --config lives on the LEAF subcommands (`daemon once --config PATH`),
    # not the root parser, per the CLI contract.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=None,
                        help="config file (default: ./console.config.json, "
                             "falling back to the $XDG_CONFIG_HOME/portable-console/"
                             "console.config.json written by `install`; "
                             "relative paths resolve against CWD)")
    common.add_argument("--data", default=None,
                        help="override the resolved data dir (v0.2.0; "
                             "e.g. for throwaway runs against a bundle data/)")
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

    inst = sub.add_parser("install",
                          help="bootstrap config + token + systemd user units "
                               "(python port of install.sh)")
    inst.add_argument("--port", type=int, default=None,
                      help="set listen.port in the config")
    inst.add_argument("--no-systemd", action="store_true",
                      help="skip unit installation, print manual run hints")
    inst.add_argument("--config", default=None,
                      help="target config path (default: "
                           "$XDG_CONFIG_HOME/portable-console/console.config.json)")
    inst.add_argument("--data-dir", default=None,
                      help="data dir override (default: per-config / XDG rules)")

    sub.add_parser("uninstall",
                   help="disable + remove the user units installed by `install`")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # v0.2.0: install/uninstall create their own config; dispatch before the
    # mandatory load so a fresh machine (no console.config.json) works.
    if args.command == "install":
        return cmd_install(args)
    if args.command == "uninstall":
        return cmd_uninstall()
    try:
        cfg = load_config(_config_path(args))
    except ConfigError as exc:
        return _fail(f"config error: {exc}")
    if getattr(args, "data", None):
        cfg = replace(cfg, data_dir=Path(args.data))

    if args.command == "daemon":
        if args.mode == "once":
            return run_once_cli(_config_path(args),
                                data_dir=Path(args.data) if args.data else None)
        return run_forever(cfg)
    if args.command == "serve":
        # server module is owned by the parallel server agent — import only
        # when actually serving so the daemon works without it present.
        from portable_console.server.server import main as serve_main
        return int(serve_main(cfg) or 0)
    if args.command == "card":
        return cmd_card(cfg, args.action, args.card_id)
    if args.command == "token":
        return cmd_token(cfg)
    return _fail(f"unknown command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
