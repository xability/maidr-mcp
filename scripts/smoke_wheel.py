"""Runs an installed maidr-mcp the way the MCP Registry tells a host to: `maidr-mcp --stdio`.

ci.yml's `package` job installs the wheel `uv build` made, alone, and runs this, so a wheel
that leaves out a file the server needs, such as the chart view, fails before PyPI has it:

    uv build && uv run --no-project --isolated --with dist/*.whl python scripts/smoke_wheel.py
"""

from __future__ import annotations

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

CHART = {"type": "bar", "categories": ["a", "b"], "series": [{"values": [1, 2]}]}
VIEW_URI = "ui://maidr/chart.html"


async def main() -> None:
    server = StdioServerParameters(command="maidr-mcp", args=["--stdio"])
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        shown = await session.call_tool("show_chart", {"chart": CHART})
        assert not shown.is_error and shown.structured_content["viewId"], shown
        view = await session.read_resource(VIEW_URI)
        assert "maidr" in view.contents[0].text, "the chart view is not in the wheel"
    print("The installed wheel serves show_chart and its view over stdio.")


if __name__ == "__main__":
    anyio.run(main)
