"""maidr-mcp: accessible maidr charts in ChatGPT and Claude conversations, through MCP Apps."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("maidr-mcp")
except PackageNotFoundError:  # running from a source tree that is not installed
    __version__ = "0.0.0"
