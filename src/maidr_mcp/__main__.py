"""Run the maidr MCP server: Streamable HTTP for claude.ai and ChatGPT, or stdio locally."""

from __future__ import annotations

import argparse
import os

import uvicorn
from starlette.middleware.cors import CORSMiddleware

from maidr_mcp.server import build_server


def http_app(host: str):
    """The Streamable HTTP app at ``/mcp``, stateless.

    One process holds every chart's relay queue, so serve it with a single worker. CORS is
    open because browser-based MCP clients, such as ext-apps' reference host, call it from
    another origin; nothing here relies on cookies.
    """
    app = build_server().streamable_http_app(stateless_http=True, host=host)
    return CORSMiddleware(
        app,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["Mcp-Session-Id"],
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
    args = parser.parse_args(argv)
    if args.stdio:
        build_server().run("stdio")
        return
    uvicorn.run(http_app(args.host), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
