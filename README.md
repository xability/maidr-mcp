# maidr-mcp

An [MCP](https://modelcontextprotocol.io) server that shows accessible [maidr](https://maidr.ai) charts inside ChatGPT and Claude conversations, and lets the conversation's model move the reader through them.

The model calls `show_chart` with the data, and the chart appears in the conversation as an [MCP App](https://modelcontextprotocol.io/extensions/apps). A blind or low-vision reader Tabs into it and explores it the way they explore any maidr chart: arrow keys, screen reader, sonification, braille. Two things then happen through the model:

- **The model can take the reader somewhere.** When the reader asks for "the highest bar", the model calls `maidr_get_layer_data` to find it and `maidr_navigate` to move there, and maidr announces the point by speech, braille and sound.
- **The model knows where the reader is.** As the reader moves, the chart tells the model their position, so "what is this point?" needs no tool call.

> **Experimental.** Checked end to end in the MCP Apps SDK's reference host ([below](#try-it-in-the-reference-host)). Not yet checked in claude.ai or ChatGPT.

## How it works

1. `show_chart` draws the chart on the server with matplotlib and [py-maidr](https://github.com/xability/py-maidr), so the model sends numbers rather than an SVG. The SVG reaches the chart view in the tool result's `_meta`, which is not added to the model's context.
2. The view loads [maidr.js](https://github.com/xability/maidr) and the MCP Apps SDK from `cdn.jsdelivr.net`, the only domain its CSP declares, at pinned versions.
3. maidr.js registers its own [WebMCP tools](https://github.com/xability/maidr/blob/main/docs/WEBMCP.md) on `document.modelContext`. No host hands a view's tools to the model yet ([ext-apps#797](https://github.com/modelcontextprotocol/ext-apps/issues/797)), so the view supplies `document.modelContext` itself. Each server tool of the same name relays its call to the view that is open:
   - the model calls the server tool;
   - the view long-polls an app-only tool, picks up the call, runs maidr's tool, and posts maidr's answer back;
   - that answer becomes the model's tool result.

   If hosts adopt WebMCP for MCP Apps, as [ext-apps#798](https://github.com/modelcontextprotocol/ext-apps/pull/798) proposes, maidr's tools reach the model directly and the relay can go.
4. On each key the reader presses in the chart, the view sends their position with `ui/update-model-context`.

## Tools

| Tool | Called by | Does |
| --- | --- | --- |
| `show_chart` | model | Draws the chart and shows it. Returns the `viewId` the other tools take. |
| `maidr_list_charts` | model | Returns the chart's layers, point counts, and where the reader is. Silent. |
| `maidr_get_layer_data` | model | Returns a page of a layer's points, each with the `target` that `maidr_navigate` takes. Silent. |
| `maidr_navigate` | model | Moves the reader to a point and announces it. |
| `maidr_view_poll`, `maidr_view_reply`, `maidr_view_svg` | the chart view only | Carry the relay, and the SVG for hosts that drop `_meta`. |

`show_chart` takes one of these chart types. Each maps onto the maidr layer type shown:

| `type` | Fields | maidr layer |
| --- | --- | --- |
| `bar` | `categories`, `series` (one series, or several side by side, or `stacked`) | `bar`, `dodged_bar`, `stacked_bar` |
| `line` | `x` (numbers or labels), `series` | `line` |
| `scatter` | `x`, `y` | `point` |
| `histogram` | `values`, `bins` | `hist` |
| `box` | `groups` | `box` |
| `heatmap` | `x_labels`, `y_labels`, `values`, `z_label` | `heat` |

Every type also takes `title`, `x_label` and `y_label`.

## Use it

Run the server, then add its URL to Claude or ChatGPT. Both need a public HTTPS address; they cannot reach `localhost`.

```bash
uvx --from git+https://github.com/xability/maidr-mcp maidr-mcp --host 0.0.0.0 --port 8000
# or
docker build -t maidr-mcp . && docker run -p 8000:8000 maidr-mcp
```

The endpoint is `/mcp`, over Streamable HTTP.
- **Run one instance.** The relay keeps each chart's queue in memory.
- **Restarts are tolerated.** An open chart registers itself again on its next poll after a restart.

**Claude.** Add a custom connector: Customize > Connectors > Add custom connector, with `https://<your-host>/mcp`.
- **Plans:** custom connectors work on every plan; Free allows one.
- **Team and Enterprise:** an Owner adds it.
- **Where charts render:** on web, Desktop, and iOS/Android once the connector is added.

**ChatGPT.** Turn on developer mode, then create an app for `https://<your-host>/mcp`. Developer mode is available on the web for Plus, Pro, Business, Enterprise and Education accounts.

## Try it in the reference host

`e2e/run.sh` checks the whole loop without a ChatGPT or Claude account. It does three things:
1. Builds the reference host from [ext-apps](https://github.com/modelcontextprotocol/ext-apps) (`examples/basic-host`, at tag `v2.0.3`).
2. Starts this server.
3. Drives both with Playwright the way a model and a reader would.

```bash
cd e2e && npm install && npx playwright-core install chromium && cd ..
bash e2e/run.sh
```

It checks:
- the chart appears;
- the model's calls are answered by maidr inside the chart;
- a move made while the reader is in the chat waits, and is announced when they Tab in;
- the arrow keys announce, and the reader's position reaches the host as model context;
- a move made while the reader is in the chart is announced at once;
- there are no console errors or CSP violations.

## Limits

- **The model learns the reader's position one turn late.** It sees the position on its next turn, not in the middle of one.
- **Moves wait until the reader is in the chart.** maidr never moves focus. A move made while the reader is typing to the model is kept, and announced when they Tab back into the chart. `maidr_navigate` says so (`applied: "on-next-focus"`), and the model should tell them.
- **Each `show_chart` call adds a chart.** Claude mounts a new view for every call, and earlier views stay.
- **The relay keeps a request open.** The view's poll holds a request for up to 20 seconds. Whether a given host limits calls made from a view is not yet known.
- **There is no authentication.** Anyone with the server's URL can draw charts. A chart can only be read or driven with its `viewId`, a random 24-character token.
- **Six chart families so far.** The server does not take plotting code.

## Network and data

- **The server:** receives the data the model sends to draw a chart. It keeps the chart's SVG in memory until the chart has gone 15 minutes without polling, and logs nothing about the data beyond the HTTP access log.
- **The chart view:** loads maidr.js and the MCP Apps SDK from `cdn.jsdelivr.net`. maidr's own AI chat inside the chart works as it does anywhere else, with a key the reader adds.

## Development

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest
bash e2e/run.sh
```

## License

GPL-3.0-or-later, like the rest of maidr.
