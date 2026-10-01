// End-to-end check in ext-apps' reference host (basic-host): the chart is shown
// through the host, a "model" calls the server's maidr_* tools over HTTP, and a
// reader enters the chart and moves with the arrow keys. Exits non-zero on failure.
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

/** A tools/call the way a model's host makes it. */
async function callTool(name, args) {
  const response = await fetch(SERVER_URL, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json, text/event-stream" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name, arguments: args } }),
  });
  const body = await response.text();
  const line = body.split("\n").find((l) => l.startsWith("data: "));
  return JSON.parse(line ? line.slice(6) : body).result?.structuredContent ?? {};
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
  // Fetch the CDN through Node, which follows this machine's proxy settings, or serve
  // maidr.js from MAIDR_JS_FILE when it is set.
  let localMaidrServed = 0;
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

  const listedCommands = await callTool("maidr_list_commands", { viewId });
  const braille = listedCommands.commands?.find((c) => c.command === "toggle_braille");
  check(
    "maidr_list_commands answers from maidr in the view, with the reader's keys and modes",
    listedCommands.ok && braille?.runnable === true && braille.keys === "b" &&
      listedCommands.modes?.sound === true && listedCommands.reader?.inChart === false && listedCommands.pending === 0,
    { toggle_braille: braille, modes: listedCommands.modes, reader: listedCommands.reader, error: listedCommands.error },
  );

  const announced = () =>
    view.evaluate(() =>
      [...document.querySelectorAll("[role=alert],[aria-live]")].map((e) => e.textContent.trim()).filter(Boolean).join(" | "),
    );
  // The view's own status line, which says when a move or command waits for the reader.
  const notice = () => view.evaluate(() => document.getElementById("status").textContent);
  const noticeComes = (pattern) =>
    view
      .waitForFunction((source) => new RegExp(source).test(document.getElementById("status").textContent), pattern.source, {
        timeout: 3000,
      })
      .then(() => true, () => false);
  const top = points.reduce((a, b) => (b.point.y > a.point.y ? b : a));
  const low = points.reduce((a, b) => (b.point.y < a.point.y ? b : a));

  const away = await callTool("maidr_navigate", { viewId, layerId: layer.layerId, ...top.target });
  check("a move while the reader is in the chat waits for them", away.ok && away.applied === "on-next-focus", away.applied);
  check("  and nothing is announced yet", (await announced()) === "", await announced());
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
  for (let i = 0; i < 20; i++) {
    await page.keyboard.press("Tab");
    if (await view.evaluate(() => document.hasFocus() && document.activeElement !== document.body).catch(() => false)) break;
  }
  await sleep(600);
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
  const moved = await announced();
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
  const now = await announced();
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
