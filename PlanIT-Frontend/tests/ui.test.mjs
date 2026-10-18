import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { build } from "esbuild";
import { JSDOM, VirtualConsole } from "jsdom";
const bundled = await build({
  entryPoints: ["src/main.jsx"],
  bundle: true,
  write: false,
  outfile: "test.js",
  format: "iife",
  loader: { ".png": "dataurl" },
  define: { "process.env.NODE_ENV": '"production"' },
});
const js = bundled.outputFiles.find((f) => f.path.endsWith(".js")).text;
const actual = JSON.parse(
  readFileSync("tests/fixtures/backend-dangerous.json", "utf8"),
);
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
async function start(fixture = actual) {
  const errors = [];
  const calls = [];
  const vc = new VirtualConsole();
  vc.on("jsdomError", (e) => errors.push(e));
  const dom = new JSDOM('<!doctype html><div id="root"></div>', {
    url: "http://localhost:5173",
    runScripts: "outside-only",
    pretendToBeVisual: true,
    virtualConsole: vc,
  });
  const w = dom.window;
  w.ResizeObserver = class {
    observe() {}
    disconnect() {}
  };
  w.SVGSVGElement.prototype.createSVGRect = () => ({});
  w.HTMLElement.prototype.scrollTo = function () {};
  w.__PLANIT_LAND__ = { type: "FeatureCollection", features: [] };
  w.EventSource = class {
    addEventListener() {}
    close() {}
  };
  w.fetch = async (path, options = {}) => {
    calls.push([path, options]);
    let data;
    if (path.endsWith("/api/health")) data = { status: "ok" };
    else if (path.endsWith("/api/risks")) data = fixture.map_response;
    else if (path.includes("/detail")) data = fixture.detail_response;
    else if (path.includes("/briefings/preview"))
      data = {
        preview: true,
        briefing: {
          target_date: "2026-10-18",
          timezone: "America/New_York",
          required_safety_actions:
            actual.detail_response.required_safety_actions,
          missing_information: ["Test source coverage is incomplete."],
          rule_based_assessment: {
            availability: "partial",
            highest_known_level: "high",
            available_hours: 12,
            expected_hours: 24,
            highest_risk_periods: [],
          },
          unresolved_crew_reports: [],
        },
      };
    else if (path.endsWith("/briefings")) data = { briefings: [] };
    else data = {};
    return { ok: true, status: 200, json: async () => data };
  };
  w.eval(js);
  await wait(300);
  return { dom, w, document: w.document, errors, calls };
}
function click(ctx, text) {
  const b = [...ctx.document.querySelectorAll("button")].find(
    (b) => b.textContent.trim() === text,
  );
  assert.ok(b, `Button found: ${text}`);
  b.click();
  return b;
}
function fill(ctx, name, value) {
  const el = ctx.document.querySelector(`[name="${name}"]`);
  assert.ok(el, name);
  el.value = value;
  el.dispatchEvent(new ctx.w.Event("input", { bubbles: true }));
}
test("demo site creation works and begins with unknown conditions", async () => {
  const c = await start();
  try {
    assert.match(
      c.document.body.textContent,
      /All site conditions and observations are demo data/,
    );
    click(c, "Add site");
    await wait(40);
    fill(c, "name", "Test construction site");
    fill(c, "lat", "25.5");
    fill(c, "lon", "-80.5");
    c.document
      .querySelector("form")
      .dispatchEvent(
        new c.w.Event("submit", { bubbles: true, cancelable: true }),
      );
    await wait(100);
    assert.match(
      c.document.querySelector("h1").textContent,
      /Test construction site/,
    );
    assert.match(c.document.body.textContent, /Data unavailable/);
    assert.equal(c.document.querySelector('[role="dialog"]'), null);
    assert.equal(c.errors.length, 0, c.errors.map(String).join("\n"));
  } finally {
    c.dom.window.close();
  }
});
test("crew reporting, resolution and resolved-history filter work in demo", async () => {
  const c = await start();
  try {
    click(c, "Crew reports");
    await wait(30);
    click(c, "New report");
    await wait(30);
    fill(c, "description", "Test observation beside access gate");
    c.document
      .querySelector("form")
      .dispatchEvent(
        new c.w.Event("submit", { bubbles: true, cancelable: true }),
      );
    await wait(80);
    assert.match(
      c.document.body.textContent,
      /Test observation beside access gate/,
    );
    const card = [...c.document.querySelectorAll(".report-card")].find((x) =>
      x.textContent.includes("Test observation"),
    );
    card.querySelector("button").click();
    await wait(30);
    click(c, "Mark resolved");
    await wait(80);
    click(c, "Resolved");
    await wait(30);
    assert.match(
      c.document.querySelector(".report-grid").textContent,
      /Test observation beside access gate/,
    );
    assert.equal(c.errors.length, 0, c.errors.map(String).join("\n"));
  } finally {
    c.dom.window.close();
  }
});
test("live connection consumes the actual backend contract and removes demo sites", async () => {
  const c = await start();
  try {
    click(c, "Connect your backend");
    await wait(30);
    c.document
      .querySelector("form")
      .dispatchEvent(
        new c.w.Event("submit", { bubbles: true, cancelable: true }),
      );
    await wait(220);
    assert.match(c.document.body.textContent, /Backend connected/);
    assert.equal(c.document.querySelector(".demo-banner"), null);
    assert.match(
      c.document.body.textContent,
      new RegExp(actual.map_response.sites[0].site.name),
    );
    assert.ok(
      !c.document.body.textContent.includes("Big Cypress mobilization"),
    );
    assert.ok(c.calls.some(([p]) => p.includes("/detail")));
    assert.equal(c.errors.length, 0, c.errors.map(String).join("\n"));
  } finally {
    c.dom.window.close();
  }
});
test("live site shows experimental warning result and pending assessment without changing hazard", async () => {
  const fixture = structuredClone(actual);
  const row = fixture.map_response.sites[0];
  const siteId = row.site.id;
  row.initial_assessment = { status: "running" };
  row.weather_model = { availability: "available", probability: .23, score: 2.3 };
  row.metar = { availability: "stale", reason: "old_observation" };
  fixture.detail_response.site = row.site;
  fixture.detail_response.initial_assessment = { status: "running" };
  fixture.detail_response.weather_model = {
    availability: "available", probability: .23, score: 2.3,
    model_version: "test-frozen-v1", prediction_time: "2026-10-17T12:00:00Z",
    outcome_start: "2026-10-17T12:00:00Z", outcome_end: "2026-10-17T18:00:00Z",
  };
  fixture.detail_response.metar = { availability: "stale", reason: "old_observation",
    station: { station_id: "KJAX", distance_km: 12 }, current_observed_at: "2026-10-17T10:00:00Z" };
  const c = await start(fixture);
  try {
    click(c, "Connect your backend");
    await wait(30);
    c.document.querySelector("form").dispatchEvent(
      new c.w.Event("submit", { bubbles: true, cancelable: true }));
    await wait(220);
    assert.match(c.document.body.textContent, /Experimental six-hour severe thunderstorm warning prediction/);
    assert.match(c.document.body.textContent, /23.00% estimated probability/);
    assert.match(c.document.body.textContent, /Calibration is not established/);
    assert.match(c.document.body.textContent, /METAR Stale/);
    assert.match(c.document.body.textContent, /Assessing…/);
    assert.equal(c.document.querySelector(".weather-model-panel")?.textContent.includes("test-frozen-v1"), true);
    assert.equal(c.errors.length, 0, c.errors.map(String).join("\n"));
  } finally { c.dom.window.close(); }
});

test("briefing preview renders backend limitations without consuming delivery", async () => {
  const c = await start();
  try {
    click(c, "Connect your backend");
    await wait(30);
    c.document
      .querySelector("form")
      .dispatchEvent(
        new c.w.Event("submit", { bubbles: true, cancelable: true }),
      );
    await wait(180);
    click(c, "Daily briefings");
    await wait(60);
    click(c, "Generate preview");
    await wait(120);
    assert.match(
      c.document.querySelector(".briefing-result").textContent,
      /Test source coverage is incomplete/,
    );
    assert.match(
      c.document.querySelector(".briefing-outlook").textContent,
      /12 of 24 hours covered/,
    );
    assert.ok(
      c.calls.some(
        ([p, o]) => p.endsWith("/briefings/preview") && o.method === "POST",
      ),
    );
    assert.ok(
      !c.calls.some(
        ([p, o]) => p.endsWith("/briefings") && o.method === "POST",
      ),
    );
    assert.equal(c.errors.length, 0, c.errors.map(String).join("\n"));
  } finally {
    c.dom.window.close();
  }
});
