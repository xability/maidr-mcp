// End-to-end check in ext-apps' reference host (basic-host): the chart is shown
// through the host, a "model" calls the server's maidr_* tools over HTTP, and a
// reader enters the chart and moves with the arrow keys. Exits non-zero on failure.
//
// The checks run in this order, so that each finds the reader's focus, and the number of
// views, as it expects:
// 1. The bar chart, in the host's first page. show_chart leaves the reader in the host page,
//    in the chat, so the model's calls come first: the read-only ones, maidr_list_commands
//    and maidr_run_command's ids against maidr's, then a move and a command that wait for
//    the reader, which the chart's status line names. Then the reader Tabs in, where the move
//    lands and the command runs, and moves with the arrow keys; then the model's jump, move
//    and command while they are in the chart. Opening the host's model-context panel ends
//    this, and takes the reader out of the chart.
// 2. update_chart, in that same view. With the reader outside: what the model left waiting in
//    the old chart goes with it, and a move the model then makes in the new one joins the
//    change in the status line. The reader Tabs in, and the model updates the chart with them
//    in it; then a view that missed an update catches up. These count the page's views and
//    follow the reader's focus in it, so they come before any other chart opens.
//    Last in this page, the model takes the reader, typing in the host page, into the chart
//    with focus: true, which maidr then will not repeat for 10 seconds; the run waits that out
//    once. Last, because it leaves the reader in the chart and starts maidr's 10 seconds,
//    which no later check of this page expects.
// 3. Each other chart family, in a page of its own, read by maidr and moved through.
// 4. Last, in a context of its own, a host made to cut the view's poll after a few seconds:
//    the view falls back to short polls and still answers, update_chart included. Last, so
//    that no other chart is open while it times the polls.
//
//   HOST_URL       the reference host        (default http://localhost:8080)
//   SERVER_URL     maidr-mcp's MCP endpoint  (default http://localhost:3001/mcp)
//   CHROMIUM       a Chromium binary to use instead of Playwright's own
//   MAIDR_JS_FILE  a local maidr.js build the view loads in place of the pinned release
import { readFile } from "node:fs/promises";
import { chromium } from "playwright-core";

const HOST_URL = process.env.HOST_URL ?? "http://localhost:8080";
const SERVER_URL = process.env.SERVER_URL ?? "http://localhost:3001/mcp";
const MAIDR_JS_FILE = process.env.MAIDR_JS_FILE || undefined;
// Read up front, so a wrong path fails here rather than as a chart that never loads maidr.
const LOCAL_MAIDR_JS = MAIDR_JS_FILE ? await readFile(MAIDR_JS_FILE) : undefined;
const MAIDR_JS_URL = /^https:\/\/cdn\.jsdelivr\.net\/npm\/maidr@[^/]+\/dist\/maidr\.js$/;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const failures = [];
function check(step, ok, detail) {
  console.log(`${ok ? "PASS" : "FAIL"}  ${step}${detail === undefined ? "" : `  ${JSON.stringify(detail)}`}`);
  if (!ok) failures.push(step);
}

/** A JSON-RPC request to the server the way a model's host sends it: the result. */
async function request(method, params) {
  const response = await fetch(SERVER_URL, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json, text/event-stream" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
  });
  const body = await response.text();
  const line = body.split("\n").find((l) => l.startsWith("data: "));
  return JSON.parse(line ? line.slice(6) : body).result;
}

/** The server's tool list, as a model's host reads it. */
async function listTools() {
  return (await request("tools/list", {}))?.tools ?? [];
}

/** A tools/call the way a model's host makes it: the whole result. */
async function callToolResult(name, args) {
  return request("tools/call", { name, arguments: args });
}

/** A tools/call's structured result. */
async function callTool(name, args) {
  return (await callToolResult(name, args))?.structuredContent ?? {};
}

/**
 * A browser context that fetches the CDN through Node, which follows this machine's proxy
 * settings, or serves maidr.js from MAIDR_JS_FILE when it is set. Every context the driver
 * opens comes from here, so every chart loads the same maidr.js.
 */
let localMaidrServed = 0;
async function newContext(browser) {
  const context = await browser.newContext();
  await context.route("https://cdn.jsdelivr.net/**", async (route) => {
    if (LOCAL_MAIDR_JS && MAIDR_JS_URL.test(route.request().url())) {
      localMaidrServed++;
      await route.fulfill({
        status: 200,
        headers: { "content-type": "text/javascript; charset=utf-8" },
        body: LOCAL_MAIDR_JS,
      });
      return;
    }
    const r = await fetch(route.request().url());
    await route.fulfill({
      status: r.status,
      headers: { "content-type": r.headers.get("content-type") ?? "" },
      body: Buffer.from(await r.arrayBuffer()),
    });
  });
  return context;
}

/** The arguments of a maidr_view_poll the host sends to the server, or undefined for any other request. */
function pollArguments(request) {
  if (request.url() !== SERVER_URL || request.method() !== "POST") return undefined;
  try {
    const body = request.postDataJSON();
    return body?.params?.name === "maidr_view_poll" ? body.params.arguments : undefined;
  } catch {
    return undefined;
  }
}

