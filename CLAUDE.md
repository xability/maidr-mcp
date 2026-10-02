# maidr-mcp — development guide

An MCP server that shows accessible maidr charts in ChatGPT and Claude conversations (MCP Apps), and relays the model's `maidr_*` tool calls to the chart, where maidr.js's own WebMCP tools answer them.

## Commands

```bash
uv sync                                   # install
uv run ruff check . && uv run ruff format --check .
uv run pytest                             # unit tests
bash e2e/run.sh                           # the whole loop in ext-apps' reference host
uv run maidr-mcp --port 8000              # HTTP at /mcp; --stdio for a local host
uv run --no-project python scripts/update_maidr.py update   # raise maidr.js and py-maidr to the newest
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
scripts/          # update_maidr.py: raises the two maidr pins, for update-maidr.yml
```

## Principles

1. **Accessibility is the product.** A change that shows the chart but drops an announcement, a braille update or a keyboard path is broken. `e2e/run.sh` is the check that says so.
2. **maidr owns the chart.** The relay runs maidr's own WebMCP tools and returns their answers unchanged. It never reimplements them, and its model-facing tools keep maidr's names and arguments. The view never moves the reader's focus itself, and maidr decides when a move is announced: maidr moves their focus into the chart only when the model passes `focus: true`, because the reader asked to be taken there or for something now. The view's one exception is `update_chart`: a reader who was in the chart it replaces is put back in, on the new chart, since the old one took their focus with it.
3. **A `viewId` is the only key to a chart.** Nothing falls back to "the open chart": on a shared server that could be another reader's.
4. **What the model sees stays small.** The SVG travels in the result's `_meta`, for the view. `content` and `structuredContent` carry the `viewId` and a summary.
5. **Pin what the view loads.** maidr.js and the MCP Apps SDK come from jsDelivr at fixed versions (`server.py`). `.github/workflows/update-maidr.yml` raises the maidr.js pin, and py-maidr in `uv.lock`, within an hour of a release: it commits to `main` only after ruff, pytest and `e2e/run.sh` pass against the new release, and opens an issue when they do not. It always pairs the newest of both, so a maidr.js that fails holds py-maidr back with it until someone raises py-maidr alone, by running the workflow with the current maidr.js. A version other than the newest is still set by hand, with the workflow's `maidr_js_version` input or `scripts/update_maidr.py`, and holding an older one means disabling the workflow, which otherwise raises it again within the hour. The MCP Apps SDK is raised by hand; run `e2e/run.sh` when you raise it.

## Git

Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `ci:`, `chore:`), imperative mood, lower case. One logical change per commit. Work on a branch; `main` takes pull requests, and the `chore:` commits `update-maidr.yml` pushes once its checks pass.
