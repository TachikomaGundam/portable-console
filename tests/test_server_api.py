"""Integration tests: real sockets against the ThreadingHTTPServer (port 0)."""
from __future__ import annotations

import http.client
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.server import build_server  # noqa: E402

TOKEN = "s3cret"


def write_script(root: Path, name: str, body: str) -> str:
    (root / "ctl").mkdir(exist_ok=True)
    path = root / "ctl" / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return f"ctl/{name}"


def make_cfg(root: Path) -> dict:
    return {
        "listen": {"host": "127.0.0.1", "port": 0},
        "data_dir": "data",
        "cards": [
            {"id": "ok", "name": "OK Card", "icon": "gpu", "timeout_s": 10,
             "scripts": {"status": write_script(root, "status_ok.sh",
                                                "echo '{\"state\":\"running\"}'"),
                         "start": write_script(root, "start_ok.sh", "echo started"),
                         "stop": write_script(root, "stop_ok.sh", "echo stopped")}},
            {"id": "slow", "timeout_s": 0.3,
             "scripts": {"status": write_script(root, "slow.sh", "sleep 5"),
                         "stop": "ctl/slow.sh"}},
            {"id": "failstop",
             "scripts": {"stop": write_script(root, "stop_fail.sh", "exit 1")}},
            {"id": "nostart",
             "scripts": {"status": "ctl/status_ok.sh"}},
        ],
        "links": [{"name": "Wiki", "url": "http://{host}:3000",
                   "icon": "book", "port_check": 3000}],
    }


def make_bundle(tmp_path: Path, *, with_token: bool = True) -> tuple[Path, dict]:
    root = tmp_path / "bundle"
    (root / "web").mkdir(parents=True)
    (root / "web" / "index.html").write_text("<h1>portal</h1>", encoding="utf-8")
    data = root / "data"
    data.mkdir()
    (data / "health.json").write_text('{"cpu_cores": 8}', encoding="utf-8")
    if with_token:
        token = data / "console.token"
        token.write_text(TOKEN + "\n", encoding="utf-8")
        token.chmod(0o600)
    return root, make_cfg(root)


def serve(root: Path, cfg: dict):
    httpd, _ = build_server(cfg, root)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread


@pytest.fixture()
def port(tmp_path: Path):
    root, cfg = make_bundle(tmp_path)
    httpd, thread = serve(root, cfg)
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


@pytest.fixture()
def port_notoken(tmp_path: Path):
    root, cfg = make_bundle(tmp_path, with_token=False)
    httpd, thread = serve(root, cfg)
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def req(p: int, method: str, path: str, token: str | None = None,
        host: str | None = None, raw_path: str | None = None) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", p, timeout=10)
    headers = {}
    if token is not None:
        headers["X-Control-Token"] = token
    if host is not None:
        headers["Host"] = host
    try:
        conn.request(method, raw_path or path, body=b"" if method == "POST" else None,
                     headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def jreq(p: int, *args, **kw) -> tuple[int, dict]:
    code, body = req(p, *args, **kw)
    return code, json.loads(body)


# ---- API surface -----------------------------------------------------------

def test_cards_list_no_auth(port):
    code, body = jreq(port, "GET", "/api/cards")
    assert code == 200
    assert {"id": "ok", "name": "OK Card", "icon": "gpu",
            "actions": ["start", "stop"]} in body["cards"]


def test_card_status_passthrough_with_updated_at(port):
    code, body = jreq(port, "GET", "/api/cards/ok/status")
    assert code == 200
    assert body["state"] == "running"
    assert body["updated_at"].endswith("+08:00")


def test_card_status_timeout_degrades_honestly(port):
    code, body = jreq(port, "GET", "/api/cards/slow/status")
    assert code == 200
    assert body["state"] == "unknown"
    assert "timed out" in body["detail"]


def test_card_status_unknown_404(port):
    assert jreq(port, "GET", "/api/cards/ghost/status")[0] == 404


def test_card_status_missing_script_500(port):
    code, body = jreq(port, "GET", "/api/cards/failstop/status")
    assert code == 500 and "no 'status' script" in body["error"]


def test_post_without_token_403(port):
    code, body = jreq(port, "POST", "/api/cards/ok/start")
    assert code == 403
    assert body == {"error": "bad or missing X-Control-Token"}


def test_post_wrong_token_403(port):
    code, body = jreq(port, "POST", "/api/cards/ok/start", token="wrong")
    assert code == 403
    assert body["error"] == "bad or missing X-Control-Token"


def test_post_with_token_200_steps_present(port):
    code, body = jreq(port, "POST", "/api/cards/ok/start", token=TOKEN)
    assert code == 200
    assert body["action"] == "start"
    assert body["result"] == "ok"
    assert body["steps"] and "start rc=0" in body["steps"][0]


def test_post_nonzero_rc_200_failed(port):
    code, body = jreq(port, "POST", "/api/cards/failstop/stop", token=TOKEN)
    assert code == 200
    assert body["result"] == "failed"


def test_post_unknown_card_404(port):
    assert jreq(port, "POST", "/api/cards/ghost/stop", token=TOKEN)[0] == 404


def test_post_timeout_504(port):
    code, body = jreq(port, "POST", "/api/cards/slow/stop", token=TOKEN)
    assert code == 504
    assert body["result"] == "failed"
    assert "timed out" in body["steps"][0]


def test_post_missing_script_500(port):
    code, body = jreq(port, "POST", "/api/cards/nostart/start", token=TOKEN)
    assert code == 500 and body["result"] == "failed"


def test_post_token_file_missing_503(port_notoken):
    code, body = jreq(port_notoken, "POST", "/api/cards/ok/start", token=TOKEN)
    assert code == 503
    assert body == {"error": "token file missing"}


def test_links_host_substitution_from_host_header(port):
    code, body = jreq(port, "GET", "/api/links", host="10.0.0.9:8090")
    assert code == 200
    link = body["links"][0]
    assert link["url"] == "http://10.0.0.9:3000"  # port stripped from Host, {host} replaced
    assert link["port_check"] == 3000  # other fields passed through


# ---- static serving --------------------------------------------------------

def test_root_serves_index(port):
    code, body = req(port, "GET", "/")
    assert code == 200 and b"<h1>portal</h1>" in body


def test_health_json_at_web_root(port):
    code, body = jreq(port, "GET", "/health.json")
    assert code == 200 and body == {"cpu_cores": 8}


def test_health_json_via_data_alias(port):
    assert jreq(port, "GET", "/data/health.json")[0] == 200


def test_token_file_never_served(port):
    assert req(port, "GET", "/console.token")[0] == 404
    assert req(port, "GET", "/data/console.token")[0] == 404


def test_static_traversal_rejected(port):
    for raw in ("/../../etc/passwd", "/%2e%2e/%2e%2e/etc/passwd", "/web/../../etc/passwd"):
        assert req(port, "GET", raw, raw_path=raw)[0] in (403, 404), raw


def test_unknown_path_404_json(port):
    code, body = jreq(port, "GET", "/nope.txt")
    assert code == 404 and "error" in body