/** Shows `chart` through the host as a model's call would, and returns its view and viewId. */
async function showChart(page, chart) {
  await page.goto(HOST_URL);
  await page.waitForFunction(() =>
    [...document.querySelectorAll("select option")].some((o) => o.value === "show_chart"),
  );
  await page.selectOption("select >> nth=1", "show_chart");
  await page.fill("textarea", JSON.stringify({ chart }));
  await page.click("button[type=submit]");

  let view;
  for (let i = 0; i < 150 && !view; i++) {
    for (const frame of page.frames()) {
      if (await frame.evaluate(() => !!document.querySelector("#chart svg[maidr]")).catch(() => false)) view = frame;
    }
    if (!view) await sleep(200);
  }
  if (!view) return {};

  // A model learns the viewId from show_chart's result.
  await page.getByText("📤 Tool Result").first().click();
  await page.waitForFunction(() => /viewId [\w-]{20,}/.test(document.body.textContent));
  const viewId = await page.evaluate(() => document.body.textContent.match(/viewId ([\w-]{20,})/)[1]);
  return { view, viewId };
}

/** maidr_list_charts, once maidr in the view has started answering. */
async function listCharts(viewId) {
  let listed;
  for (let i = 0; i < 60; i++) {
    listed = await callTool("maidr_list_charts", { viewId });
    if (listed.ok) break;
    await sleep(250);
  }
  return listed;
}

/** The console errors and CSP reports a page logs. */
function watch(page) {
  const problems = [];
  page.on("console", (m) => {
    if (m.type() === "error" || /Content Security Policy/i.test(m.text())) problems.push(m.text());
  });
  return problems;
}

const announced = (view) =>
  view.evaluate(() =>
    [...document.querySelectorAll("[role=alert],[aria-live]")].map((e) => e.textContent.trim()).filter(Boolean).join(" | "),
  );

/** Tabs from the host page into the chart view, as a reader would. */
async function tabInto(page, view) {
  for (let i = 0; i < 30; i++) {
    await page.keyboard.press("Tab");
    if (await view.evaluate(() => document.hasFocus() && document.activeElement !== document.body).catch(() => false)) break;
  }
  await sleep(600);
}

const chart = {
  type: "bar",
  title: "The Number of Tips by Day",
  x_label: "Day",
  y_label: "Count",
  categories: ["Sat", "Sun", "Thur", "Fri"],
  series: [{ values: [87, 76, 62, 19] }],
};

