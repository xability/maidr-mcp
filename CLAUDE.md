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
2. **maidr owns the chart.** The relay runs maidr's own WebMCP tools and returns their answers unchanged. It never reimplements them, and its model-facing tools keep maidr's names and arguments. The view never moves the reader's focus itself, and maidr decides when a move is announced: maidr moves their focus into the chart only when the model passes `focus: true`, because the reader asked to be taken there or for something now. The view's one exception is `update_chart`: a reader who was in the chart it replaces is put back in, on the new chart, since the old one took their focus with it.
3. **A `viewId` is the only key to a chart.** Nothing falls back to "the open chart": on a shared server that could be another reader's.
4. **What the model sees stays small.** The SVG travels in the result's `_meta`, for the view. `content` and `structuredContent` carry the `viewId` and a summary.
5. **Pin what the view loads.** maidr.js and the MCP Apps SDK come from jsDelivr at fixed versions (`server.py`). Raise them deliberately, and run `e2e/run.sh` when you do.

## Git

Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `ci:`, `chore:`), imperative mood, lower case. One logical change per commit. Work on a branch; `main` takes pull requests. The type decides the release, below.

## Release

`.github/workflows/release.yml` releases `main` with python-semantic-release (configured in `pyproject.toml`), Mondays at 17:00 UTC, an hour after py-maidr and two after maidr, or when run by hand.

- **What releases.** `feat:` raises the minor version, `fix:` and `perf:` the patch; other types release nothing. It stays 0.x: a breaking change raises the minor version too.
- **The gate.** Scheduled runs do nothing until the repository variable `RELEASE_ENABLED` is `true`, set once PyPI trusts the workflow: a tag pushed without its PyPI upload is never uploaded later. A run by hand always runs.
- **What a release does.** Tests and `e2e/run.sh` first. Then it stamps the version into `pyproject.toml`, `server.json` (three places) and `uv.lock`, writes `CHANGELOG.md`, commits, tags `vX.Y.Z` and makes the GitHub release. Then it publishes `maidr-mcp` to PyPI (trusted publishing), `ghcr.io/xability/maidr-mcp:X.Y.Z` and `:latest`, and, once both are out, `server.json` to the MCP Registry as `io.github.xability/maidr-mcp`. Never edit those versions or `CHANGELOG.md` by hand.
- **A failed publish.** Fix the cause and use "Re-run failed jobs" on that run. A new run finds the tag and releases nothing.
- **Pin updates release.** The maidr.js pin (`MAIDR_JS_VERSION`) ships inside the package and the image, so `update-maidr.yml` commits its bumps as `fix(deps): ...`: the next weekly run cuts a patch release that carries them, where a `chore:` bump would never reach users. py-maidr is not pinned for users: the package and the image install the newest `maidr>=1.26,<2`, so moving it in `uv.lock` changes what CI tests, not what users get.
- **The registry name is proven twice.** `<!-- mcp-name: io.github.xability/maidr-mcp -->` in `README.md` (PyPI's long description) and the `io.modelcontextprotocol.server.name` label in the `Dockerfile`. `tests/test_release.py` keeps both, and `server.json`'s versions, in step.
