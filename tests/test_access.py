"""The optional access token on the HTTP transport, and keeping it out of the logs."""

from __future__ import annotations

import http.client
import json
import logging
import threading
import time

import pytest
import uvicorn
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from uvicorn.logging import AccessFormatter

from maidr_mcp import __main__ as cli
from maidr_mcp.access import LOGGERS, HideToken, hide_token_in_logs, read_token

TOKEN = "Zr7_kq2-XbW9.vN4~pL0sT8yH"
HOST = "127.0.0.1"
MCP = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


@pytest.fixture(autouse=True)
def no_token_in_the_environment(monkeypatch):
    monkeypatch.delenv("MAIDR_MCP_TOKEN", raising=False)
    monkeypatch.delenv("MAIDR_MCP_TOKEN_FILE", raising=False)


@pytest.fixture
def uvicorn_loggers():
    """Puts uvicorn's loggers back as they were: hiding the token adds filters to them."""
    loggers = [logging.getLogger(name) for name in LOGGERS]
    saved = [(logger.filters[:], logger.handlers[:]) for logger in loggers]
    yield
    for logger, (filters, handlers) in zip(loggers, saved, strict=True):
        logger.filters[:] = filters
        logger.handlers[:] = handlers


def client(token: str | None = None) -> TestClient:
    return TestClient(cli.http_app(HOST, token), base_url=f"http://{HOST}:8000")


def tools(response) -> list[str]:
    assert response.status_code == 200, response.text
    line = next(line for line in response.text.splitlines() if line.startswith("data: "))
    return [tool["name"] for tool in json.loads(line[6:])["result"]["tools"]]


def test_without_a_token_the_server_answers_as_before():
    with client() as c:
        assert "show_chart" in tools(c.post("/mcp", json=LIST_TOOLS, headers=MCP))
        # The URL form does not exist without a token.
        assert c.post(f"/mcp/{TOKEN}", json=LIST_TOOLS, headers=MCP).status_code == 404


def test_the_token_in_a_bearer_header_lets_a_request_through():
    with client(TOKEN) as c:
        for scheme in ("Bearer", "bearer"):  # the scheme is case-insensitive
            headers = {**MCP, "authorization": f"{scheme} {TOKEN}"}
            assert "show_chart" in tools(c.post("/mcp", json=LIST_TOOLS, headers=headers))


def test_the_token_in_the_url_lets_a_request_through():
    with client(TOKEN) as c:
        assert "show_chart" in tools(c.post(f"/mcp/{TOKEN}", json=LIST_TOOLS, headers=MCP))
        assert "show_chart" in tools(c.post(f"/mcp/{TOKEN}/", json=LIST_TOOLS, headers=MCP))


def test_a_chart_drawn_through_the_url_form_answers_the_model():
    chart = {"type": "bar", "categories": ["a", "b"], "series": [{"values": [1, 2]}]}
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "show_chart", "arguments": {"chart": chart}},
    }
    with client(TOKEN) as c:
        response = c.post(f"/mcp/{TOKEN}", json=call, headers=MCP)
    assert response.status_code == 200
    assert '"viewId"' in response.text


@pytest.mark.parametrize(
    ("path", "headers", "invalid"),
    [
        ("/mcp", {}, False),
        ("/mcp", {"authorization": "Bearer not-the-token-at-all"}, True),
        ("/mcp", {"authorization": f"Bearer {TOKEN[:-1]}"}, True),
        ("/mcp", {"authorization": f"Bearer {TOKEN}x"}, True),
        ("/mcp", {"authorization": f"Basic {TOKEN}"}, False),
        (f"/mcp/{TOKEN[:-1]}", {}, True),
        (f"/mcp/{TOKEN}x", {}, True),
        (f"/mcp/{TOKEN.upper()}", {}, True),
        ("/mcp/", {}, True),
        # The URL form takes the token from the URL only.
        ("/mcp/wrong", {"authorization": f"Bearer {TOKEN}"}, True),
        (f"/{TOKEN}", {}, False),
        ("/", {}, False),
    ],
)
def test_a_request_without_the_token_gets_401_and_a_challenge(path, headers, invalid):
    browser = {"origin": "http://localhost:8080"}
    with client(TOKEN) as c:
        response = c.post(path, json=LIST_TOOLS, headers={**MCP, **browser, **headers})
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith('Bearer realm="maidr-mcp"')
    assert ('error="invalid_token"' in challenge) is invalid
    assert TOKEN not in response.text
    # A browser client can read the 401 and its challenge.
    assert response.headers["access-control-allow-origin"] == "*"
    assert "www-authenticate" in response.headers["access-control-expose-headers"].lower()


@pytest.mark.parametrize("path", ["/mcp", f"/mcp/{TOKEN}", "/mcp/anything"])
def test_a_cors_preflight_needs_no_token(path):
    preflight = {
        "origin": "http://localhost:8080",
        "access-control-request-method": "POST",
        "access-control-request-headers": "authorization,content-type",
    }
    with client(TOKEN) as c:
        response = c.options(path, headers=preflight)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    assert "authorization" in response.headers["access-control-allow-headers"]


def test_a_plain_options_probe_needs_no_token():
    # e2e/run.sh and the Docker check ask whether the server is up this way.
    with client() as open_:
        expected = open_.options("/mcp").status_code
    with client(TOKEN) as c:
        assert c.options("/mcp").status_code == expected != 401


