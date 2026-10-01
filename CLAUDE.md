# maidr-mcp — development guide

An MCP server that shows accessible maidr charts in ChatGPT and Claude conversations (MCP Apps), and relays the model's `maidr_*` tool calls to the chart, where maidr.js's own WebMCP tools answer them.

## Commands

```bash
uv sync                                   # install
uv run ruff check . && uv run ruff format --check .
uv run pytest                             # unit tests
bash e2e/run.sh                           # the whole loop in ext-apps' reference host
uv run maidr-mcp --port 8000              # HTTP at /mcp; --stdio for a local host
```

## Layout

```
src/maidr_mcp/
├─ charts.py     # chart specs (pydantic) -> matplotlib -> py-maidr SVG
├─ relay.py      # per-view queues: model call -> view poll -> view reply
├─ server.py     # MCPServer + Apps extension: tools, the view resource
├─ view.html     # the MCP App view: maidr.js, document.modelContext, relay loop
├─ access.py     # the optional access token: header or /mcp/<token>, kept out of logs
└─ __main__.py   # CLI: Streamable HTTP (stateless, CORS, optional token) or stdio
tests/            # pytest, in-process through mcp.Client
e2e/              # Playwright driver against ext-apps' basic-host
```

## Principles

1. **Accessibility is the product.** A change that shows the chart but drops an announcement, a braille update or a keyboard path is broken. `e2e/run.sh` is the check that says so.
2. **maidr owns the chart.** The relay runs maidr's own WebMCP tools and returns their answers unchanged. It never reimplements them, and its model-facing tools keep maidr's names and arguments. Never move the reader's focus; maidr decides when a move is announced.
3. **A `viewId` is the only key to a chart.** Nothing falls back to "the open chart": on a shared server that could be another reader's.
4. **What the model sees stays small.** The SVG travels in the result's `_meta`, for the view. `content` and `structuredContent` carry the `viewId` and a summary.
5. **Pin what the view loads.** maidr.js and the MCP Apps SDK come from jsDelivr at fixed versions (`server.py`). Raise them deliberately, and run `e2e/run.sh` when you do.

## Git

Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `ci:`, `chore:`), imperative mood, lower case. One logical change per commit. Work on a branch; `main` takes pull requests.
