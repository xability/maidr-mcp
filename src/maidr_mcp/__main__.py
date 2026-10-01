"""Run the maidr MCP server: Streamable HTTP for claude.ai and ChatGPT, or stdio locally."""

from __future__ import annotations

import argparse
import os
import sys

import uvicorn
from starlette.middleware.cors import CORSMiddleware

from maidr_mcp.access import TokenGate, hide_token_in_logs, read_token
from maidr_mcp.server import build_server

LOOPBACK = ("127.0.0.1", "localhost", "::1")


def http_app(host: str, token: str | None = None):
    """The Streamable HTTP app at ``/mcp``, stateless, behind the access token if one is set.

    One process holds every chart's relay queue, so serve it with a single worker. CORS is
    open because browser-based MCP clients, such as ext-apps' reference host, call it from
    another origin; nothing here relies on cookies. CORS sits outside the token, so a
    preflight is answered without it and a 401 carries the headers a browser needs to read it.
    """
    app = build_server().streamable_http_app(stateless_http=True, host=host)
    if token is not None:
        app = TokenGate(app, token)
    return CORSMiddleware(
        app,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["Mcp-Session-Id", "WWW-Authenticate"],
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="maidr-mcp", description=__doc__)
    parser.add_argument(
        "--stdio", action="store_true", help="serve over stdin and stdout instead of HTTP"
    )
    parser.add_argument(
        "--host", default=os.environ.get("HOST", "127.0.0.1"), help="HTTP host (default 127.0.0.1)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8000")),
        help="HTTP port (default 8000)",
    )
    # Never put the token's default in a help string: --help would print the secret.
    parser.add_argument(
        "--token",
        default=os.environ.get("MAIDR_MCP_TOKEN"),
        help="require this access token on every HTTP request, as a Bearer header or in the "
        "URL /mcp/<token> (default: $MAIDR_MCP_TOKEN). Other users of the machine can see "
        "it in the process list; prefer the variable or --token-file",
    )
    parser.add_argument(
        "--token-file",
        default=os.environ.get("MAIDR_MCP_TOKEN_FILE"),
        help="read the access token from this file (default: $MAIDR_MCP_TOKEN_FILE)",
    )
    args = parser.parse_args(argv)
    if args.stdio:  # the host started this process itself: there is no one else to keep out
        build_server().run("stdio")
        return
    try:
        token = read_token(args.token, args.token_file)
    except ValueError as error:
        parser.error(str(error))
    if token is not None:
        hide_token_in_logs(token)
        print(
            "maidr-mcp: every request needs the access token, as an Authorization: Bearer "
            "header or in the URL /mcp/<token>",
            file=sys.stderr,
        )
    elif args.host not in LOOPBACK:
        print(
            f"maidr-mcp: no access token, so anyone who can reach {args.host}:{args.port} can "
            "use this server; set MAIDR_MCP_TOKEN or --token-file to require one",
            file=sys.stderr,
        )
    uvicorn.run(http_app(args.host, token), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