def test_a_websocket_without_the_token_is_closed_as_a_policy_violation():
    with client(TOKEN) as c, pytest.raises(WebSocketDisconnect) as closed:
        with c.websocket_connect("/mcp"):
            pass
    assert closed.value.code == 1008


@pytest.mark.parametrize("token", [TOKEN, f"  {TOKEN}\n"])
def test_the_token_is_read_from_a_file(tmp_path, token):
    path = tmp_path / "token"
    path.write_text(token)
    assert read_token(None, str(path)) == TOKEN


@pytest.mark.parametrize(
    ("token", "token_file", "message"),
    [
        (TOKEN, "/some/file", "one way"),
        (None, "/no/such/file", "cannot read"),
        ("", None, "empty"),
        ("short-token", None, "at least 16"),
        ("a token with spaces", None, "letters, digits"),
        ("a/token/with/slashes", None, "letters, digits"),
        ("ä-token-with-umlauts", None, "letters, digits"),
    ],
)
def test_a_token_that_cannot_be_used_is_refused(token, token_file, message):
    with pytest.raises(ValueError, match=message) as refused:
        read_token(token, token_file)
    if token:
        assert token not in str(refused.value)


def test_no_token_is_none():
    assert read_token(None, None) is None


def _run_main(monkeypatch, argv: list[str]) -> dict:
    ran: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: ran.update(app=app, **options))
    cli.main(argv)
    return ran


def test_main_takes_the_token_from_the_environment(monkeypatch, uvicorn_loggers, capsys):
    monkeypatch.setenv("MAIDR_MCP_TOKEN", TOKEN)
    ran = _run_main(monkeypatch, ["--port", "8000"])
    with TestClient(ran["app"], base_url=f"http://{HOST}:8000") as c:
        assert c.post("/mcp", json=LIST_TOOLS, headers=MCP).status_code == 401
        assert "show_chart" in tools(c.post(f"/mcp/{TOKEN}", json=LIST_TOOLS, headers=MCP))
    for name in LOGGERS:
        assert any(isinstance(f, HideToken) for f in logging.getLogger(name).filters), name
    assert TOKEN not in capsys.readouterr().err


def test_main_takes_the_token_from_a_file(monkeypatch, uvicorn_loggers, tmp_path):
    path = tmp_path / "token"
    path.write_text(TOKEN + "\n")
    monkeypatch.setenv("MAIDR_MCP_TOKEN_FILE", str(path))
    ran = _run_main(monkeypatch, [])
    with TestClient(ran["app"], base_url=f"http://{HOST}:8000") as c:
        assert "show_chart" in tools(c.post(f"/mcp/{TOKEN}", json=LIST_TOOLS, headers=MCP))


def test_main_refuses_a_weak_token_without_printing_it(monkeypatch, capsys):
    with pytest.raises(SystemExit) as exited:
        _run_main(monkeypatch, ["--token", "hunter2"])
    assert exited.value.code == 2
    assert "hunter2" not in capsys.readouterr().err


def test_main_warns_when_a_public_server_has_no_token(monkeypatch, capsys):
    ran = _run_main(monkeypatch, ["--host", "0.0.0.0"])
    assert ran["host"] == "0.0.0.0"
    assert "no access token" in capsys.readouterr().err


def test_stdio_takes_no_token(monkeypatch):
    served = []

    class Server:
        def run(self, transport):
            served.append(transport)

    monkeypatch.setattr(cli, "build_server", lambda: Server())
    cli.main(["--stdio", "--token", "short"])  # not read, so not refused
    assert served == ["stdio"]


def test_hide_token_hides_the_token_and_any_path_after_mcp():
    hide = HideToken(TOKEN)
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "POST", f"/mcp/{TOKEN[:-1]}?t={TOKEN}", "1.1", 401),
        None,
    )
    assert hide.filter(record)
    assert record.args[2] == "/mcp/<redacted>?t=<redacted>"
    assert record.args[4] == 401


def test_the_access_log_never_shows_the_token(uvicorn_loggers):
    """A real uvicorn server, with its own logging set up after the filter, as main does."""
    hide_token_in_logs(TOKEN)
    config = uvicorn.Config(cli.http_app(HOST, TOKEN), host=HOST, port=0)  # sets up logging
    server = uvicorn.Server(config)
    lines: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record):
            lines.append(self.format(record))

    collect = Collect()
    collect.setFormatter(AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s'))
    logging.getLogger("uvicorn.access").addHandler(collect)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started:
            assert time.monotonic() < deadline, "the server did not start"
            time.sleep(0.05)
        port = server.servers[0].sockets[0].getsockname()[1]
        statuses = {}
        for path, headers in [
            (f"/mcp/{TOKEN}", {}),
            (f"/mcp/{TOKEN[:-1]}", {}),
            (f"/mcp?token={TOKEN}", {}),
            ("/mcp", {"authorization": f"Bearer {TOKEN}"}),
        ]:
            connection = http.client.HTTPConnection(HOST, port, timeout=10)
            connection.request("POST", path, json.dumps(LIST_TOOLS), {**MCP, **headers})
            statuses[path] = connection.getresponse().status
            connection.close()
    finally:
        server.should_exit = True
        thread.join(15)

    assert list(statuses.values()) == [200, 401, 401, 200]
    assert len(lines) == 4, lines
    for line in lines:
        assert TOKEN not in line and TOKEN[:-1] not in line, line
    assert 'POST /mcp/<redacted> HTTP/1.1" 200' in lines[0]
    assert 'POST /mcp/<redacted> HTTP/1.1" 401' in lines[1]
    assert "/mcp?token=<redacted>" in lines[2]
