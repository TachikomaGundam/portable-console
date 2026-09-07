#!/usr/bin/env python3
"""console server: static portal + /api/* control surface (DESIGN.md §4/§5).

Single stdlib ThreadingHTTPServer replacing the legacy per-model control
servers. Read-only by design: it only execs scripts declared in the trusted
config; POSTs require X-Control-Token (constant-time compare against
data_dir/console.token, which this process never writes).
"""
from __future__ import annotations

import hmac
import json
import mimetypes
import os
import re
import sys
import traceback
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

BUNDLE_ROOT = Path(__file__).resolve().parents[1]

if __name__ == "__main__" and not __package__:  # direct run: python3 server/server.py
    sys.path.insert(0, str(BUNDLE_ROOT))  # standard __package__ bootstrap
    __package__ = "server"

from .control import ACTIONS, CardController, CardTimeoutError  # noqa: E402
TOKEN_NAME = "console.token"
DATA_FILES = frozenset({"health.json", "health.history.json", "usage-stats.json", "stats.json"})
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8090
_CST = timezone(timedelta(hours=8))
_STATUS_RE = re.compile(r"^/api/cards/([^/]+)/status/?$")
_ACTION_RE = re.compile(r"^/api/cards/([^/]+)/(" + "|".join(ACTIONS) + r")/?$")


def _host_from_header(host_header: str) -> str:
    """Bare hostname from a Host header, port stripped (IPv6 brackets kept)."""
    if host_header.startswith("["):
        return host_header.split("]", 1)[0] + "]"
    return host_header.rsplit(":", 1)[0] if host_header.count(":") == 1 else host_header


def _render_links(links: list[dict[str, Any]], host: str) -> list[dict[str, Any]]:
    rendered = []
    for link in links:
        item = dict(link)
        if isinstance(item.get("url"), str):
            item["url"] = item["url"].replace("{host}", host)
        rendered.append(item)
    return rendered


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        return os.path.commonpath([str(root), str(candidate)]) == str(root)
    except ValueError:
        return False


def _content_type(name: str) -> str:
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype == "application/json":
        ctype += "; charset=utf-8"
    return ctype


@dataclass(frozen=True)
class HandlerDeps:
    """Immutable wiring the request handler needs (one owner: build_server)."""

    cfg: dict[str, Any]
    controller: CardController
    web_root: Path
    data_dir: Path
    token_file: Path


def _make_handler(deps: HandlerDeps) -> type[BaseHTTPRequestHandler]:
    cfg, controller, web_root, data_dir, token_file = (
        deps.cfg, deps.controller, deps.web_root, deps.data_dir, deps.token_file)
    web_res, data_res = web_root.resolve(), data_dir.resolve()

    class Handler(BaseHTTPRequestHandler):
        protocol_version: str = "HTTP/1.1"
        server_version: str = "console-server/1.0"

        # -- plumbing -------------------------------------------------------
        def _send(self, code: int, body: str | bytes, ctype: str = "application/json; charset=utf-8") -> None:
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            _ = self.wfile.write(data)

        def _send_json(self, code: int, obj: Any) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False))

        def do_GET(self) -> None:  # noqa: N802
            self._guard(self._route_get)

        def do_POST(self) -> None:  # noqa: N802
            self._guard(self._route_post)

        def _guard(self, route: Callable[[], None]) -> None:
            try:
                route()
            except (BrokenPipeError, ConnectionResetError):
                pass  # client hung up; nothing to report
            except Exception:  # noqa: BLE001 — HTTP boundary: never kill the thread
                traceback.print_exc()
                if not getattr(self, "headers_sent", False):
                    self._send_json(500, {"error": "internal server error"})

        # -- /api/* ---------------------------------------------------------
        def _route_get(self) -> None:
            path = urllib.parse.urlsplit(self.path).path
            if path == "/api/cards":
                self._send_json(200, {"cards": controller.list_cards()})
            elif path == "/api/links":
                host = _host_from_header(self.headers.get("Host", ""))
                self._send_json(200, {"links": _render_links(cfg.get("links") or [], host)})
            elif (m := _STATUS_RE.match(path)):
                self._card_status(m.group(1))
            else:
                self._static(path)

        def _card_status(self, card_id: str) -> None:
            try:
                body = controller.status(card_id)
            except KeyError:
                self._send_json(404, {"error": f"unknown card '{card_id}'"})
                return
            except CardTimeoutError as exc:  # honest degrade, never an error page
                body = {"state": "unknown", "detail": str(exc)}
            except OSError as exc:  # script missing / not executable: honest degrade
                body = {"state": "unknown", "detail": f"status script failed: {exc}"}
            except ValueError as exc:  # no status script configured
                self._send_json(500, {"error": str(exc)})
                return
            body["updated_at"] = datetime.now(_CST).isoformat(timespec="seconds")
            self._send_json(200, body)

        def _route_post(self) -> None:
            path = urllib.parse.urlsplit(self.path).path
            m = _ACTION_RE.match(path)
            if not m:
                self._send_json(404, {"error": "not found"})
                return
            card_id, verb = m.group(1), m.group(2)
            if not self._authorized():
                return
            try:
                result = controller.action(card_id, verb)
            except KeyError:
                self._send_json(404, {"error": f"unknown card '{card_id}'"})
            except CardTimeoutError as exc:
                self._send_json(504, {"action": verb, "result": "failed", "steps": [str(exc)]})
            except (ValueError, OSError) as exc:  # no script / exec failure
                self._send_json(500, {"action": verb, "result": "failed",
                                      "steps": [f"{verb} failed: {exc}"]})
            else:
                self._send_json(200, result)

        def _authorized(self) -> bool:
            try:
                expected = token_file.read_text("utf-8").strip().encode()
            except OSError:
                expected = b""
            supplied = self.headers.get("X-Control-Token", "").encode()
            if not expected:
                self._send_json(503, {"error": "token file missing"})
                return False
            if not hmac.compare_digest(supplied, expected):
                self._send_json(403, {"error": "bad or missing X-Control-Token"})
                return False
            return True

        # -- static ---------------------------------------------------------
        def _static(self, url_path: str) -> None:
            rel = urllib.parse.unquote(url_path).lstrip("/") or "index.html"
            parts = PurePosixPath(rel).parts
            if not parts or ".." in parts or PurePosixPath(rel).is_absolute():
                self._send_json(403, {"error": "forbidden"})
                return
            candidates: list[tuple[Path, Path]] = [(web_res, web_root / rel)]
            name = parts[-1]
            if len(parts) == 1 and name in DATA_FILES:
                candidates.append((data_res, data_dir / name))
            elif parts[0] == "data" and len(parts) == 2 and name in DATA_FILES:
                candidates.append((data_res, data_dir / name))
            for base, cand in candidates:
                if not cand.is_file():
                    continue
                target = cand.resolve()
                if not _is_within(base, target):
                    continue
                self._send(200, target.read_bytes(), _content_type(target.name))
                return
            self._send_json(404, {"error": "not found"})

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — match base signature
            pass  # silence per-request noise

    return Handler