// The other families, each with the layers maidr should read, what the reader's first ArrowRight
// announces and, where maidr gives the first layer's points a target, what a model's move to its
// last point announces. maidr 4.12.0 gives
// no targets for pie, violin, candlestick or trend-line points: the reader moves through them,
// and the model reads them but cannot move the reader there.
const families = [
  {
    chart: {
      type: "step",
      title: "Standard fare by year",
      x_label: "Year",
      y_label: "Fare",
      x: [2019, 2020, 2021, 2022, 2023],
      series: [{ values: [2.5, 2.5, 2.75, 2.75, 2.9] }],
    },
    layers: ["step"],
    first: [/2019/, /2\.5/],
    last: [/2023/, /2\.9/],
  },
  {
    chart: {
      type: "scatter",
      trend: true,
      title: "Tip by bill",
      x_label: "Bill",
      y_label: "Tip",
      x: [10, 15, 20, 25, 30, 35],
      y: [1.5, 2.5, 3, 3.5, 5, 5.5],
    },
    layers: ["point", "smooth"],
    first: [/10/, /1\.5/],
    last: [/35/, /5\.5/],
  },
  {
    chart: {
      type: "violin",
      title: "Petal length by species",
      x_label: "Species",
      y_label: "Petal length",
      groups: [
        { name: "setosa", values: [1.4, 1.4, 1.3, 1.5, 1.4, 1.7, 1.4, 1.5, 1.4, 1.5] },
        { name: "versicolor", values: [4.7, 4.5, 4.9, 4.0, 4.6, 4.5, 4.7, 3.3, 4.6, 3.9] },
        { name: "virginica", values: [6.0, 5.1, 5.9, 5.6, 5.8, 6.6, 4.5, 6.3, 5.8, 6.1] },
      ],
    },
    layers: ["violin_box", "violin_kde"],
    first: [/setosa/],
  },
  {
    chart: {
      type: "pie",
      title: "Tips by day",
      x_label: "Day",
      y_label: "Tips",
      categories: ["Sat", "Sun", "Thur", "Fri"],
      values: [87, 76, 62, 19],
    },
    layers: ["pie"],
    first: [/Sat/, /87/],
  },
  {
    chart: {
      type: "candlestick",
      title: "Acme shares",
      y_label: "Price",
      dates: ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"],
      open: [10, 11, 12, 11.5, 12],
      high: [12, 13, 14, 12, 13.5],
      low: [9, 10, 11, 10.5, 11.8],
      close: [11, 12, 11, 11.8, 13],
    },
    layers: ["candlestick"],
    first: [/2024-01-02|Jan 02/, /11/],
  },
];

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM || undefined });
try {
  const context = await newContext(browser);
  const waits = []; // the wait of each poll the view makes
  const revisions = []; // and the revision of the chart it says it shows
  context.on("request", (request) => {
    const args = pollArguments(request);
    if (args) {
      waits.push(args.wait);
      revisions.push(args.revision);
    }
  });
  const page = await context.newPage();
  const problems = watch(page);

  const { view, viewId } = await showChart(page, chart);
  check("the host shows the chart view", !!view);
  if (!view) throw new Error("no chart view");
  if (MAIDR_JS_FILE) check(`the view loads maidr.js from ${MAIDR_JS_FILE}`, localMaidrServed > 0);

  // Everything maidr's live regions say, in order: an announcement can replace the one before
  // it before a check reads it, as a kept command's does the point the reader landed on.
  await view.evaluate(() => {
    const read = () =>
      [...document.querySelectorAll("[role=alert],[aria-live]")].map((e) => e.textContent.trim()).filter(Boolean).join(" | ");
    window.__heard = [];
    new MutationObserver(() => {
      const now = read();
      if (now && now !== window.__heard.at(-1)) window.__heard.push(now);
    }).observe(document.body, { subtree: true, childList: true, characterData: true });
  });
  const heard = () => view.evaluate(() => window.__heard.slice());
  // The view's own status line, which says when the chart changed and when a move or command
  // waits for the reader.
  const notice = () => view.evaluate(() => document.getElementById("status").textContent);
  // Polled on a timer: a view the host has scrolled out of sight gets no animation frames.
  const noticeComes = (pattern) =>
    view
      .waitForFunction((source) => new RegExp(source).test(document.getElementById("status").textContent), pattern.source, {
        timeout: 3000,
        polling: 100,
      })
      .then(() => true, () => false);

  const listed = await listCharts(viewId);
  const layer = listed.content?.charts?.[0]?.layers?.[0];
  check("maidr_list_charts answers from maidr in the view", listed.ok && layer?.type === "bar" && layer?.pointCount === 4, layer);

  const data = await callTool("maidr_get_layer_data", { viewId, layerId: layer.layerId });
  const points = data.content?.points ?? [];
  check("maidr_get_layer_data returns the points with targets", points.length === 4 && points.every((p) => p.target), points.map((p) => p.point));

  const listedCommands = await callTool("maidr_list_commands", { viewId });
  const braille = listedCommands.commands?.find((c) => c.command === "toggle_braille");
  check(
    "maidr_list_commands answers from maidr in the view, with the reader's keys and modes",
    listedCommands.ok && braille?.runnable === true && braille.keys === "b" &&
      listedCommands.modes?.sound === true && listedCommands.reader?.inChart === false && listedCommands.pending === 0,
    { toggle_braille: braille, modes: listedCommands.modes, reader: listedCommands.reader, error: listedCommands.error },
  );

  // The server offers the model maidr's runnable ids as an enum, copied by hand: it must match
  // what the maidr.js the view loads says it runs, or the model is offered ids maidr refuses.
  const runTool = (await listTools()).find((t) => t.name === "maidr_run_command");
  const offered = [...(runTool?.inputSchema?.properties?.command?.enum ?? [])].sort();
  const runnable = (listedCommands.commands ?? []).filter((c) => c.runnable).map((c) => c.command).sort();
  check(
    "maidr_run_command offers the model exactly the commands maidr lists as runnable",
    runnable.length > 0 && JSON.stringify(offered) === JSON.stringify(runnable),
    { onlyOffered: offered.filter((c) => !runnable.includes(c)), onlyInMaidr: runnable.filter((c) => !offered.includes(c)) },
  );

  const top = points.reduce((a, b) => (b.point.y > a.point.y ? b : a));
  const low = points.reduce((a, b) => (b.point.y < a.point.y ? b : a));

  const away = await callTool("maidr_navigate", { viewId, layerId: layer.layerId, ...top.target });
  check("a move while the reader is in the chat waits for them", away.ok && away.applied === "on-next-focus", away.applied);
  check("  and nothing is announced yet", (await announced(view)) === "", await announced(view));
  check(
    "  and the chart's status line tells the reader it waits",
    await noticeComes(/^The assistant has a move waiting for you: Tab into the chart to hear it\.$/),
    await notice(),
  );

  const keptCommand = await callTool("maidr_run_command", { viewId, command: "toggle_sound" });
  check("a command while the reader is in the chat waits for them", keptCommand.ok && keptCommand.applied === "on-next-focus", keptCommand);
  const counted = await callTool("maidr_list_commands", { viewId });
  check("  and maidr counts it, and the status line names the move and the command", counted.pending === 1 &&
    (await noticeComes(/^The assistant has a move and a command waiting for you: Tab into the chart to hear them\.$/)), {
    pending: counted.pending,
    notice: await notice(),
  });

  const before = (await heard()).length;
  await tabInto(page, view);
  const landed = (await heard()).slice(before);
  const landing = landed.findIndex((t) => /Sat/.test(t) && /87/.test(t));
  check("the reader Tabs in and lands on the highest bar", landing >= 0, landed);

  // maidr runs the kept command 500 ms after the reader enters, behind the move's announcement.
  let afterKept;
  for (let i = 0; i < 20; i++) {
    afterKept = await callTool("maidr_list_commands", { viewId });
    if (afterKept.modes?.sound === false) break;
    await sleep(200);
  }
  const sinceLanding = (await heard()).slice(before + Math.max(landing, 0) + 1);
  check(
    "  then the kept command runs: sound goes off, and the reader hears so",
    afterKept.ok && afterKept.modes?.sound === false && afterKept.pending === 0 && afterKept.reader?.inChart === true &&
      sinceLanding.some((t) => /sound is off/i.test(t)),
    { modes: afterKept.modes, pending: afterKept.pending, reader: afterKept.reader, heard: sinceLanding },
  );
  check("  and the status line's notice is gone", (await notice()) === "", await notice());

  await page.keyboard.press("ArrowRight");
  await sleep(900);
  const moved = await announced(view);
  check("ArrowRight announces the next bar", /Sun/.test(moved) && /76/.test(moved), moved);

  // Where a command takes the reader, the model only learns from the host's model context on
  // its next turn; within this one, maidr_list_charts reads it live.
  const jumpAt = (await heard()).length;
  const jumped = await callTool("maidr_run_command", { viewId, command: "go_to_max_value" });
  await sleep(600);
  const jumpHeard = (await heard()).slice(jumpAt);
  check(
    "a jump the model runs while the reader is in the chart lands at once: go_to_max_value, on the highest bar",
    jumped.ok && jumped.applied === "now" && jumpHeard.some((t) => /Sat/.test(t) && /87/.test(t)),
    { applied: jumped.applied, heard: jumpHeard, error: jumped.error },
  );
  const live = (await callTool("maidr_list_charts", { viewId })).content?.charts?.[0]?.reader;
  check(
    "  and maidr_list_charts gives the model the reader's new position within the turn",
    live?.inChart === true && /Sat/.test(live.position) && /87/.test(live.position),
    live,
  );

  const here = await callTool("maidr_navigate", { viewId, layerId: layer.layerId, ...low.target });
  await sleep(400);
  const now = await announced(view);
  check("a move while the reader is in the chart is announced at once", here.applied === "now" && /Fri/.test(now) && /19/.test(now), { applied: here.applied, now });

  const ranAt = (await heard()).length;
  const ran = await callTool("maidr_run_command", { viewId, command: "toggle_sound" });
  await sleep(400);
  const ranHeard = (await heard()).slice(ranAt);
  check(
    "a command while the reader is in the chart applies at once: sound comes back on",
    ran.ok && ran.applied === "now" && ran.modes?.sound === true && ranHeard.some((t) => /sound is on/i.test(t)),
    { applied: ran.applied, modes: ran.modes, heard: ranHeard, error: ran.error },
  );
  check("  and the status line stays empty", (await notice()) === "", await notice());

  // Last of this part, because opening the panel takes focus out of the chart. The host shows
  // the model context collapsed to 100 characters; open it to read it all.
  await page.getByText("📋 Model Context").first().click();
  const context_ = await page.evaluate(
    () => [...document.querySelectorAll("div")].map((d) => d.textContent).find((t) => t.startsWith("📋 Model Context")) ?? "",
  );
  // The last move was the model's, to Fri: the context follows the reader there, not only their keys.
  check("the host's model context follows the reader, the model's moves included", /Fri/.test(context_) && /19/.test(context_), context_);

  // update_chart replaces the chart in the view show_chart mounted. The host mounts a view for
  // every call to a tool with a UI, so this first update goes through the host, which also
  // leaves the reader in the host page, outside the chart.
  const chartViews = async () => {
    let n = 0;
    for (const frame of page.frames()) {
      if (await frame.evaluate(() => !!document.getElementById("chart")).catch(() => false)) n++;
    }
    return n;
  };
  const shown = () =>
    view.evaluate(() => {
      const svgs = document.querySelectorAll("svg[maidr]");
      const focused = document.activeElement;
      return {
        svgs: svgs.length,
        figures: document.querySelectorAll("figure").length,
        maidr: svgs[0]?.getAttribute("maidr") ?? "",
        status: document.getElementById("status").textContent,
        hasFocus: document.hasFocus(),
        onChart: !!focused?.matches('[tabindex="0"]') && !!svgs[0] && focused.contains(svgs[0]),
      };
    });
  const onlyChart = async () => {
    const listed_ = await callTool("maidr_list_charts", { viewId });
    const charts = listed_.content?.charts ?? [];
    return { count: charts.length, chartId: charts[0]?.chartId, layer: charts[0]?.layers?.[0] };
  };
  const firstChartId = listed.content.charts[0].chartId;
  const byTime = {
    type: "bar",
    title: "Tips by Time",
    x_label: "Time",
    y_label: "Count",
    categories: ["Lunch", "Dinner"],
    series: [{ values: [68, 176] }],
  };
  // A move and a command left waiting in the chart the update replaces: maidr drops them with
  // it, and the status line must stop naming them.
  const keptMove = await callTool("maidr_navigate", { viewId, layerId: layer.layerId, ...top.target });
  const keptRun = await callTool("maidr_run_command", { viewId, command: "toggle_sound" });
  const keptBefore =
    keptMove.applied === "on-next-focus" && keptRun.applied === "on-next-focus" &&
    (await noticeComes(/^The assistant has a move and a command waiting for you: Tab into the chart to hear them\.$/));
  await page.selectOption("select >> nth=1", "update_chart");
  await page.fill("textarea", JSON.stringify({ viewId, chart: byTime }));
  await page.click("button[type=submit]");
  await page.evaluate(() => (window.focusedBeforeUpdate = document.activeElement));
  await page.getByText("📤 Tool Result").nth(1).waitFor();
  const outside = await shown();
  const focusKept = await page.evaluate(() => document.activeElement === window.focusedBeforeUpdate);
  check(
    "update_chart through the host adds no view: the chart changes in its own",
    (await chartViews()) === 1 && outside.svgs === 1 && outside.figures === 1 && /Dinner/.test(outside.maidr) && !/Thur/.test(outside.maidr),
    { views: await chartViews(), svgs: outside.svgs, figures: outside.figures },
  );
  await page.getByText("📤 Tool Result").nth(1).click(); // into the host page, still outside the chart
  const resultText = await page.evaluate(() => document.body.textContent);
  check(
    "  the reader outside the chart keeps their focus, and the chart's status says it changed",
    focusKept && !outside.hasFocus && /"readerInChart": false/.test(resultText) && /"droppedWaiting": true/.test(resultText) &&
      outside.status === "Chart updated: Tips by Time. What the assistant had waiting for you went with the old chart.",
    { focusKept, viewHasFocus: outside.hasFocus, status: outside.status },
  );
  await sleep(1_000); // past the status line's refresh after the update
  const dropped = await callTool("maidr_list_commands", { viewId });
  const afterDrop = await notice();
  check(
    "  and what waited in the old chart goes with it: maidr counts no command, and the status line says it went",
    keptBefore && dropped.ok && dropped.pending === 0 && afterDrop === "Chart updated: Tips by Time. What the assistant had waiting for you went with the old chart.",
    { keptBefore, pending: dropped.pending, status: afterDrop },
  );
  const second = await onlyChart();
  check(
    "  maidr lists the new chart alone",
    second.count === 1 && second.chartId !== firstChartId && second.layer?.type === "bar" && second.layer?.pointCount === 2,
    second,
  );

  // A move the model makes in the new chart, still with the reader outside: the status line
  // names it after the change it already announces, and does not drop that announcement.
  const newPoints = (await callTool("maidr_get_layer_data", { viewId, layerId: second.layer?.layerId })).content?.points ?? [];
  const lunch = newPoints.find((p) => p.point?.x === "Lunch") ?? newPoints[0];
  const keptNew = await callTool("maidr_navigate", { viewId, layerId: second.layer?.layerId, ...lunch?.target });
  check(
    "a move the model makes in the new chart while the reader is outside joins the change in the status line",
    keptNew.applied === "on-next-focus" &&
      (await noticeComes(/^Chart updated: Tips by Time\. What the assistant had waiting for you went with the old chart\. The assistant has a move waiting for you: Tab into the chart to hear it\.$/)),
    { applied: keptNew.applied ?? keptNew.error, status: await notice() },
  );

  const enteredAt = (await heard()).length;
  await tabInto(page, view);
  await sleep(700); // past the 500 ms after which maidr would run a kept command
  const enteredNew = (await heard()).slice(enteredAt);
  const modesIn = (await callTool("maidr_list_commands", { viewId })).modes;
  check(
    "  the reader Tabs in: that move lands, the command dropped with the old chart does not run, and the status line empties",
    enteredNew.some((t) => /Lunch/.test(t) && /68/.test(t)) && modesIn?.sound === true && (await notice()) === "",
    { heard: enteredNew, sound: modesIn?.sound, status: await notice() },
  );
  await page.keyboard.press("ArrowRight");
  await sleep(900);
  const inNew = await announced(view);
  check("the reader Tabs into the new chart and its arrow keys announce its bars", /Lunch|Dinner/.test(inNew) && !/Sat|Sun|Thur|Fri/.test(inNew), inNew);

  // Then the model updates the chart while the reader is in it: they stay in it.
  const byHour = {
    type: "line",
    title: "Tips by Hour",
    x_label: "Hour",
    y_label: "Count",
    x: ["Noon", "Evening", "Night"],
    series: [{ values: [25, 60, 15] }],
  };
  const updated = await callTool("update_chart", { viewId, chart: byHour });
  const inside = await shown();
  check(
    "update_chart while the reader is in the chart keeps them in it, on the new chart",
    updated?.readerInChart === true && inside.hasFocus && inside.onChart && inside.svgs === 1 && inside.figures === 1 && /Evening/.test(inside.maidr),
    { readerInChart: updated?.readerInChart, layers: updated?.layers, ...inside, maidr: undefined },
  );
  check("  and tells them it changed", inside.status === "Chart updated: Tips by Hour", inside.status);
  const third = await onlyChart();
  check(
    "  in the same view, which maidr lists alone",
    (await chartViews()) === 1 && third.count === 1 && third.chartId !== second.chartId && third.layer?.type === "line" && third.layer?.pointCount === 3,
    third,
  );
  await page.keyboard.press("ArrowRight");
  await sleep(900);
  const onLine = await announced(view);
  check("  and the arrow keys move through it", /Noon|Evening|Night/.test(onLine) && !/Lunch|Dinner/.test(onLine), onLine);
  await sleep(600);
  // The panel opened above stays open, so it is read without taking the reader out of the chart.
  const contextAfter = await page.evaluate(
    () => [...document.querySelectorAll("div")].map((d) => d.textContent).find((t) => t.startsWith("📋 Model Context")) ?? "",
  );
  check("  and the model context follows them there", /Noon|Evening|Night/.test(contextAfter) && !/Fri/.test(contextAfter), contextAfter);

  // A view that misses an update catches up on its next poll, which the server answers at once,
  // long before its wait is up: the view must not take that answer for a cut. The host holds the
  // view's next poll back until update_chart has given up on the view.
  let releasePoll;
  const pollReleased = new Promise((resolve) => (releasePoll = resolve));
  let pollHeld = false;
  await context.route(SERVER_URL, async (route) => {
    if (pollHeld || !pollArguments(route.request())) return route.fallback();
    pollHeld = true;
    await pollReleased;
    await route.fallback();
  });
  await callTool("maidr_list_charts", { viewId }); // answers the poll in flight; the next is held
  for (let i = 0; i < 50 && !pollHeld; i++) await sleep(100);
  const missed = await callToolResult("update_chart", { viewId, chart });
  const missedText = missed?.content?.[0]?.text ?? "";
  const heldAt = waits.length;
  releasePoll();
  let caught;
  for (let i = 0; i < 50; i++) {
    caught = await shown();
    if (waits.length > heldAt && caught.status === "Chart updated: The Number of Tips by Day") break;
    await sleep(200);
  }
  await context.unroute(SERVER_URL);
  check(
    "a view that missed an update catches up on its next poll",
    pollHeld && missed?.isError && /keeps the new chart/.test(missedText) && caught.status === "Chart updated: The Number of Tips by Day" && caught.svgs === 1 && /Thur/.test(caught.maidr),
    { pollHeld, missed: missedText, status: caught.status, svgs: caught.svgs },
  );
  check("  and a reader in the chart stays in it", caught.hasFocus && caught.onChart, { hasFocus: caught.hasFocus, onChart: caught.onChart });
  check(
    "  and it keeps to long polls, now on the server's revision",
    waits.length > heldAt && waits.slice(heldAt).every((w) => w === 20) && revisions.at(-1) === 3,
    { waits: waits.slice(heldAt), revisions: revisions.slice(heldAt) },
  );

  // The model takes the reader into the chart because they asked: from the host page, where they
  // are typing, maidr moves their focus into the view's frame and makes the move there. The status
  // line, which still says the chart changed, clears as on a Tab in. Then, within 10 seconds,
  // maidr moves their focus nowhere again, so a reader who went back to the host is not pulled
  // in: the move and command are kept, and the status line names them. After the 10 seconds, a
  // command with focus takes them in again, behind what was kept. maidr 4.12.0 takes no focus,
  // and refuses these calls as "invalid input".
  const noFocusHint = (result) =>
    result?.error === "invalid input"
      ? "the maidr.js the view loads takes no focus (4.12.0 does not): set MAIDR_JS_FILE to a build that does, or raise MAIDR_JS_VERSION to a release that does"
      : undefined;
  const hostFocus = () => page.evaluate(() => document.activeElement?.tagName);
  const inChartFocus = () => view.evaluate(() => document.hasFocus() && document.getElementById("chart").contains(document.activeElement));
  const fourth = await onlyChart();
  const fourthPoints = (await callTool("maidr_get_layer_data", { viewId, layerId: fourth.layer?.layerId })).content?.points ?? [];
  const thur = fourthPoints.find((p) => p.point?.x === "Thur");
  const sat = fourthPoints.find((p) => p.point?.x === "Sat");
  await page.click("textarea");
  await page.keyboard.type(" take me to Thursday");
  const typing = { host: await hostFocus(), view: await inChartFocus(), status: await notice() };
  const takenAt = (await heard()).length;
  const taken = await callTool("maidr_navigate", { viewId, layerId: fourth.layer?.layerId, ...thur?.target, focus: true });
  const focusMovedAt = Date.now(); // maidr's 10 seconds started no later than this
  await sleep(600);
  check(
    "maidr_navigate with focus: true takes a reader typing in the host page into the chart: applied now, focused true",
    typing.host === "TEXTAREA" && !typing.view && taken.ok && taken.applied === "now" && taken.focused === true,
    { typing, result: taken, hint: noFocusHint(taken) },
  );
  const takenTo = await shown();
  check(
    "  their focus is in the chart, in the view's frame",
    (await hostFocus()) === "IFRAME" && (await inChartFocus()) && takenTo.onChart,
    { host: await hostFocus(), view: takenTo.hasFocus, onChart: takenTo.onChart },
  );
  const takenHeard = (await heard()).slice(takenAt);
  const takenLive = (await callTool("maidr_list_charts", { viewId })).content?.charts?.[0]?.reader;
  check(
    "  the point is announced, and maidr_list_charts says they are on it",
    takenHeard.some((t) => /Thur/.test(t) && /62/.test(t)) && takenLive?.inChart === true && /Thur/.test(takenLive.position),
    { heard: takenHeard, reader: takenLive },
  );
  check(
    "  and the status line, which said the chart changed, is empty, as on a Tab in",
    typing.status === "Chart updated: The Number of Tips by Day" && (await notice()) === "",
    { before: typing.status, after: await notice() },
  );
  await page.keyboard.press("ArrowRight");
  await sleep(900);
  const onFromThere = await announced(view);
  check("  and their arrow keys move on from there", /Fri/.test(onFromThere) && /19/.test(onFromThere), onFromThere);

  // The reader goes back to the host page, and the model asks for their focus again at once.
  await page.click("textarea");
  await page.keyboard.type(" thanks");
  const tooSoonMove = await callTool("maidr_navigate", { viewId, layerId: fourth.layer?.layerId, ...sat?.target, focus: true });
  const tooSoon = /less than 10 seconds ago/.test(tooSoonMove.message) && /ask them first/.test(tooSoonMove.message);
  check(
    "within 10 seconds maidr moves their focus nowhere again: the move answers focused false, is kept, and the model is told to ask them",
    Date.now() - focusMovedAt < 10_000 && tooSoonMove.ok && tooSoonMove.applied === "on-next-focus" && tooSoonMove.focused === false && tooSoon &&
      (await hostFocus()) === "TEXTAREA" && !(await inChartFocus()),
    { result: tooSoonMove, host: await hostFocus(), hint: noFocusHint(tooSoonMove) },
  );
  check(
    "  and the status line tells the reader it waits",
    await noticeComes(/^The assistant has a move waiting for you: Tab into the chart to hear it\.$/),
    await notice(),
  );
  const tooSoonRun = await callTool("maidr_run_command", { viewId, command: "toggle_sound", focus: true });
  const keptBoth = await callTool("maidr_list_commands", { viewId });
  check(
    "  a command with focus: true is kept too, and the status line names the move and the command",
    tooSoonRun.ok && tooSoonRun.applied === "on-next-focus" && tooSoonRun.focused === false && /less than 10 seconds ago/.test(tooSoonRun.message) &&
      keptBoth.pending === 1 && keptBoth.reader?.inChart === false &&
      (await noticeComes(/^The assistant has a move and a command waiting for you: Tab into the chart to hear them\.$/)),
    { result: tooSoonRun, pending: keptBoth.pending, status: await notice(), hint: noFocusHint(tooSoonRun) },
  );

  // Past the 10 seconds, a command with focus takes the reader in: it waits its turn behind the
  // kept move and command. Two toggles of sound leave it on, as the checks after this expect.
  await sleep(Math.max(0, focusMovedAt + 10_500 - Date.now()));
  const queuedAt = (await heard()).length;
  const queued = await callTool("maidr_run_command", { viewId, command: "toggle_sound", focus: true });
  check(
    "after 10 seconds maidr_run_command with focus: true takes them in: applied queued, focused true, no modes yet",
    queued.ok && queued.applied === "queued" && queued.focused === true && queued.modes === undefined && (await inChartFocus()),
    { result: queued, view: await inChartFocus(), hint: noFocusHint(queued) },
  );
  let afterQueued;
  for (let i = 0; i < 30; i++) {
    afterQueued = await callTool("maidr_list_commands", { viewId });
    if (afterQueued.pending === 0 && afterQueued.modes?.sound === true) break;
    await sleep(200);
  }
  const queuedHeard = (await heard()).slice(queuedAt);
  const landedSat = queuedHeard.findIndex((t) => /Sat/.test(t) && /87/.test(t));
  const soundOff = queuedHeard.findIndex((t, i) => i > landedSat && /sound is off/i.test(t));
  const soundOn = queuedHeard.findIndex((t, i) => i > soundOff && /sound is on/i.test(t));
  check(
    "  the kept move lands, then the kept command runs and this one after it: sound goes off and back on",
    landedSat >= 0 && soundOff > landedSat && soundOn > soundOff &&
      afterQueued.pending === 0 && afterQueued.modes?.sound === true && afterQueued.reader?.inChart === true,
    { heard: queuedHeard, pending: afterQueued.pending, modes: afterQueued.modes, reader: afterQueued.reader },
  );
  check("  and the status line is empty", (await notice()) === "", await notice());

  check("the reference host holds long polls, and the view keeps to them", waits.length > 0 && waits.every((w) => w === 20), waits);
  check("no console errors or CSP reports", problems.length === 0, problems);

  // Each other family in a page of its own, in this same host: the bar chart's page and its
  // focus are left as they are, and its checks are all done. The poll listener above sees
  // these pages' polls too, but only reads them, and nothing checks the waits after this.
  for (const family of families) {
    const name = family.chart.type + (family.chart.trend ? " with a trend line" : "");
    const page = await context.newPage();
    const problems = watch(page);
    const { view, viewId } = await showChart(page, family.chart);
    check(`${name}: the host shows the chart view`, !!view);
    if (!view) continue;

    const layers = (await listCharts(viewId)).content?.charts?.[0]?.layers ?? [];
    check(
      `${name}: maidr_list_charts reads it as ${family.layers.join(", ")}`,
      layers.map((l) => l.type).join() === family.layers.join() && layers.every((l) => l.pointCount > 0),
      layers.map((l) => [l.type, l.pointCount]),
    );
    if (!layers.length) continue;

    const lasts = [];
    for (const l of layers) {
      const data = await callTool("maidr_get_layer_data", { viewId, layerId: l.layerId, offset: l.pointCount - 1 });
      lasts.push(data.content?.points?.at(-1));
    }
    check(`${name}: maidr_get_layer_data gives the model every layer's points`, lasts.every((p) => p?.point !== undefined), lasts);

    await tabInto(page, view);
    await page.keyboard.press("ArrowRight");
    await sleep(900);
    const moved = await announced(view);
    check(`${name}: ArrowRight announces the first point`, family.first.every((re) => re.test(moved)), moved);

    if (family.last) {
      const here = await callTool("maidr_navigate", { viewId, layerId: layers[0].layerId, ...lasts[0].target });
      await sleep(400);
      const now = await announced(view);
      check(
        `${name}: the model's move to the last point is announced at once`,
        here.applied === "now" && family.last.every((re) => re.test(now)),
        { applied: here.applied, now },
      );
    } else {
      const refused = await callTool("maidr_navigate", { viewId, layerId: layers[0].layerId, row: 0, col: 0 });
      check(`${name}: maidr's own refusal of a move reaches the model`, refused.error === "layer not navigable", refused);
    }

    check(`${name}: no console errors or CSP reports`, problems.length === 0, problems);
    await page.close();
  }
  // Closed before the next host opens, so that no other chart polls while it times them.
  await context.close();

  // A host that will not hold a request open: it cuts the view's poll after CUT_MS. The
  // view's long poll fails, it falls back to short polls, and the model is still answered.
  const CUT_MS = 3_000;
  const cutting = await newContext(browser);
  const polls = []; // { wait, at, carried } for each poll the view makes
  await cutting.route(SERVER_URL, async (route) => {
    const args = pollArguments(route.request());
    if (!args) return route.fallback();
    const poll = { wait: args.wait, at: Date.now(), carried: false };
    polls.push(poll);
    try {
      const response = await route.fetch({ timeout: CUT_MS });
      poll.carried = (await response.text()).includes('"callId"');
      await route.fulfill({ response });
    } catch {
      await route.abort("timedout");
    }
  });
  const cutPage = await cutting.newPage();
  const cspReports = []; // the cut requests are logged as errors, so only CSP counts here
  cutPage.on("console", (m) => {
    if (/Content Security Policy/i.test(m.text())) cspReports.push(m.text());
  });

  const cut = await showChart(cutPage, chart);
  check("a host that cuts long polls shows the chart view", !!cut.view);
  if (!cut.view) throw new Error("no chart view in the host that cuts long polls");
  for (let i = 0; i < 75 && !polls.some((p) => p.wait === 0); i++) await sleep(200);
  check("  its long poll is cut, and the view falls back to short polls", polls[0]?.wait === 20 && polls.some((p) => p.wait === 0), polls.map((p) => p.wait));

  const asked = Date.now();
  const cutListed = await callTool("maidr_list_charts", { viewId: cut.viewId });
  const cutLayer = cutListed.content?.charts?.[0]?.layers?.[0];
  check(
    "  a short poll carries the model's call, and maidr answers inside the reply window",
    cutListed.ok && cutLayer?.type === "bar" && polls.some((p) => p.wait === 0 && p.carried),
    { ok: cutListed.ok, ms: Date.now() - asked, error: cutListed.error },
  );

  const cutData = await callTool("maidr_get_layer_data", { viewId: cut.viewId, layerId: cutLayer?.layerId });
  const cutTop = (cutData.content?.points ?? []).reduce((a, b) => (!a || b.point.y > a.point.y ? b : a), undefined);
  const cutAway = await callTool("maidr_navigate", { viewId: cut.viewId, layerId: cutLayer?.layerId, ...cutTop?.target });
  check("  and a move is kept for the reader", cutAway.ok && cutAway.applied === "on-next-focus", cutAway.applied ?? cutAway.error);

  const updateAsked = Date.now();
  const cutUpdated = await callTool("update_chart", { viewId: cut.viewId, chart: byTime });
  const cutStatus = await cut.view.evaluate(() => document.getElementById("status").textContent);
  const carried = polls.filter((p) => p.carried);
  check(
    "  a short poll carries update_chart, and the chart changes inside the reply window",
    cutUpdated?.readerInChart === false && cutUpdated?.droppedWaiting === true &&
      cutStatus === "Chart updated: Tips by Time. What the assistant had waiting for you went with the old chart." && carried.at(-1)?.wait === 0,
    { ms: Date.now() - updateAsked, readerInChart: cutUpdated?.readerInChart, status: cutStatus, wait: carried.at(-1)?.wait },
  );

  const short = polls.filter((p) => p.wait === 0);
  const gaps = short.slice(1).map((p, i) => p.at - short[i].at);
  check("  short polls come at most one every 2 seconds", gaps.length > 0 && Math.min(...gaps) >= 1_500, gaps);

  await cutPage.getByTitle("Close").first().click();
  await sleep(1_000);
  const polled = polls.length;
  await sleep(5_000);
  check("  closing the chart stops its polls", polls.length === polled, polls.length - polled);
  check("  no CSP reports", cspReports.length === 0, cspReports);
} finally {
  await browser.close();
}

if (failures.length) {
  console.error(`\n${failures.length} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
