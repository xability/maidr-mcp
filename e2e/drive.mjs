// End-to-end check in ext-apps' reference host (basic-host): the chart is shown
// through the host, a "model" calls the server's maidr_* tools over HTTP, and a
// reader enters the chart and moves with the arrow keys. Then each other chart family
// is shown the same way, read by maidr, and moved through. Exits non-zero on failure.
//
//   HOST_URL     the reference host        (default http://localhost:8080)
//   SERVER_URL   maidr-mcp's MCP endpoint  (default http://localhost:3001/mcp)
//   CHROMIUM     a Chromium binary to use instead of Playwright's own
import { chromium } from "playwright-core";

const HOST_URL = process.env.HOST_URL ?? "http://localhost:8080";
const SERVER_URL = process.env.SERVER_URL ?? "http://localhost:3001/mcp";
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const failures = [];
function check(step, ok, detail) {
  console.log(`${ok ? "PASS" : "FAIL"}  ${step}${detail === undefined ? "" : `  ${JSON.stringify(detail)}`}`);
  if (!ok) failures.push(step);
}

/** A tools/call the way a model's host makes it. */
async function callTool(name, args) {
  const response = await fetch(SERVER_URL, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json, text/event-stream" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name, arguments: args } }),
  });
  const body = await response.text();
  const line = body.split("\n").find((l) => l.startsWith("data: "));
  return JSON.parse(line ? line.slice(6) : body).result.structuredContent;
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
  for (let i = 0; i < 20; i++) {
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
// last point announces. maidr 4.11.0 gives
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
  const context = await browser.newContext();
  // Fetch the CDN through Node, which follows this machine's proxy settings.
  await context.route("https://cdn.jsdelivr.net/**", async (route) => {
    const r = await fetch(route.request().url());
    await route.fulfill({
      status: r.status,
      headers: { "content-type": r.headers.get("content-type") ?? "" },
      body: Buffer.from(await r.arrayBuffer()),
    });
  });
  const page = await context.newPage();
  const problems = watch(page);

  const { view, viewId } = await showChart(page, chart);
  check("the host shows the chart view", !!view);
  if (!view) throw new Error("no chart view");

  const listed = await listCharts(viewId);
  const layer = listed.content?.charts?.[0]?.layers?.[0];
  check("maidr_list_charts answers from maidr in the view", listed.ok && layer?.type === "bar" && layer?.pointCount === 4, layer);

  const data = await callTool("maidr_get_layer_data", { viewId, layerId: layer.layerId });
  const points = data.content?.points ?? [];
  check("maidr_get_layer_data returns the points with targets", points.length === 4 && points.every((p) => p.target), points.map((p) => p.point));

  const top = points.reduce((a, b) => (b.point.y > a.point.y ? b : a));
  const low = points.reduce((a, b) => (b.point.y < a.point.y ? b : a));

  const away = await callTool("maidr_navigate", { viewId, layerId: layer.layerId, ...top.target });
  check("a move while the reader is in the chat waits for them", away.ok && away.applied === "on-next-focus", away.applied);
  check("  and nothing is announced yet", (await announced(view)) === "", await announced(view));

  await tabInto(page, view);
  const entered = await announced(view);
  check("the reader Tabs in and lands on the highest bar", /Sat/.test(entered) && /87/.test(entered), entered);

  await page.keyboard.press("ArrowRight");
  await sleep(900);
  const moved = await announced(view);
  check("ArrowRight announces the next bar", /Sun/.test(moved) && /76/.test(moved), moved);

  const here = await callTool("maidr_navigate", { viewId, layerId: layer.layerId, ...low.target });
  await sleep(400);
  const now = await announced(view);
  check("a move while the reader is in the chart is announced at once", here.applied === "now" && /Fri/.test(now) && /19/.test(now), { applied: here.applied, now });

  // Last, because opening the panel takes focus out of the chart. The host shows the model
  // context collapsed to 100 characters; open it to read it all.
  await page.getByText("📋 Model Context").first().click();
  const context_ = await page.evaluate(
    () => [...document.querySelectorAll("div")].map((d) => d.textContent).find((t) => t.startsWith("📋 Model Context")) ?? "",
  );
  // The last move was the model's, to Fri: the context follows the reader there, not only their keys.
  check("the host's model context follows the reader, the model's moves included", /Fri/.test(context_) && /19/.test(context_), context_);

  check("no console errors or CSP reports", problems.length === 0, problems);

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
} finally {
  await browser.close();
}

if (failures.length) {
  console.error(`\n${failures.length} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
