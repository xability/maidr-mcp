"""An optional access token for the HTTP transport.

A request carries the token as ``Authorization: Bearer <token>`` on ``/mcp``, or in the
URL as ``/mcp/<token>``. The URL form is for hosts that take only a URL for a remote MCP
server, such as Claude's custom connectors and ChatGPT's developer-mode apps: there the URL
itself is the secret. This is one shared secret, not OAuth.

uvicorn's access log prints each request's path, so the URL form would write the token to
it. ``HideToken`` takes it out of uvicorn's log lines.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from pathlib import Path

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

ENDPOINT = "/mcp"
MIN_LENGTH = 16
HIDDEN = "<redacted>"
LOGGERS = ("uvicorn.access", "uvicorn.error")

# RFC 3986's unreserved characters: a token made of them stands in a URL path unchanged.
_URL_SAFE = re.compile(r"[A-Za-z0-9._~-]+")
_PATH_TOKEN = re.compile(r"(/mcp/)[^/?#\s\"]+")
_GENERATE = "python3 -c 'import secrets; print(secrets.token_urlsafe(32))'"


def read_token(token: str | None, token_file: str | None) -> str | None:
    """The access token given to the server, or None when there is none.

    Raises:
        ValueError: The token is given both ways, cannot be read, is shorter than
            ``MIN_LENGTH``, or holds a character that would not stand in a URL. The message
            never includes the token.
    """
    if token is not None and token_file is not None:
        raise ValueError(
            "give the access token one way: --token (or MAIDR_MCP_TOKEN), "
            "or --token-file (or MAIDR_MCP_TOKEN_FILE)"
        )
    if token_file is not None:
        try:
            token = Path(token_file).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as error:
            raise ValueError(f"cannot read the access token file: {error}") from None
    if token is None:
        return None
    if not token:  # set but empty: refuse rather than run open by mistake
        raise ValueError("the access token is empty; to run without one, do not set it at all")
    if len(token) < MIN_LENGTH:
        raise ValueError(
            f"the access token has {len(token)} characters; use at least {MIN_LENGTH}, "
            f"such as the output of: {_GENERATE}"
        )
    if not _URL_SAFE.fullmatch(token):
        raise ValueError(
            "the access token may hold only letters, digits and - . _ ~, so that it can stand "
            f"in a URL; generate one with: {_GENERATE}"
        )
    return token


def _digest(value: str) -> bytes:
    # Comparing digests of equal length keeps even the token's length out of the timing.
    return hashlib.sha256(value.encode("utf-8", "replace")).digest()


def _bearer(scope: Scope) -> str | None:
    for name, value in scope["headers"]:
        if name == b"authorization":
            scheme, _, credentials = value.decode("latin-1").partition(" ")
            return credentials.strip() if scheme.lower() == "bearer" else None
    return None


class TokenGate:
    """Lets a request through only when it carries the token.

    On ``/mcp/<token>`` the token is the URL's last segment, and the app is handed the
    request as plain ``/mcp``, so nothing behind the gate sees it. Anywhere else it is the
    ``Authorization: Bearer`` header. OPTIONS passes without it: browsers send CORS
    preflights without credentials, and probes use it to ask whether the server is up.
    Anything else gets 401 with a ``WWW-Authenticate`` challenge.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self._digest = _digest(token)

    def _is_token(self, candidate: str | None) -> bool:
        return candidate is not None and hmac.compare_digest(_digest(candidate), self._digest)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):  # lifespan
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        if path.startswith(ENDPOINT + "/"):
            candidate: str | None = path[len(ENDPOINT) + 1 :].removesuffix("/")
            if self._is_token(candidate):
                plain = dict(scope, path=ENDPOINT, raw_path=ENDPOINT.encode())
                await self.app(plain, receive, send)
                return
        else:
            candidate = _bearer(scope)
            if self._is_token(candidate):
                await self.app(scope, receive, send)
                return
        if scope["type"] == "http" and scope["method"] == "OPTIONS":
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await WebSocketClose(code=1008)(scope, receive, send)
            return
        challenge = 'Bearer realm="maidr-mcp"'
        if candidate is not None:
            challenge += ', error="invalid_token"'
        response = PlainTextResponse(
            "This maidr-mcp server needs its access token: send it as "
            "Authorization: Bearer <token>, or use the URL /mcp/<token>.\n",
            status_code=401,
            headers={"WWW-Authenticate": challenge},
        )
        await response(scope, receive, send)


class HideToken(logging.Filter):
    """Takes the token, and whatever follows ``/mcp/`` in a path, out of log records.

    Hiding every ``/mcp/<segment>``, not only the right token, keeps a near miss, such as
    the token with one character mistyped, out of the log too.
    """

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    def _hide(self, value: object) -> object:
        if not isinstance(value, str):
            return value
        return _PATH_TOKEN.sub(rf"\g<1>{HIDDEN}", value.replace(self._token, HIDDEN))

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._hide(record.msg)
        # uvicorn's access formatter unpacks the arguments, so they keep their shape.
        if isinstance(record.args, tuple):
            record.args = tuple(self._hide(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: self._hide(arg) for key, arg in record.args.items()}
        return True


def hide_token_in_logs(token: str) -> None:
    """Adds ``HideToken`` to uvicorn's loggers.

    Call it before ``uvicorn.run``: uvicorn sets its logging up as it starts, which replaces
    a logger's handlers but keeps its filters.
    """
    hide = HideToken(token)
    for name in LOGGERS:
        logging.getLogger(name).addFilter(hide)
