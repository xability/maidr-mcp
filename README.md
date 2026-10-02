# maidr-mcp

An [MCP](https://modelcontextprotocol.io) server that shows accessible [maidr](https://maidr.ai) charts inside ChatGPT and Claude conversations, and lets the conversation's model move the reader through them and run the chart's commands for them.

The model calls `show_chart` with the data, and the chart appears in the conversation as an [MCP App](https://modelcontextprotocol.io/extensions/apps). A blind or low-vision reader Tabs into it and explores it the way they explore any maidr chart: arrow keys, screen reader, sonification, braille. Three things then happen through the model:

- **The model can take the reader somewhere.** When the reader asks for "the highest bar", the model calls `maidr_get_layer_data` to find it and `maidr_navigate` to move there, and maidr announces the point by speech, braille and sound. When the reader is typing to the model rather than in the chart, it passes `focus: true`, since they asked to be taken there, and maidr moves their keyboard focus into the chart to make the move.
- **The model can press the chart's keys for the reader.** When the reader asks to turn braille off, play the chart, or jump to the lowest value, the model calls `maidr_list_commands` to find the command and the reader's current modes, and `maidr_run_command` to run it. maidr announces the result as if the reader had pressed the key, and the model tells them which key that was.
- **The model knows where the reader is.** As the reader moves, the chart tells the model their position, so "what is this point?" needs no tool call. Within a turn, `maidr_list_charts` reads it live.

> **Experimental.** Checked end to end in the MCP Apps SDK's reference host ([below](#try-it-in-the-reference-host)). Not yet checked in claude.ai or ChatGPT.

## How it works

1. `show_chart` draws the chart on the server with matplotlib and [py-maidr](https://github.com/xability/py-maidr), so the model sends numbers rather than an SVG. The SVG reaches the chart view in the tool result's `_meta`, which is not added to the model's context.
2. The view loads [maidr.js](https://github.com/xability/maidr) and the MCP Apps SDK from `cdn.jsdelivr.net`, the only domain its CSP declares, at pinned versions.
3. maidr.js registers its own [WebMCP tools](https://github.com/xability/maidr/blob/main/docs/WEBMCP.md) on `document.modelContext`. No host hands a view's tools to the model yet ([ext-apps#797](https://github.com/modelcontextprotocol/ext-apps/issues/797)), so the view supplies `document.modelContext` itself. Each server tool of the same name relays its call to the view that is open:
   - the model calls the server tool;
   - the view polls an app-only tool, picks up the call, runs maidr's tool, and posts maidr's answer back;
   - that answer becomes the model's tool result.

   The poll is a long poll: the server holds it for up to 20 seconds until a call comes, so the call reaches the chart at once. If the host cuts or refuses a request held that long, the view falls back to short polls, which the server answers at once, one every 2 seconds, so the model's call is still answered within the 10 seconds the server waits for the chart. The view tries a long poll again after a minute, and waits twice as long after each one the host cuts.

   If hosts adopt WebMCP for MCP Apps, as [ext-apps#798](https://github.com/modelcontextprotocol/ext-apps/pull/798) proposes, maidr's tools reach the model directly and the relay can go.
4. On each key the reader presses in the chart, the view sends their position with `ui/update-model-context`.
5. `update_chart` draws a new chart for a `viewId` and relays it to that view, which swaps it in place: maidr lets go of the old chart, with any move or command still waiting for the reader in it, and binds the new one. When something was waiting, the chart's status line says it went, and `update_chart` tells the model so it can take back what it promised the reader. The server keeps the latest chart, so a view that missed the swap, out of view or mounted again by the host, catches up on its next poll.
6. The view never moves the reader's focus itself, except to put a reader back into the chart `update_chart` replaced. maidr moves it into the chart only when the model passes `focus: true`, which the server's instructions and the tools' descriptions tell it to do only when the reader asked to be taken somewhere or for something now, and never because they left the chart. Any other move or command the model makes while the reader is outside the chart waits for them, as does one with `focus: true` that could not take them in (`focused: false`). Until they enter the chart, the view's status line says so: "The assistant has a move waiting for you: Tab into the chart to hear it." After `update_chart` the same line says "Chart updated: <title>", until the reader enters the chart, by Tab or with `focus: true`; when a move or command waits as well, it says both.

## Tools

| Tool | Called by | Does |
| --- | --- | --- |
| `show_chart` | model | Draws the chart and shows it. Returns the `viewId` the other tools take. |
| `update_chart` | model | Draws a new chart and puts it in place of the one in that `viewId`'s view. Adds no view. A reader in the chart stays in it, on the new chart; anyone else keeps their focus. Both are told it changed. |
| `maidr_list_charts` | model | Returns the chart's layers, point counts, and, while the reader is in the chart, where they are, live. Silent. |
| `maidr_get_layer_data` | model | Returns a page of a layer's points, each with the `target` that `maidr_navigate` takes. Silent. |
| `maidr_navigate` | model | Moves the reader to a point and announces it. With `focus: true`, for a reader who asked to be taken there, maidr first moves their keyboard focus into the chart, and answers `focused: true`, or `focused: false` when it could not, leaving the move to wait for them. |
| `maidr_list_commands` | model | Returns the reader's commands, each with its id, title, keys, and whether the model can run it, and the reader's current modes (text, sound, braille, autoplay and more). Silent. |
| `maidr_run_command` | model | Runs one of the reader's commands, such as `toggle_braille`, `autoplay_forward` or `go_to_max_value`, as if they had pressed its keys, and announces the result. Commands that open a dialog or text field are the reader's own. With `focus: true`, for a reader who asked for it now, maidr first moves their keyboard focus into the chart, and the command runs in turn half a second after they hear where they are (`applied: "queued"`, `focused: true`), or answers `focused: false` when it could not, leaving the command to wait for them. |
| `maidr_view_poll`, `maidr_view_reply`, `maidr_view_svg` | the chart view only | Carry the relay, and the SVG for `update_chart` and for hosts that drop `_meta`. |

`show_chart` and `update_chart` take one of these chart types, the ones py-maidr marks [stable](https://py.maidr.ai/stability.html). Each maps onto the maidr layer type shown:

| `type` | Fields | maidr layer | The model can move the reader there |
| --- | --- | --- | --- |
| `bar` | `categories`, `series` (one series, or several side by side, or `stacked`) | `bar`, `dodged_bar`, `stacked_bar` | yes |
| `line` | `x` (numbers or labels), `series` | `line` | yes |
| `step` | `x`, `series`, `where` (`post`, `pre` or `mid`) | `step` | yes |
| `scatter` | `x`, `y`, and `trend` for a least-squares line | `point`, and `smooth` for the trend line | to the points, not the line |
| `histogram` | `values`, `bins` | `hist` | yes |
| `box` | `groups` | `box` | no |
| `violin` | `groups` | `violin_box`, `violin_kde` | no |
| `heatmap` | `x_labels`, `y_labels`, `values`, `z_label` | `heat` | yes |
| `pie` | `categories`, `values` (read clockwise from 12 o'clock) | `pie` | no |
| `candlestick` | `dates`, `open`, `high`, `low`, `close` | `candlestick` | no |

Every type also takes `title`, `x_label` and `y_label`. A pie has no axes, so there they name what the slices are and what the values measure.

The model reads every layer's points with `maidr_get_layer_data`. Where the last column says no, maidr gives the points no `target`: the reader moves through them with the keys, and `maidr_navigate` answers `layer not navigable`.

## Use it

What the server needs depends on how the host reaches it:

| Host | How it connects | What the server needs |
| --- | --- | --- |
| Claude (web, Desktop, mobile) | a custom connector, which takes a remote MCP server | a public HTTPS address |
| ChatGPT | a developer-mode app | a public HTTPS address, or [Secure MCP Tunnel](#chatgpt-without-a-public-address) to your own machine |
| ChatGPT desktop app | its own MCP servers, started over STDIO | nothing hosted; see [below](#chatgpt-without-a-public-address) for what is untested |

### With a public address

```bash
# Make a token once, and keep it: it is the <token> in each host's URL below.
export MAIDR_MCP_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
uvx --from git+https://github.com/xability/maidr-mcp maidr-mcp --host 0.0.0.0 --port 8000
# or
docker build -t maidr-mcp . && docker run -p 8000:8000 -e MAIDR_MCP_TOKEN maidr-mcp
```

The endpoint is `/mcp`, over Streamable HTTP, and with a token also `/mcp/<token>`.
- **Run one instance.** The relay keeps each chart's queue in memory.
- **Restarts are tolerated.** An open chart registers itself again on its next poll after a restart.

**Claude.** Add a custom connector: Customize > Connectors > Add custom connector, with `https://<your-host>/mcp/<token>`.
- **Plans:** custom connectors work on every plan; Free allows one.
- **Team and Enterprise:** an Owner adds it.
- **Where charts render:** on web, Desktop, and iOS/Android once the connector is added.

**ChatGPT.** Turn on developer mode, then create an app for `https://<your-host>/mcp/<token>`. Developer mode is available on the web for Plus, Pro, Business, Enterprise and Education accounts.

### An access token

Without a token, anyone who can reach the server can draw charts. With one, every HTTP request but OPTIONS (CORS preflights, probes) has to carry it, and any other gets `401`. Set it with `MAIDR_MCP_TOKEN`, with `--token-file` or `MAIDR_MCP_TOKEN_FILE` naming a file that holds just the token, or with `--token`. Over STDIO there is none: the host starts the server itself.

A request carries the token one of two ways:
- **In the URL,** as `/mcp/<token>`. Claude's custom connectors and ChatGPT's developer-mode apps take a URL and nothing else short of OAuth, so this is the form for them.
- **In a header,** as `Authorization: Bearer <token>` on `/mcp`, for clients that can send one.

`python3 -c 'import secrets; print(secrets.token_urlsafe(32))'` makes a good one. The server refuses a token shorter than 16 characters, or one holding anything but letters, digits and `-._~`.

- **The URL is the secret.** It lives in the host's connector or app settings, and anyone who sees it there can use the server.
- **Rotate it by restarting** the server with a new token, then give each host the new URL. The old one stops working at once.
- **It is one shared token, not OAuth.** Everyone given the URL shares it, and it cannot be taken back from one of them alone. Full OAuth is not implemented.
- **Logs.** The server's access log shows the URL as `/mcp/<redacted>`. A proxy, load balancer or platform in front of the server may log the full URL; use the header where the client can send one.
- **`--token` shows in the process list** to other users of the machine. Prefer the variable, or a file: with Docker secrets, `-e MAIDR_MCP_TOKEN_FILE=/run/secrets/<name>`.
- **Serve it over HTTPS.** Over plain HTTP the token crosses the network in the clear.

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

With `MAIDR_MCP_TOKEN` set, it runs the server behind that token, and the host reaches it at `/mcp/<token>`.

It checks:
- the chart appears;
- the model's calls are answered by maidr inside the chart, `maidr_list_commands` included, and `maidr_run_command` offers exactly the commands maidr lists as runnable;
- a move and a command made while the reader is in the chat wait, the chart's status line says so, and when the reader Tabs in, the move is announced, the command takes effect, and the notice goes;
- with `focus: true`, `maidr_navigate` takes a reader typing in the host page into the chart: their focus lands in it, the point is announced, the status line clears as on a Tab in, and their arrow keys move on from there. Within 10 seconds maidr moves their focus nowhere again: a move and a command with `focus: true` answer `focused: false`, wait, and the status line names them. After that a command with `focus: true` takes them in, answers `queued`, and runs after what waited;
- the arrow keys announce, and the reader's position reaches the host as model context;
- a move and commands made while the reader is in the chart take effect at once, and `maidr_list_charts` gives the model the position a command took the reader to;
- `update_chart` changes the chart in its own view, with no view added: a reader outside it keeps their focus, a reader in it stays in it, both are told, and maidr reads only the new chart; what waited for the reader in the old chart goes with it, and both the model and the status line say so, and a move waiting in the new one joins the change in the status line;
- a view that missed an update catches up on its next poll, and keeps to long polls;
- there are no console errors or CSP violations;
- a step, violin, pie and candlestick chart and a scatter with a trend line each appear: maidr reads each as the layers in the [table above](#tools), ArrowRight announces its first point, and a move the model asks for is announced, or refused by maidr where the table says so;
- when the host cuts long polls, the view falls back to short polls, still answers the model, `update_chart` included, and stops polling when the chart is closed.

`MAIDR_JS_FILE=/path/to/maidr/dist/maidr.js bash e2e/run.sh` runs the same checks against a local build of maidr.js in place of the pinned release.

## Limits

- **The position the chart reports reaches the model with the reader's next message.** Hosts apply `ui/update-model-context` on the next turn, so within a turn the model context misses the model's own moves and commands, and the reader moving on while the model answers. `maidr_list_charts` reads the position live, and the server's instructions tell the model to call it when the reader asks about "this point" and the context may be stale. What remains: maidr knows the position only while the reader is in the chart, so while they are typing to the model the last report stands; and a model that skips the call answers from the context alone.
- **A move or command waits unless the reader asked to be taken there.** Without `focus: true`, a move or command made while the reader is typing to the model is kept, and happens when they Tab back into the chart: the move first, then the commands, half a second apart. The tool answers `applied: "on-next-focus"` and the model is told to say so; the chart's status line says so too, where a screen reader browsing the conversation finds it. With `focus: true`, which the model is told to pass only when the reader asked, maidr moves their focus into the chart and does it there. What remains:
  - **Safari refuses.** WebKit keeps a chart in another page's frame from taking focus without the reader's own key or click, so there maidr answers `focused: false`, and the reader Tabs in themselves and finds the move or command waiting. Chromium and Firefox let the chart take focus.
  - **Once every 10 seconds.** maidr moves focus for the model at most once every 10 seconds on a page, so that a reader who left the chart is not pulled straight back in. A call sooner answers `focused: false` and waits like one without `focus`. Each chart view is a page of its own, so the limit does not span charts: the model could take the reader into a second chart at once, and only its instructions stop it.
  - **The host's own dialogs.** The chart cannot see a dialog the host shows over the conversation, so maidr cannot keep from taking focus from one. The model is told not to pass `focus: true` while one is open; nothing else stops it.
  - At most 8 commands wait.
- **Each `show_chart` call still adds a chart.** Claude mounts a new view for every call to a tool with a UI, and keeps the earlier ones. A chart that changes stays in its view only when the model calls `update_chart`, as the server's instructions ask; a second `show_chart` is a second view.
- **The relay polls.** Each open chart makes a request every 20 seconds, or every 2 seconds on a host that cuts requests held open, where a model's call can also take up to 2 seconds longer to reach the chart. A host that also allows a view fewer than 30 calls a minute leaves some of the model's calls unanswered. Which hosts limit calls from a view is not yet known. The same chart open twice, say in two tabs, makes each copy read the other's polls as cuts, so both settle on short polls.
- **Access is one shared token, and only if you set one.** Without a token, anyone with the server's URL can draw charts. Set `MAIDR_MCP_TOKEN` and every request needs it; Claude and ChatGPT carry it in the URL. That URL, kept in the host's connector settings, is then the secret; rotating it means restarting the server with a new token and updating each host. Full OAuth is not implemented. See [An access token](#an-access-token). A chart can only be read or driven with its `viewId`, a random 24-character token.
- **Ten chart families, and no plotting code.** The model sends data for one of the types [above](#tools), and the server draws it. It deliberately takes no plotting code: anyone with its URL could run code on it. py-maidr's experimental plot types are left out until they have been tried with readers.

## Network and data

- **The server:** receives the data the model sends to draw a chart. It keeps the chart's latest SVG in memory until the chart has gone 15 minutes without polling, and logs nothing about the data beyond the HTTP access log, which shows a token in the URL as `/mcp/<redacted>`.
- **The chart view:** loads maidr.js and the MCP Apps SDK from `cdn.jsdelivr.net`. maidr's own AI chat inside the chart works as it does anywhere else, with a key the reader adds.

## Development

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest
bash e2e/run.sh
```

### How maidr releases reach maidr-mcp

- **maidr.js,** in the chart view, is the version `MAIDR_JS_VERSION` in `server.py` names. `update-maidr.yml` raises it within about an hour of an npm release, once the checks below pass, so an install or image made from `main` after that loads it.
- **py-maidr,** which draws the chart, is resolved afresh by every `uvx` or Docker install, within `maidr>=1.26,<2`, so a new install has a release as soon as PyPI does. `uv.lock`, which the same workflow raises, pins the py-maidr that CI and a checkout test against.

`.github/workflows/update-maidr.yml` checks npm and PyPI every hour. A new maidr.js must carry npm provenance from maidr's release workflow, with a Sigstore signature that `npm audit signatures` verifies. Then ruff, pytest on Python 3.10 and 3.13, and `e2e/run.sh` run against the new versions, and only when all of them pass does the workflow commit them to `main`: as `fix(deps):` when the maidr.js pin moves, since that ships to users, and as `chore(deps):` when only `uv.lock` does. A maidr.js that changes its runnable commands fails `e2e/run.sh` rather than shipping with a stale `RunnableCommand`. When a check fails, nothing is pushed, and an issue names the versions, the step, and the run. A failure that may pass next time, such as a download or the browser's install, is tried again every hour for a day; one that would only repeat, such as a check failing on the new versions, holds them until the issue is closed or the workflow is run by hand. The issue closes itself once an update at least as new reaches `main`. Every update pairs npm's latest maidr.js with PyPI's newest py-maidr, so while a maidr.js release fails, a new py-maidr is tried only with it and `uv.lock` waits too; the issue says how to raise py-maidr alone, by running the workflow by hand with the maidr.js `main` already loads.

To pick another version, run the workflow from the Actions tab with a maidr.js version, or in a checkout run `uv run --no-project python scripts/update_maidr.py update --maidr-js <version> --allow-lower` and open a pull request. The next hourly run raises a lower pin again, so to hold maidr.js at an older version, disable the workflow in the Actions tab until maidr releases a fix.

## License

GPL-3.0-or-later, like the rest of maidr.
