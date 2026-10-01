// End-to-end check in ext-apps' reference host (basic-host): the chart is shown
// through the host, a "model" calls the server's maidr_* tools over HTTP, and a
// reader enters the chart and moves with the arrow keys. Exits non-zero on failure.
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

  check("no console errors or CSP reports", problems.length === 0, problems);
} finally {
  await browser.close();
}

if (failures.length) {
  console.error(`\n${failures.length} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