def _config_view(cfg: Any) -> dict[str, Any]:
    """Plain-dict view of either config shape.

    build_server accepts the server's native shape (raw JSON dict, used by
    tests and `_load_config`) *and* the daemon's typed ConsoleConfig dataclass
    — `console.py serve` forwards the loaded dataclass. Duck-typed via getattr
    so server/ still works when daemon/ is absent (DESIGN.md §1 layering).
    """
    if isinstance(cfg, dict):
        return cfg
    listen = getattr(cfg, "listen", None)
    return {
        "listen": {"host": getattr(listen, "host", None),
                   "port": getattr(listen, "port", None)},
        "data_dir": getattr(cfg, "data_dir", "data"),
        "cards": list(getattr(cfg, "cards", ()) or ()),
        "links": list(getattr(cfg, "links", ()) or ()),
    }


def build_server(cfg: Any, bundle_root: Path | str | None = None,
                 ) -> tuple[ThreadingHTTPServer, CardController]:
    """Assemble (server, controller) from config dict or ConsoleConfig; port 0 OK."""
    cfg = _config_view(cfg)
    root = Path(bundle_root) if bundle_root else BUNDLE_ROOT
    listen: dict[str, Any] = cfg.get("listen") or {}
    controller = CardController(cfg.get("cards") or [], root)
    data_dir = Path(str(cfg.get("data_dir") or "data"))
    if not data_dir.is_absolute():
        data_dir = root / data_dir
    deps = HandlerDeps(cfg=cfg, controller=controller, web_root=root / "web",
                       data_dir=data_dir, token_file=data_dir / TOKEN_NAME)
    httpd = ThreadingHTTPServer((str(listen.get("host") or DEFAULT_HOST),
                                 int(listen.get("port") or DEFAULT_PORT)),
                                _make_handler(deps))
    httpd.daemon_threads = True
    return httpd, controller


def _load_config() -> dict[str, Any]:
    """Load the bundle config as a dict (server's shape: raw JSON).

    daemon.config.load() validates the same file into a typed ConsoleConfig
    (it passes cards/links through opaquely — they are the server's sections),
    so when daemon/ is importable we fail fast on malformed config through it.
    When daemon/ is absent or unfinished, we honestly degrade to the plain
    JSON loader instead of crashing.
    """
    if str(BUNDLE_ROOT) not in sys.path:
        sys.path.insert(0, str(BUNDLE_ROOT))
    import importlib
    path = BUNDLE_ROOT / "console.config.json"
    if not path.is_file():
        path = BUNDLE_ROOT / "console.config.example.json"
    raw: dict[str, Any] = json.loads(path.read_text("utf-8")) if path.is_file() else {}
    try:
        daemon_config = importlib.import_module("daemon.config")
    except Exception as exc:  # noqa: BLE001 — bootstrap: daemon/ optional at boot
        print(f"[console] daemon.config unavailable ({exc!r}); raw JSON loader only",
              file=sys.stderr)
        return raw
    load = getattr(daemon_config, "load", None)
    if callable(load) and path.is_file():
        load(path)  # ConfigError propagates: malformed config must fail loudly
    return raw


def main(cfg: Any = None, bundle_root: Path | str | None = None) -> None:
    if cfg is None:
        cfg = _load_config()
    httpd, _ = build_server(cfg, bundle_root)
    host, port = httpd.server_address[0], httpd.server_address[1]
    print(f"[console] serving http://{host}:{port} (Ctrl-C to stop)", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[console] shutting down", file=sys.stderr)
    finally:
        httpd.server_close()


if __name__ == "__main__":
    argv = sys.argv[1:]
    root_arg = None
    if "--root" in argv:
        i = argv.index("--root")
        root_arg = Path(argv.pop(i + 1))
        _ = argv.pop(i)
    config = json.loads(Path(argv[0]).read_text("utf-8")) if argv else None
    main(config, bundle_root=root_arg)
