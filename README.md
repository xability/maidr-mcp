# maidr-mcp

An [MCP](https://modelcontextprotocol.io) server that shows accessible [maidr](https://maidr.ai) charts inside ChatGPT and Claude conversations, and lets the conversation's model move the reader through them and run the chart's commands for them.

The model calls `show_chart` with the data, and the chart appears in the conversation as an [MCP App](https://modelcontextprotocol.io/extensions/apps). A blind or low-vision reader Tabs into it and explores it the way they explore any maidr chart: arrow keys, screen reader, sonification, braille. Three things then happen through the model:

- **The model can take the reader somewhere.** When the reader asks for "the highest bar", the model calls `maidr_get_layer_data` to find it and `maidr_navigate` to move there, and maidr announces the point by speech, braille and sound.
- **The model can press the chart's keys for the reader.** When the reader asks to turn braille off, play the chart, or jump to the lowest value, the model calls `maidr_list_commands` to find the command and the reader's current modes, and `maidr_run_command` to run it. maidr announces the result as if the reader had pressed the key, and the model tells them which key that was.
- **The model knows where the reader is.** As the reader moves, the chart tells the model their position, so "what is this point?" needs no tool call. Within a turn, `maidr_list_charts` reads it live.

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
5. maidr never moves the reader's focus. A move or command the model makes while the reader is outside the chart waits for them, and until they enter the chart, the view's status line says so: "The assistant has a move waiting for you: Tab into the chart to hear it."

## Tools

| Tool | Called by | Does |
| --- | --- | --- |
| `show_chart` | model | Draws the chart and shows it. Returns the `viewId` the other tools take. |
| `maidr_list_charts` | model | Returns the chart's layers, point counts, and, while the reader is in the chart, where they are, live. Silent. |
| `maidr_get_layer_data` | model | Returns a page of a layer's points, each with the `target` that `maidr_navigate` takes. Silent. |
| `maidr_navigate` | model | Moves the reader to a point and announces it. |
| `maidr_list_commands` | model | Returns the reader's commands, each with its id, title, keys, and whether the model can run it, and the reader's current modes (text, sound, braille, autoplay and more). Silent. |
| `maidr_run_command` | model | Runs one of the reader's commands, such as `toggle_braille`, `autoplay_forward` or `go_to_max_value`, as if they had pressed its keys, and announces the result. Commands that open a dialog or text field are the reader's own. |
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

What the server needs depends on how the host reaches it:

| Host | How it connects | What the server needs |
| --- | --- | --- |
| Claude (web, Desktop, mobile) | a custom connector, which takes a remote MCP server | a public HTTPS address |
| ChatGPT | a developer-mode app | a public HTTPS address, or [Secure MCP Tunnel](#chatgpt-without-a-public-address) to your own machine |
| ChatGPT desktop app | its own MCP servers, started over STDIO | nothing hosted; see [below](#chatgpt-without-a-public-address) for what is untested |

### With a public address

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

### ChatGPT without a public address

**Your own machine, through Secure MCP Tunnel.** ChatGPT calls an app's MCP server from OpenAI's side, so it cannot reach `localhost` directly. OpenAI's [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) connects it to a server that stays off the internet:
- **Server:** runs on your machine, either `maidr-mcp` (HTTP at `127.0.0.1:8000/mcp`) or `maidr-mcp --stdio`.
- **Tunnel client:** OpenAI's open-source client runs next to the server and opens only outbound HTTPS to OpenAI. It needs a `tunnel_id` from the Platform's tunnel settings and a runtime API key.
- **App:** you add it in developer mode through the tunnel, as that guide describes.
- **Uptime:** the machine has to stay on while the chart is in use.

**The ChatGPT desktop app's own MCP servers.** The [desktop app can start a local server itself](https://learn.chatgpt.com/docs/extend/mcp). Open Settings > MCP servers > Add server, choose STDIO, and give it this command, which needs [uv](https://docs.astral.sh/uv/):

```bash
uvx --from git+https://github.com/xability/maidr-mcp maidr-mcp --stdio
```

The desktop app shares this configuration with the Codex CLI and IDE extension. There are two caveats:
- Whether the app draws an MCP App's UI for a server added this way is not documented.
- This route has not been tried with maidr-mcp, so it is not known whether the chart appears.

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
- the model's calls are answered by maidr inside the chart, `maidr_list_commands` included;
- a move and a command made while the reader is in the chat wait, the chart's status line says so, and when the reader Tabs in, the move is announced, the command takes effect, and the notice goes;
- the arrow keys announce, and the reader's position reaches the host as model context;
- a move and commands made while the reader is in the chart take effect at once, and `maidr_list_charts` gives the model the position a command took the reader to;
- there are no console errors or CSP violations.

`MAIDR_JS_FILE=/path/to/maidr/dist/maidr.js bash e2e/run.sh` runs the same checks against a local build of maidr.js in place of the pinned release.

## Limits

- **The position the chart reports reaches the model with the reader's next message.** Hosts apply `ui/update-model-context` on the next turn, so within a turn the model context misses the model's own moves and commands, and the reader moving on while the model answers. `maidr_list_charts` reads the position live, and the server's instructions tell the model to call it when the reader asks about "this point" and the context may be stale. What remains: maidr knows the position only while the reader is in the chart, so while they are typing to the model the last report stands; and a model that skips the call answers from the context alone.
- **Moves and commands wait until the reader is in the chart.** maidr never moves focus, so a move or command made while the reader is typing to the model is kept, and happens when they Tab back into the chart: the move first, then the commands, half a second apart. The tool answers `applied: "on-next-focus"` and the model is told to say so; the chart's status line says so too, where a screen reader browsing the conversation finds it. What remains is that the reader has to go to the chart: nothing happens until they do, and at most 8 commands wait.
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
