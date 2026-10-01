// End-to-end check in ext-apps' reference host (basic-host): the chart is shown
// through the host, a "model" calls the server's maidr_* tools over HTTP, and a
// reader enters the chart and moves with the arrow keys. Then update_chart changes
// the chart in its own view, with the reader outside it and inside it. Exits
// non-zero on failure.
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

const chart = {
  type: "bar",
  title: "The Number of Tips by Day",
  x_label: "Day",
  y_label: "Count",
  categories: ["Sat", "Sun", "Thur", "Fri"],
  series: [{ values: [87, 76, 62, 19] }],
};

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
  const problems = [];
  page.on("console", (m) => {
    if (m.type() === "error" || /Content Security Policy/i.test(m.text())) problems.push(m.text());
  });

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
  check("the host shows the chart view", !!view);
  if (!view) throw new Error("no chart view");

  // A model learns the viewId from show_chart's result.
  await page.getByText("📤 Tool Result").first().click();
  await page.waitForFunction(() => /viewId [\w-]{20,}/.test(document.body.textContent));
  const viewId = await page.evaluate(() => document.body.textContent.match(/viewId ([\w-]{20,})/)[1]);

  let listed;
  for (let i = 0; i < 60; i++) {
    listed = await callTool("maidr_list_charts", { viewId });
    if (listed.ok) break;
    await sleep(250);
  }
  const layer = listed.content?.charts?.[0]?.layers?.[0];
  check("maidr_list_charts answers from maidr in the view", listed.ok && layer?.type === "bar" && layer?.pointCount === 4, layer);

  const data = await callTool("maidr_get_layer_data", { viewId, layerId: layer.layerId });
  const points = data.content?.points ?? [];
  check("maidr_get_layer_data returns the points with targets", points.length === 4 && points.every((p) => p.target), points.map((p) => p.point));

  const announced = () =>
    view.evaluate(() =>
      [...document.querySelectorAll("[role=alert],[aria-live]")].map((e) => e.textContent.trim()).filter(Boolean).join(" | "),
    );
  const top = points.reduce((a, b) => (b.point.y > a.point.y ? b : a));
  const low = points.reduce((a, b) => (b.point.y < a.point.y ? b : a));

  const away = await callTool("maidr_navigate", { viewId, layerId: layer.layerId, ...top.target });
  check("a move while the reader is in the chat waits for them", away.ok && away.applied === "on-next-focus", away.applied);
  check("  and nothing is announced yet", (await announced()) === "", await announced());

  for (let i = 0; i < 20; i++) {
    await page.keyboard.press("Tab");
    if (await view.evaluate(() => document.hasFocus() && document.activeElement !== document.body).catch(() => false)) break;
  }
  await sleep(600);
  const entered = await announced();
  check("the reader Tabs in and lands on the highest bar", /Sat/.test(entered) && /87/.test(entered), entered);

  await page.keyboard.press("ArrowRight");
  await sleep(900);
  const moved = await announced();
  check("ArrowRight announces the next bar", /Sun/.test(moved) && /76/.test(moved), moved);

  const here = await callTool("maidr_navigate", { viewId, layerId: layer.layerId, ...low.target });
  await sleep(400);
  const now = await announced();
  check("a move while the reader is in the chart is announced at once", here.applied === "now" && /Fri/.test(now) && /19/.test(now), { applied: here.applied, now });

  // Last, because opening the panel takes focus out of the chart. The host shows the model
  // context collapsed to 100 characters; open it to read it all.
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
    focusKept && !outside.hasFocus && /"readerInChart": false/.test(resultText) && outside.status === "Chart updated: Tips by Time",
    { focusKept, viewHasFocus: outside.hasFocus, status: outside.status },
  );
  const second = await onlyChart();
  check(
    "  maidr lists the new chart alone",
    second.count === 1 && second.chartId !== firstChartId && second.layer?.type === "bar" && second.layer?.pointCount === 2,
    second,
  );

  for (let i = 0; i < 30; i++) {
    await page.keyboard.press("Tab");
    if (await view.evaluate(() => document.hasFocus() && document.activeElement !== document.body).catch(() => false)) break;
  }
  await sleep(600);
  await page.keyboard.press("ArrowRight");
  await sleep(900);
  const inNew = await announced();
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
  const onLine = await announced();
  check("  and the arrow keys move through it", /Noon|Evening|Night/.test(onLine) && !/Lunch|Dinner/.test(onLine), onLine);
  await sleep(600);
  // The panel opened above stays open, so it is read without taking the reader out of the chart.
  const contextAfter = await page.evaluate(
    () => [...document.querySelectorAll("div")].map((d) => d.textContent).find((t) => t.startsWith("📋 Model Context")) ?? "",
  );
  check("  and the model context follows them there", /Noon|Evening|Night/.test(contextAfter) && !/Fri/.test(contextAfter), contextAfter);

  check("no console errors or CSP reports", problems.length === 0, problems);
} finally {
  await browser.close();
}

if (failures.length) {
  console.error(`\n${failures.length} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
